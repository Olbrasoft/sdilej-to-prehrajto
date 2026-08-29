from __future__ import annotations

import subprocess
import threading
from pathlib import Path


class GitStateError(RuntimeError):
    pass


class GitStatePersister:
    """Commit durable transfer checkpoints from an Actions runner."""

    MAX_TRACKED_FILE_BYTES = 90 * 1024 * 1024
    PUSH_ATTEMPTS = 5
    CHECKPOINT_INTERVALS = {"source": 25, "attempt": 25, "failure": 25, "success": 25}

    def __init__(self, repo_root: Path, extra_paths: tuple[Path, ...] = ()):
        self.repo_root = repo_root.resolve()
        self.extra_paths = extra_paths
        self._lock = threading.RLock()
        self._pending: dict[str, int] = {}
        self._initial_source_persisted = False

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            raise GitStateError(f"git {args[0]} failed")
        return result

    def __call__(self, state_path: Path, event: str) -> None:
        with self._lock:
            if event == "flush":
                self._persist(state_path, event)
                self._pending.clear()
                return
            interval = self.CHECKPOINT_INTERVALS.get(event)
            if interval is None:
                return
            if event == "source" and not self._initial_source_persisted:
                self._persist(state_path, event)
                self._initial_source_persisted = True
                self._pending.clear()
                return
            self._pending[event] = self._pending.get(event, 0) + 1
            if self._pending[event] < interval:
                return
            self._persist(state_path, event)
            self._pending.clear()

    def _persist(self, state_path: Path, event: str) -> None:
        paths = [state_path, *self.extra_paths]
        relative_paths: list[str] = []
        for path in paths:
            try:
                relative_paths.append(str(path.resolve().relative_to(self.repo_root)))
            except ValueError as error:
                raise GitStateError("State path is outside the repository") from error
        existing = [path for path in relative_paths if (self.repo_root / path).exists()]
        if not existing:
            return
        oversized = [
            path
            for path in existing
            if (self.repo_root / path).is_file()
            and (self.repo_root / path).stat().st_size > self.MAX_TRACKED_FILE_BYTES
        ]
        if oversized:
            raise GitStateError(
                f"Refusing to persist files above 90 MiB: {', '.join(oversized)}"
            )
        self._run("add", "--", *existing)
        if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self._run("commit", "-m", f"chore(sync): persist {event} checkpoint")
        last_error = "concurrent update"
        for attempt in range(self.PUSH_ATTEMPTS):
            pushed = self._run("push", "origin", "HEAD:main", check=False)
            if pushed.returncode == 0:
                return
            last_error = self._failure_detail(pushed)
            if attempt + 1 < self.PUSH_ATTEMPTS:
                rebased, detail = self._rebase_checkpoint(relative_paths)
                if not rebased:
                    last_error = detail
        raise GitStateError(
            "git push failed after concurrent update retries: " + last_error
        )

    def _rebase_checkpoint(self, checkpoint_paths: list[str]) -> tuple[bool, str]:
        # A killed retry must not poison a later checkpoint in the same runner.
        self._run("rebase", "--abort", check=False)
        fetched = self._run("fetch", "origin", "main", check=False)
        if fetched.returncode != 0:
            return False, self._failure_detail(fetched)

        rebased = self._run("rebase", "--autostash", "origin/main", check=False)
        if rebased.returncode == 0:
            return True, ""

        conflicts_result = self._run(
            "diff", "--name-only", "--diff-filter=U", check=False
        )
        conflicts = [
            path.strip() for path in conflicts_result.stdout.splitlines() if path.strip()
        ]
        if conflicts and set(conflicts).issubset(checkpoint_paths):
            # During a rebase, "theirs" is the checkpoint commit being replayed.
            restored = self._run("checkout", "--theirs", "--", *conflicts, check=False)
            if restored.returncode == 0:
                self._run("add", "--", *conflicts)
                continued = self._run(
                    "-c", "core.editor=true", "rebase", "--continue", check=False
                )
                if continued.returncode == 0:
                    return True, ""
                detail = self._failure_detail(continued)
            else:
                detail = self._failure_detail(restored)
        else:
            detail = self._failure_detail(rebased)

        self._run("rebase", "--abort", check=False)
        return False, detail

    @staticmethod
    def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return detail[-1] if detail else "unknown git error"

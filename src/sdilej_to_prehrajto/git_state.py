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
    INITIAL_SOURCE_CHECKPOINTS = 4
    INITIAL_DEEP_CHECKPOINTS = 1
    # Publish one upload-shard-sized source batch at a time. Keeping 25 sources
    # only inside a producer runner starves the six upload workers for too long.
    # Negative discovery results are cheap and common during a full backlog
    # rescan. Persist them in larger batches so six preparation workers do not
    # create a bot commit every few seconds. Verified sources and uploads still
    # use small batches because those checkpoints feed the live upload queue.
    # Transfer checkpoints are written immediately: losing a prepared target
    # ID can duplicate a multi-gigabyte upload after a runner restart.
    CHECKPOINT_INTERVALS = {
        "source": 4,
        "attempt": 250,
        "deep_scan": 10,
        "prepared": 1,
        "processing": 1,
        "failure": 1,
        "success": 1,
    }

    def __init__(self, repo_root: Path, extra_paths: tuple[Path, ...] = ()):
        self.repo_root = repo_root.resolve()
        self.extra_paths = extra_paths
        self._lock = threading.RLock()
        self._pending: dict[str, int] = {}
        self._source_checkpoints = 0
        self._deep_checkpoints = 0

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
            if (
                event == "source"
                and self._source_checkpoints < self.INITIAL_SOURCE_CHECKPOINTS
            ):
                self._persist(state_path, event)
                self._source_checkpoints += 1
                self._pending.clear()
                return
            if (
                event == "deep_scan"
                and self._deep_checkpoints < self.INITIAL_DEEP_CHECKPOINTS
            ):
                # Publish one diagnostic result immediately after startup;
                # subsequent deep results remain batched to protect history.
                self._persist(state_path, event)
                self._deep_checkpoints += 1
                self._pending.clear()
                return
            self._pending[event] = self._pending.get(event, 0) + 1
            if self._pending[event] < interval:
                return
            self._persist(state_path, event)
            self._pending.clear()

    def read_remote_file(self, relative_path: str) -> str:
        """Read a fresh origin/main file while serializing local git operations."""
        if relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise GitStateError("Remote path must stay inside the repository")
        with self._lock:
            fetched = self._run("fetch", "origin", "main", check=False)
            if fetched.returncode != 0:
                raise GitStateError(
                    "git fetch failed while refreshing queue: "
                    + self._failure_detail(fetched)
                )
            shown = self._run("show", f"FETCH_HEAD:{relative_path}", check=False)
            if shown.returncode != 0:
                raise GitStateError(
                    "git show failed while refreshing queue: "
                    + self._failure_detail(shown)
                )
            return shown.stdout

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

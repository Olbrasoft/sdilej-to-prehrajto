from __future__ import annotations

import subprocess
import threading
from pathlib import Path


class GitStateError(RuntimeError):
    pass


class GitStatePersister:
    """Commit durable transfer checkpoints from an Actions runner."""

    MAX_TRACKED_FILE_BYTES = 90 * 1024 * 1024
    CHECKPOINT_INTERVALS = {"source": 25, "attempt": 25, "failure": 25, "success": 25}

    def __init__(self, repo_root: Path, extra_paths: tuple[Path, ...] = ()):
        self.repo_root = repo_root.resolve()
        self.extra_paths = extra_paths
        self._lock = threading.RLock()
        self._pending: dict[str, int] = {}

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
        for _attempt in range(3):
            pushed = self._run(
                "push", "origin", "HEAD:main", check=False
            )
            if pushed.returncode == 0:
                return
            rebased = self._run("pull", "--rebase", "origin", "main", check=False)
            if rebased.returncode != 0:
                raise GitStateError("git pull --rebase failed while retrying state push")
        raise GitStateError("git push failed after concurrent update retries")

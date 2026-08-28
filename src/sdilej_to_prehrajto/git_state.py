from __future__ import annotations

import subprocess
from pathlib import Path


class GitStateError(RuntimeError):
    pass


class GitStatePersister:
    """Commit durable transfer checkpoints from an Actions runner."""

    DURABLE_EVENTS = {"plan", "prepared", "attempt", "success"}

    def __init__(self, repo_root: Path, extra_paths: tuple[Path, ...] = ()):
        self.repo_root = repo_root.resolve()
        self.extra_paths = extra_paths

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
        if event not in self.DURABLE_EVENTS:
            return
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
        self._run("add", "--", *existing)
        if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self._run("commit", "-m", f"chore(sync): persist {event} checkpoint")
        self._run("push", "origin", "HEAD:main")

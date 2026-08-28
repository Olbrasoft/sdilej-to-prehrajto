from pathlib import Path

from sdilej_to_prehrajto import cli


def test_repo_root_prefers_github_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    assert cli.repo_root() == tmp_path.resolve()

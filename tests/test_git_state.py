import subprocess

from sdilej_to_prehrajto.git_state import GitStatePersister
from sdilej_to_prehrajto.git_state import GitStateError


def test_git_checkpoints_batch_sources_and_persist_transfers(tmp_path, monkeypatch) -> None:
    persister = GitStatePersister(tmp_path)
    persisted = []
    monkeypatch.setattr(
        persister,
        "_persist",
        lambda state_path, event: persisted.append((state_path, event)),
    )
    state_path = tmp_path / "state.json"

    for _index in range(100):
        persister(state_path, "claim")
    for _index in range(4):
        persister(state_path, "source")
    assert persisted == [(state_path, "source")] * 4

    for _index in range(3):
        persister(state_path, "source")
    assert persisted == [(state_path, "source")] * 4

    persister(state_path, "source")
    assert persisted == [(state_path, "source")] * 5

    persister(state_path, "prepared")
    persister(state_path, "processing")
    persister(state_path, "failure")
    persister(state_path, "success")
    assert persisted == [(state_path, "source")] * 5 + [
        (state_path, "prepared"),
        (state_path, "processing"),
        (state_path, "failure"),
        (state_path, "success"),
    ]


def test_git_checkpoints_batch_negative_discovery_results(tmp_path, monkeypatch) -> None:
    persister = GitStatePersister(tmp_path)
    persisted = []
    monkeypatch.setattr(
        persister,
        "_persist",
        lambda state_path, event: persisted.append((state_path, event)),
    )
    state_path = tmp_path / "state.json"

    for _index in range(249):
        persister(state_path, "attempt")
    assert persisted == []

    persister(state_path, "attempt")
    assert persisted == [(state_path, "attempt")]

    persister(state_path, "deep_scan")
    assert persisted == [(state_path, "attempt"), (state_path, "deep_scan")]

    for _index in range(9):
        persister(state_path, "deep_scan")
    assert persisted == [(state_path, "attempt"), (state_path, "deep_scan")]

    persister(state_path, "deep_scan")
    assert persisted == [
        (state_path, "attempt"),
        (state_path, "deep_scan"),
        (state_path, "deep_scan"),
    ]


def test_git_flush_persists_final_partial_batch(tmp_path, monkeypatch) -> None:
    persister = GitStatePersister(tmp_path)
    persisted = []
    monkeypatch.setattr(
        persister,
        "_persist",
        lambda state_path, event: persisted.append((state_path, event)),
    )
    state_path = tmp_path / "state.json"

    persister(state_path, "attempt")
    persister(state_path, "flush")
    assert persisted == [(state_path, "flush")]


def test_git_state_reads_fresh_remote_file_without_touching_worktree(
    tmp_path, monkeypatch
) -> None:
    persister = GitStatePersister(tmp_path)
    commands = []

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        stdout = '{"cr_film_id": 1}\n' if args[0] == "show" else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(persister, "_run", fake_run)

    payload = persister.read_remote_file("manifests/selected-sources.jsonl")

    assert payload == '{"cr_film_id": 1}\n'
    assert commands == [
        ("fetch", "origin", "main"),
        ("show", "FETCH_HEAD:manifests/selected-sources.jsonl"),
    ]


def test_git_checkpoint_rejects_file_near_github_hard_limit(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b"x" * 101)
    persister = GitStatePersister(tmp_path)
    monkeypatch.setattr(persister, "MAX_TRACKED_FILE_BYTES", 100)

    try:
        persister(state_path, "flush")
    except GitStateError as error:
        assert "above 90 MiB" in str(error)
    else:
        raise AssertionError("oversized checkpoint was accepted")


def test_git_checkpoint_retries_after_failed_rebase(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"successes": 25}')
    persister = GitStatePersister(tmp_path)
    commands = []
    push_results = iter([1, 1, 0])
    rebase_results = iter([1, 0])

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        returncode = 0
        stderr = ""
        if args[0] == "push":
            returncode = next(push_results)
            stderr = "non-fast-forward" if returncode else ""
        elif args[:3] == ("diff", "--cached", "--quiet"):
            returncode = 1
        elif args[:3] == ("rebase", "--autostash", "origin/main"):
            returncode = next(rebase_results)
            stderr = "temporary rebase failure" if returncode else ""
        elif args[:3] == ("diff", "--name-only", "--diff-filter=U"):
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, returncode, "", stderr)

    monkeypatch.setattr(persister, "_run", fake_run)

    persister(state_path, "flush")

    assert commands.count(("rebase", "--abort")) == 3
    assert commands.count(("fetch", "origin", "main")) == 2
    assert commands.count(("rebase", "--autostash", "origin/main")) == 2


def test_git_checkpoint_resolves_owned_rebase_conflict(
    tmp_path, monkeypatch
) -> None:
    persister = GitStatePersister(tmp_path)
    commands = []

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[:3] == ("rebase", "--autostash", "origin/main"):
            return subprocess.CompletedProcess(args, 1, "", "conflict")
        if args[:3] == ("diff", "--name-only", "--diff-filter=U"):
            return subprocess.CompletedProcess(args, 0, "state/sync.json\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(persister, "_run", fake_run)

    rebased, detail = persister._rebase_checkpoint(["state/sync.json"])

    assert rebased is True
    assert detail == ""
    assert ("checkout", "--theirs", "--", "state/sync.json") in commands
    assert ("-c", "core.editor=true", "rebase", "--continue") in commands

from sdilej_to_prehrajto.git_state import GitStatePersister
from sdilej_to_prehrajto.git_state import GitStateError


def test_git_checkpoints_batch_events_and_ignore_claims(tmp_path, monkeypatch) -> None:
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
    for _index in range(24):
        persister(state_path, "source")
    assert persisted == []

    persister(state_path, "source")
    assert persisted == [(state_path, "source")]


def test_git_flush_persists_final_partial_batch(tmp_path, monkeypatch) -> None:
    persister = GitStatePersister(tmp_path)
    persisted = []
    monkeypatch.setattr(
        persister,
        "_persist",
        lambda state_path, event: persisted.append((state_path, event)),
    )
    state_path = tmp_path / "state.json"

    persister(state_path, "success")
    persister(state_path, "flush")
    assert persisted == [(state_path, "flush")]


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

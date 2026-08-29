from sdilej_to_prehrajto.cli import additional_worker_count, exclude_uploaded_films


def test_empty_plan_does_not_create_additional_workers() -> None:
    assert additional_worker_count(4, 0) == 0
    assert additional_worker_count(4, 1) == 0
    assert additional_worker_count(4, 4) == 3
from sdilej_to_prehrajto.models import Film
from sdilej_to_prehrajto.state import StateStore


def test_prepare_backlog_excludes_already_uploaded_films(tmp_path) -> None:
    state = StateStore(tmp_path / "sync.json")
    state.record_success(1, {"target_video_id": "777"})
    films = [
        Film(1, "uploaded", "Uploaded", None, 2000, 90, "en"),
        Film(2, "pending", "Pending", None, 2001, 90, "en"),
    ]

    pending = exclude_uploaded_films(films, state)

    assert [film.cr_film_id for film in pending] == [2]

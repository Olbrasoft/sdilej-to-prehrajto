from sdilej_to_prehrajto.cli import (
    additional_worker_count,
    exclude_uploaded_films,
    prepare_source_batch,
)


def test_empty_plan_does_not_create_additional_workers() -> None:
    assert additional_worker_count(4, 0) == 0
    assert additional_worker_count(4, 1) == 0
    assert additional_worker_count(4, 4) == 3
    assert additional_worker_count(6, 6) == 5
    assert additional_worker_count(6, 0, refill_enabled=True) == 0
    assert additional_worker_count(6, 1, refill_enabled=True) == 5
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


def test_prepare_workers_search_disjoint_backlog_slices() -> None:
    calls = []

    class Pipeline:
        def __init__(self, worker: int):
            self.worker = worker

        def prepare_sources(
            self, films, limit, *, max_scan, deadline_monotonic
        ):
            calls.append(
                (
                    self.worker,
                    [film.cr_film_id for film in films],
                    limit,
                    max_scan,
                    deadline_monotonic,
                )
            )
            return [{"worker": self.worker, "film": film.cr_film_id} for film in films[:limit]]

    films = [
        Film(film_id, f"film-{film_id}", f"Film {film_id}", None, 2000, 90, "en")
        for film_id in range(1, 7)
    ]

    rows = prepare_source_batch(
        [Pipeline(0), Pipeline(1)],
        films,
        4,
        max_scan=20,
        deadline_monotonic=123.0,
    )

    assert sorted(calls) == [
        (0, [1, 3, 5], 2, 20, 123.0),
        (1, [2, 4, 6], 2, 20, 123.0),
    ]
    assert {row["film"] for row in rows} == {1, 2, 3, 4}

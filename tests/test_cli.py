from sdilej_to_prehrajto.cli import (
    MAX_PREPARE_WORKERS,
    additional_worker_count,
    exclude_uploaded_films,
    prepare_source_batch,
    prepare_source_lane,
)
from sdilej_to_prehrajto.models import Film
from sdilej_to_prehrajto.state import StateStore


def test_empty_plan_does_not_create_additional_workers() -> None:
    assert additional_worker_count(4, 0) == 0
    assert additional_worker_count(4, 1) == 0
    assert additional_worker_count(4, 4) == 3
    assert additional_worker_count(6, 6) == 5
    assert additional_worker_count(6, 0, refill_enabled=True) == 0
    assert additional_worker_count(6, 1, refill_enabled=True) == 5


def test_preparation_supports_six_independent_workers() -> None:
    assert MAX_PREPARE_WORKERS == 6


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


def test_prepare_lane_flushes_only_when_lane_stops(monkeypatch) -> None:
    class Counter:
        def __init__(self):
            self.calls = 0

        def compact(self):
            self.calls += 1

        def persist_external(self, event):
            assert event == "flush"
            self.calls += 1

    class Pipeline:
        def __init__(self):
            self.selected_sources = Counter()
            self.state = Counter()
            self.prepares = 0

        def prepare_sources(self, _films, _limit, **_kwargs):
            self.prepares += 1
            return []

    pipeline = Pipeline()
    clock = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr(
        "sdilej_to_prehrajto.cli.time.monotonic", lambda: next(clock)
    )
    monkeypatch.setattr("sdilej_to_prehrajto.cli.time.sleep", lambda _seconds: None)

    result = prepare_source_lane(
        [pipeline],
        [],
        1,
        max_scan=1,
        deadline_monotonic=10.0,
    )

    assert result == []
    assert pipeline.prepares == 2
    assert pipeline.selected_sources.calls == 1
    assert pipeline.state.calls == 1

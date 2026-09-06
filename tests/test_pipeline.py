import threading

import pytest

from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.pipeline import SyncPipeline, plan_sha
from sdilej_to_prehrajto.prehrajto import UploadResult
from sdilej_to_prehrajto.ranking import SELECTION_POLICY
from sdilej_to_prehrajto.sdilej import DeepScanRequired, SdilejError
from sdilej_to_prehrajto.sources import SelectedSourceStore
from sdilej_to_prehrajto.state import StateStore
from sdilej_to_prehrajto.subtitles import SubtitleQueue


def row(probability: float) -> dict:
    return {
        "film": {"cr_film_id": 1},
        "selected": {
            "source_id": "10",
            "width": 3840,
            "height": 2160,
            "audio_language": "cs",
            "language_tier": "czech_audio",
            "language_probability": probability,
        },
        "display_name": "Film (2000) 4K CZ Dabing",
    }


def test_plan_approval_digest_ignores_nondeterministic_probability() -> None:
    assert plan_sha([row(0.91)]) == plan_sha([row(0.99)])


@pytest.mark.parametrize("completed", [False, True])
def test_target_reconciliation_does_not_require_available_source(tmp_path, monkeypatch, completed):
    class OfflineProvider:
        def refresh_approved(self, *_args, **_kwargs):
            pytest.fail("Existing target must be reconciled without fetching its source")

    state = StateStore(tmp_path / "state.json")
    state.record_prepared(1, "777", 100)
    pipeline = SyncPipeline(
        source_provider=OfflineProvider(), source_session=object(), target_session=object(),
        state=state, subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    candidate = Candidate("10", "https://sdilej.cz/10/film.mkv", "Film", size_bytes=100)
    film = Film(1, "film", "Film", None, 2000, 100, "en")
    pipeline._selected[1] = candidate
    ids = iter([None, "777"])
    monkeypatch.setattr(pipeline, "_target_id_by_name", lambda *_: next(ids))
    monkeypatch.setattr(pipeline, "_target_completed_and_named", lambda *_: completed)
    pipeline._execute_shard(
        [{"film": film.to_dict(), "display_name": "Film", "needs_czech_subtitles": False}],
        pipeline.source_session, pipeline.target_session, "worker",
    )
    assert state.uploaded(1) == completed
    if not completed:
        assert state.snapshot(1)["prepared"]["target_video_id"] == "777"


def test_prepare_replaces_candidate_from_old_selection_policy(tmp_path) -> None:
    selected_sources = SelectedSourceStore(tmp_path / "sources.jsonl")
    selected_sources.record(
        {
            "cr_film_id": 1,
            "source_id": "oversized-old",
            "source_url": "https://sdilej.cz/1/oversized.mkv",
            "selection_policy": "largest-file-v0",
        }
    )
    replacement = Candidate(
        "balanced-new",
        "https://sdilej.cz/2/balanced.mkv",
        "Film (2000) 1080p",
        size_bytes=5_000_000_000,
        duration_sec=6000,
        width=1920,
        height=1080,
        language_tier=LanguageTier.CZECH_AUDIO,
        audio_language="cs",
        match_tier=MatchTier.STRONG,
        filename="film-h264.mkv",
        video_codec="h264",
    )

    class Provider:
        def discover(self, _film):
            return [replacement]

    state = StateStore(tmp_path / "state.json")
    state.record_upload_failure(
        1,
        {
            "status": "source_refresh_failed",
            "source_id": "oversized-old",
            "permanent": False,
        },
    )
    pipeline = SyncPipeline(
        source_provider=Provider(),
        source_session=object(),
        target_session=object(),
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=selected_sources,
    )
    film = Film(1, "film", "Film", None, 2000, 100, "en")

    prepared = pipeline.prepare_sources([film], 1)

    assert prepared[0]["selected"]["source_id"] == "balanced-new"
    assert selected_sources.get(1)["selection_policy"] == SELECTION_POLICY


def test_prepare_defers_failed_attempt_from_current_policy(tmp_path) -> None:
    selected_sources = SelectedSourceStore(tmp_path / "sources.jsonl")
    selected_sources.record(
        {
            "cr_film_id": 1,
            "source_id": "old",
            "source_url": "https://sdilej.cz/1/old.mkv",
            "selection_policy": "old-policy",
        }
    )

    class Provider:
        calls = 0

        def discover(self, _film):
            self.calls += 1
            return []

    provider = Provider()
    state = StateStore(tmp_path / "state.json")
    pipeline = SyncPipeline(
        source_provider=provider,
        source_session=object(),
        target_session=object(),
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=selected_sources,
    )
    film = Film(1, "film", "Film", None, 2000, 100, "en")

    assert pipeline.prepare_sources([film], 1) == []
    assert pipeline.prepare_sources([film], 1) == []

    assert provider.calls == 1
    attempt = state.snapshot(1)["attempts"][-1]
    assert attempt["selection_policy"] == SELECTION_POLICY
    assert attempt["discovery_complete"] is True


def test_prepare_continues_after_transient_source_discovery_failure(tmp_path) -> None:
    candidate = Candidate(
        "prepared-two",
        "https://sdilej.cz/2/film-two.mkv",
        "Film Two (2000) 1080p CZ",
        size_bytes=4_000_000_000,
        duration_sec=6000,
        width=1920,
        height=1080,
        language_tier=LanguageTier.CZECH_AUDIO,
        audio_language="cs",
        match_tier=MatchTier.STRONG,
        filename="film-two.mkv",
        video_codec="h264",
    )

    class Provider:
        def discover(self, film):
            if film.cr_film_id == 1:
                raise SdilejError("Temporary search disconnect")
            return [candidate]

    state = StateStore(tmp_path / "state.json")
    pipeline = SyncPipeline(
        source_provider=Provider(),
        source_session=object(),
        target_session=object(),
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    films = [
        Film(1, "film-one", "Film One", None, 2000, 100, "en"),
        Film(2, "film-two", "Film Two", None, 2000, 100, "en"),
    ]

    prepared = pipeline.prepare_sources(films, 1)

    assert prepared[0]["film"]["cr_film_id"] == 2
    attempt = state.snapshot(1)["attempts"][-1]
    assert attempt["status"] == "source_discovery_failed"
    assert attempt["selection_policy"] == SELECTION_POLICY


def test_fast_prepare_delegates_difficult_film_to_deep_lane(tmp_path) -> None:
    candidate = Candidate(
        "deep-source",
        "https://sdilej.cz/2/deep-source.mkv",
        "Film One (2000) 1080p CZ",
        size_bytes=4_000_000_000,
        duration_sec=6000,
        width=1920,
        height=1080,
        language_tier=LanguageTier.CZECH_AUDIO,
        audio_language="cs",
        match_tier=MatchTier.STRONG,
        filename="deep-source.mkv",
        video_codec="h264",
    )

    class FastProvider:
        def discover(self, _film):
            raise DeepScanRequired("Too many candidates")

    class DeepProvider:
        def discover(self, _film):
            return [candidate]

    state = StateStore(tmp_path / "state.json")
    selected_sources = SelectedSourceStore(tmp_path / "sources.jsonl")
    common = {
        "source_session": object(),
        "target_session": object(),
        "state": state,
        "subtitle_queue": SubtitleQueue(tmp_path / "subtitles.jsonl"),
        "selected_sources": selected_sources,
    }
    fast = SyncPipeline(source_provider=FastProvider(), **common)
    deep = SyncPipeline(source_provider=DeepProvider(), **common)
    film = Film(1, "film-one", "Film One", None, 2000, 100, "en")

    assert fast.prepare_sources([film], 1) == []
    delegated = state.snapshot(1)["attempts"][-1]
    assert delegated["status"] == "source_deep_scan_needed"
    assert delegated["reason"] == "Too many candidates"

    prepared = deep.prepare_sources([film], 1, deep_scan_only=True)

    assert prepared[0]["selected"]["source_id"] == "deep-source"
    assert selected_sources.get(1)["selection_policy"] == SELECTION_POLICY


def test_continuous_plan_uses_replacement_after_old_source_failure(tmp_path) -> None:
    selected_sources = SelectedSourceStore(tmp_path / "sources.jsonl")
    selected_sources.record(
        {
            "cr_film_id": 1,
            "source_id": "compact-new",
            "source_url": "https://sdilej.cz/2/compact.mkv",
            "source_filename": "Film (2000) 4K H265 CZ.mkv",
            "size_bytes": 4_900_000_000,
            "duration_sec": 6000,
            "width": 3840,
            "height": 1600,
            "audio_language": "cs",
            "language_tier": "czech_audio",
            "match_tier": "strong",
            "selection_policy": SELECTION_POLICY,
            "display_name": "Film (2000) 4K CZ Dabing",
        }
    )
    state = StateStore(tmp_path / "state.json")
    state.record_upload_failure(
        1,
        {
            "status": "source_refresh_failed",
            "source_id": "oversized-old",
            "permanent": False,
        },
    )
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session=object(),
        target_session=object(),
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=selected_sources,
    )
    film = Film(1, "film", "Film", None, 2000, 100, "en")

    plan = pipeline.build_plan([film], 1, verified_only=True)

    assert plan[0]["selected"]["source_id"] == "compact-new"


def test_continuous_scan_skips_films_without_prepared_sources(tmp_path) -> None:
    selected_sources = SelectedSourceStore(tmp_path / "sources.jsonl")
    selected_sources.record(
        {
            "cr_film_id": 4,
            "source_id": "prepared-fourth",
            "source_url": "https://sdilej.cz/4/film.mkv",
            "source_filename": "Film 4 (2000) 4K H265 CZ.mkv",
            "size_bytes": 4_900_000_000,
            "duration_sec": 6000,
            "width": 3840,
            "height": 1600,
            "audio_language": "cs",
            "language_tier": "czech_audio",
            "match_tier": "strong",
            "selection_policy": SELECTION_POLICY,
            "display_name": "Film 4 (2000) 4K CZ Dabing",
        }
    )
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session=object(),
        target_session=object(),
        state=StateStore(tmp_path / "state.json"),
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=selected_sources,
    )
    films = [
        Film(film_id, f"film-{film_id}", f"Film {film_id}", None, 2000, 100, "en")
        for film_id in range(1, 5)
    ]

    plan = pipeline.build_plan(
        films,
        1,
        max_scan=1,
        verified_only=True,
    )

    assert plan[0]["selected"]["source_id"] == "prepared-fourth"


def test_continuous_plan_skips_live_claim_and_fills_from_queue(tmp_path) -> None:
    selected_sources = SelectedSourceStore(tmp_path / "sources.jsonl")
    for film_id in (1, 2):
        selected_sources.record(
            {
                "cr_film_id": film_id,
                "source_id": f"prepared-{film_id}",
                "source_url": f"https://sdilej.cz/{film_id}/film.mkv",
                "source_filename": f"Film {film_id} (2000) 4K CZ.mkv",
                "size_bytes": 4_900_000_000,
                "duration_sec": 6000,
                "width": 3840,
                "height": 1600,
                "audio_language": "cs",
                "language_tier": "czech_audio",
                "match_tier": "strong",
                "selection_policy": SELECTION_POLICY,
                "display_name": f"Film {film_id} (2000) 4K CZ Dabing",
            }
        )
    state = StateStore(tmp_path / "state.json")
    assert state.claim_upload(1, "interrupted-run")
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session=object(),
        target_session=object(),
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=selected_sources,
    )
    films = [
        Film(film_id, f"film-{film_id}", f"Film {film_id}", None, 2000, 100, "en")
        for film_id in (1, 2)
    ]

    plan = pipeline.build_plan(films, 1, verified_only=True)

    assert plan[0]["selected"]["source_id"] == "prepared-2"


def test_execute_distributes_rows_across_four_session_shards(
    tmp_path, monkeypatch
) -> None:
    state = StateStore(tmp_path / "state.json")
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session="source-default",
        target_session="target-default",
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    plan = []
    for film_id in range(1, 7):
        film = Film(film_id, f"film-{film_id}", f"Film {film_id}", None, 2000, 90, "en")
        selected = Candidate(
            str(film_id),
            f"https://sdilej.cz/{film_id}/film.mkv",
            f"Film {film_id}",
            size_bytes=100,
            duration_sec=5400,
            width=1920,
            height=1080,
            language_tier=LanguageTier.CZECH_AUDIO,
            audio_language="cs",
            match_tier=MatchTier.STRONG,
            filename="film.mkv",
            mime_type="video/x-matroska",
            download_url="https://data.sdilej.cz/file",
        )
        pipeline._selected[film_id] = selected
        plan.append(
            {
                "film": film.to_dict(),
                "selected": selected.to_dict(),
                "display_name": f"Film {film_id} (2000) 1080p CZ Dabing",
                "needs_czech_subtitles": False,
            }
        )

    used_sources = []

    def fake_relay(
        _target, source, candidate, _name, _description, *, on_prepared
    ):
        used_sources.append(source)
        on_prepared(candidate.source_id, 100)
        return UploadResult(candidate.source_id, 100, 100)

    monkeypatch.setattr("sdilej_to_prehrajto.pipeline.relay_upload", fake_relay)
    monkeypatch.setattr(
        "sdilej_to_prehrajto.pipeline.uploaded_video_id_by_name",
        lambda _session, _name: None,
    )
    pairs = [(f"source-{index}", f"target-{index}") for index in range(6)]
    pipeline.execute(plan, session_pairs=pairs)
    assert set(used_sources) == {f"source-{index}" for index in range(6)}
    assert all(state.uploaded(film_id) for film_id in range(1, 7))


def test_execute_uses_shared_queue_after_initial_worker_rows(
    tmp_path, monkeypatch
) -> None:
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session="source-default",
        target_session="target-default",
        state=StateStore(tmp_path / "state.json"),
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    plan = [{"film": {"cr_film_id": film_id}} for film_id in range(1, 7)]
    release_slow_workers = threading.Event()
    processed: list[tuple[int, str]] = []
    processed_lock = threading.Lock()

    def fake_execute_shard(rows, source, _target, _worker_id):
        film_id = rows[0]["film"]["cr_film_id"]
        with processed_lock:
            processed.append((film_id, source))
        if source != "source-0":
            assert release_slow_workers.wait(1)
        if film_id == 6:
            release_slow_workers.set()

    monkeypatch.setattr(pipeline, "_execute_shard", fake_execute_shard)
    pairs = [(f"source-{index}", f"target-{index}") for index in range(4)]

    pipeline.execute(plan, session_pairs=pairs)

    assert (5, "source-0") in processed
    assert (6, "source-0") in processed


def test_execute_refills_fast_workers_while_slow_tail_is_running(
    tmp_path, monkeypatch
) -> None:
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session="source-default",
        target_session="target-default",
        state=StateStore(tmp_path / "state.json"),
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    initial = [{"film": {"cr_film_id": film_id}} for film_id in (1, 2, 3)]
    replenished = [{"film": {"cr_film_id": film_id}} for film_id in (4, 5)]
    slow_finished = threading.Event()
    refill_seen = threading.Event()
    initial_started = threading.Barrier(2)
    processed: list[tuple[int, str]] = []
    refill_calls = 0
    refilled_while_slow = False

    def fake_execute_shard(rows, source, _target, _worker_id):
        film_id = rows[0]["film"]["cr_film_id"]
        processed.append((film_id, source))
        if film_id in {1, 2}:
            initial_started.wait(1)
        if film_id == 2:
            assert refill_seen.wait(1)
            slow_finished.set()

    def refill_plan():
        nonlocal refill_calls, refilled_while_slow
        refill_calls += 1
        if refill_calls == 1:
            refilled_while_slow = not slow_finished.is_set()
            refill_seen.set()
            return replenished
        return []

    monkeypatch.setattr(pipeline, "_execute_shard", fake_execute_shard)

    pipeline.execute(
        initial,
        session_pairs=[("source-0", "target-0"), ("source-1", "target-1")],
        refill_plan=refill_plan,
        refill_interval_seconds=0.001,
    )

    assert slow_finished.is_set()
    assert refilled_while_slow
    assert {film_id for film_id, _source in processed} == {1, 2, 3, 4, 5}


def test_execute_keeps_all_workers_available_for_continuous_refill(
    tmp_path, monkeypatch
) -> None:
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session="source-default",
        target_session="target-default",
        state=StateStore(tmp_path / "state.json"),
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    initial = [{"film": {"cr_film_id": 1}}]
    replenished = [{"film": {"cr_film_id": film_id}} for film_id in (2, 3)]
    initial_finished = threading.Event()
    refill_seen = threading.Event()
    processed: list[tuple[int, str]] = []
    refill_calls = 0

    def fake_execute_shard(rows, source, _target, _worker_id):
        film_id = rows[0]["film"]["cr_film_id"]
        processed.append((film_id, source))
        if film_id == 1:
            assert refill_seen.wait(1)
            initial_finished.set()

    def refill_plan():
        nonlocal refill_calls
        refill_calls += 1
        if refill_calls == 1:
            assert not initial_finished.is_set()
            refill_seen.set()
            return replenished
        return []

    monkeypatch.setattr(pipeline, "_execute_shard", fake_execute_shard)

    pipeline.execute(
        initial,
        session_pairs=[
            ("source-0", "target-0"),
            ("source-1", "target-1"),
            ("source-2", "target-2"),
        ],
        refill_plan=refill_plan,
        refill_interval_seconds=0.001,
    )

    assert {film_id for film_id, _source in processed} == {1, 2, 3}
    assert refill_calls >= 1


def test_execute_refreshes_fast_url_immediately_with_worker_session(
    tmp_path, monkeypatch
) -> None:
    refreshed_with = []

    class Provider:
        def refresh_approved(self, candidate, *, session):
            refreshed_with.append(session)
            candidate.download_url = "https://data.sdilej.cz/current-fast-url"
            return candidate

    state = StateStore(tmp_path / "state.json")
    pipeline = SyncPipeline(
        source_provider=Provider(),
        source_session="default-source",
        target_session="default-target",
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    film = Film(1, "film", "Film", None, 2000, 90, "en")
    selected = Candidate(
        "1",
        "https://sdilej.cz/1/film.mkv",
        "Film",
        width=1920,
        height=1080,
        language_tier=LanguageTier.CZECH_AUDIO,
        match_tier=MatchTier.STRONG,
        filename="film.mkv",
    )
    pipeline._selected[1] = selected
    plan = [
        {
            "film": film.to_dict(),
            "selected": selected.to_dict(),
            "display_name": "Film (2000) 1080p CZ Dabing",
            "needs_czech_subtitles": False,
        }
    ]

    def fake_relay(_target, _source, candidate, *_args, **kwargs):
        assert candidate.download_url.endswith("current-fast-url")
        kwargs["on_prepared"]("777", 100)
        return UploadResult("777", 100, 100)

    monkeypatch.setattr("sdilej_to_prehrajto.pipeline.relay_upload", fake_relay)
    monkeypatch.setattr(
        "sdilej_to_prehrajto.pipeline.uploaded_video_id_by_name",
        lambda _session, _name: None,
    )
    pipeline.execute(plan, session_pairs=[("worker-source", "worker-target")])
    assert refreshed_with == ["worker-source"]


def test_execute_keeps_existing_processing_video_pending(
    tmp_path, monkeypatch
) -> None:
    state = StateStore(tmp_path / "state.json")
    pipeline = SyncPipeline(
        source_provider=object(),
        source_session="source",
        target_session="target",
        state=state,
        subtitle_queue=SubtitleQueue(tmp_path / "subtitles.jsonl"),
        selected_sources=SelectedSourceStore(tmp_path / "sources.jsonl"),
    )
    film = Film(1, "film", "Film", None, 2000, 90, "en")
    selected = Candidate(
        "source-1",
        "https://sdilej.cz/1/film.mkv",
        "Film",
        size_bytes=123,
        width=1920,
        height=1080,
        language_tier=LanguageTier.CZECH_AUDIO,
        match_tier=MatchTier.STRONG,
        filename="film.mkv",
    )
    pipeline._selected[1] = selected
    plan = [
        {
            "film": film.to_dict(),
            "selected": selected.to_dict(),
            "display_name": "Film (2000) 1080p CZ Dabing",
            "needs_czech_subtitles": False,
        }
    ]
    monkeypatch.setattr(
        "sdilej_to_prehrajto.pipeline.uploaded_video_id_by_name",
        lambda _session, _name: "777",
    )
    monkeypatch.setattr(
        "sdilej_to_prehrajto.pipeline.uploaded_video_confirmed",
        lambda _session, _video_id, _name: False,
    )
    monkeypatch.setattr(
        "sdilej_to_prehrajto.pipeline.uploaded_video_count",
        lambda _session: 2375,
    )
    monkeypatch.setattr(
        "sdilej_to_prehrajto.pipeline.relay_upload",
        lambda *_args, **_kwargs: pytest.fail("processing video must not be reuploaded"),
    )

    pipeline.execute(plan)

    assert state.pending_prepared(1)
    assert not state.uploaded(1)
    assert state.snapshot(1)["attempts"][-1]["status"] == "target_processing"

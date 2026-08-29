from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.pipeline import SyncPipeline, plan_sha
from sdilej_to_prehrajto.prehrajto import UploadResult
from sdilej_to_prehrajto.ranking import SELECTION_POLICY
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
    for film_id in range(1, 5):
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
    pairs = [(f"source-{index}", f"target-{index}") for index in range(4)]
    pipeline.execute(plan, session_pairs=pairs)
    assert set(used_sources) == {f"source-{index}" for index in range(4)}
    assert all(state.uploaded(film_id) for film_id in range(1, 5))


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

from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.pipeline import SyncPipeline, plan_sha
from sdilej_to_prehrajto.prehrajto import UploadResult
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

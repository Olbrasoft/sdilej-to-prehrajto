import json

import pytest

from sdilej_to_prehrajto.models import LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import SELECTION_POLICY
from sdilej_to_prehrajto.sources import SelectedSourceStore


def test_selected_source_manifest_upserts_stable_detail_url(tmp_path) -> None:
    path = tmp_path / "selected-sources.jsonl"
    store = SelectedSourceStore(path)
    store.record(
        {
            "cr_film_id": 1,
            "source_id": "32460472",
            "source_url": "https://sdilej.cz/32460472/angelika.mkv",
            "width": 3840,
            "height": 1632,
        }
    )
    store.record(
        {
            "cr_film_id": 1,
            "source_id": "32460472",
            "source_url": "https://sdilej.cz/32460472/angelika-a-kral.mkv",
            "width": 3840,
            "height": 1632,
        }
    )
    store.compact()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    saved = rows[0]
    assert saved["source_url"].endswith("angelika-a-kral.mkv")


def test_selected_source_manifest_rejects_authenticated_url(tmp_path) -> None:
    store = SelectedSourceStore(tmp_path / "selected-sources.jsonl")
    with pytest.raises(ValueError, match="authenticated"):
        store.record(
            {
                "cr_film_id": 1,
                "source_url": "https://sdilej.cz/1/x",
                "download_url": "https://data.sdilej.cz/x?session=secret",
            }
        )


def test_selected_source_manifest_restores_verified_candidate(tmp_path) -> None:
    store = SelectedSourceStore(tmp_path / "selected-sources.jsonl")
    store.record(
        {
            "cr_film_id": 1,
            "source_id": "32460472",
            "source_url": "https://sdilej.cz/32460472/angelika.mkv",
            "source_filename": "Angelika.mkv",
            "duration_sec": 6000,
            "width": 3840,
            "height": 1632,
            "audio_language": "cs",
            "language_tier": "czech_audio",
            "match_tier": "strong",
            "selection_policy": SELECTION_POLICY,
        }
    )
    candidate = store.candidate(1)
    assert candidate is not None
    assert candidate.url == "https://sdilej.cz/32460472/angelika.mkv"
    assert candidate.language_tier == LanguageTier.CZECH_AUDIO
    assert candidate.match_tier == MatchTier.STRONG


def test_old_selection_policy_is_not_restored_for_upload(tmp_path) -> None:
    store = SelectedSourceStore(tmp_path / "selected-sources.jsonl")
    store.record(
        {
            "cr_film_id": 1,
            "source_id": "old",
            "source_url": "https://sdilej.cz/1/old.mkv",
            "selection_policy": "largest-file-v0",
        }
    )

    assert store.candidate(1) is None


def test_export_results_merges_upload_status_into_one_catalog(tmp_path) -> None:
    source_path = tmp_path / "selected-sources.jsonl"
    state_path = tmp_path / "sync.json"
    output_path = tmp_path / "film-results.jsonl"
    store = SelectedSourceStore(source_path)
    store.record(
        {
            "cr_film_id": 1,
            "source_id": "32460472",
            "source_url": "https://sdilej.cz/32460472/angelika.mkv",
        }
    )
    state_path.write_text(
        json.dumps(
            {
                "films": {
                    "1": {
                        "upload": {
                            "target_video_id": "777",
                            "uploaded_at": "2026-08-28T12:00:00+00:00",
                        }
                    }
                }
            }
        )
    )
    store.export_results(state_path, output_path)
    result = json.loads(output_path.read_text().strip())
    assert result["source_url"].endswith("angelika.mkv")
    assert result["upload_status"] == "success"
    assert result["target_video_id"] == "777"

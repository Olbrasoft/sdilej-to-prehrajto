import json

import pytest

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
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["source_url"].endswith("angelika-a-kral.mkv")


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

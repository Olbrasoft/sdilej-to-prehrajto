from sdilej_to_prehrajto.matching import classify_candidate
from sdilej_to_prehrajto.models import Film, MatchTier


def film() -> Film:
    return Film(
        cr_film_id=1,
        slug="angelika-a-kral",
        title="Angelika a král",
        original_title="Angélique et le Roy",
        year=1966,
        runtime_min=100,
        original_language="fr",
    )


def test_accepts_numbered_release_with_matching_year_and_runtime() -> None:
    result = classify_candidate(
        film(),
        "Angelika 3 Angelika a král (1966) 4K h265 AC3 5.1 CZ.mkv",
        duration_sec=6005,
    )
    assert result.tier == MatchTier.STRONG


def test_rejects_different_angelika_film_by_year() -> None:
    result = classify_candidate(
        film(),
        "Angelika 4 Nezkrotná Angelika (1967) 4K CZ.mkv",
        duration_sec=4934,
    )
    assert result.tier == MatchTier.REJECT
    assert result.reason == "wrong_year"


def test_rejects_wrong_cut_even_when_title_and_year_match() -> None:
    result = classify_candidate(
        film(),
        "Angelika 3 Angelika a král (1966) 4K.mkv",
        duration_sec=3600,
    )
    assert result.tier == MatchTier.REJECT
    assert result.reason == "wrong_runtime"


def test_title_only_match_is_not_uploadable() -> None:
    result = classify_candidate(film(), "Angelika a král.mkv")
    assert result.tier == MatchTier.AMBIGUOUS

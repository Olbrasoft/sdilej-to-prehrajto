from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import display_name, rank_candidates


def candidate(language: LanguageTier, width: int) -> Candidate:
    return Candidate(
        source_id=f"{language}-{width}",
        url="https://sdilej.cz/1/x",
        title="Film",
        width=width,
        height=width * 9 // 16,
        language_tier=language,
        match_tier=MatchTier.STRONG,
    )


def film(original_language: str = "en") -> Film:
    return Film(1, "film", "Film", None, 2000, 100, original_language)


def test_language_outweighs_resolution() -> None:
    czech_720 = candidate(LanguageTier.CZECH_AUDIO, 1280)
    foreign_4k = candidate(LanguageTier.FOREIGN_AUDIO, 3840)
    assert rank_candidates([foreign_4k, czech_720])[0] is czech_720


def test_resolution_breaks_tie_inside_language() -> None:
    czech_720 = candidate(LanguageTier.CZECH_AUDIO, 1280)
    czech_4k = candidate(LanguageTier.CZECH_AUDIO, 3840)
    assert rank_candidates([czech_720, czech_4k])[0] is czech_4k


def test_display_names_include_verified_resolution_and_language() -> None:
    assert display_name(film("cs"), candidate(LanguageTier.CZECH_AUDIO, 3840)) == "Film (2000) 4K"
    assert display_name(film("en"), candidate(LanguageTier.CZECH_AUDIO, 1920)) == "Film (2000) 1080p CZ Dabing"
    assert display_name(film("en"), candidate(LanguageTier.FOREIGN_AUDIO, 1920)) == "Film (2000) 1080p CZ Titulky"

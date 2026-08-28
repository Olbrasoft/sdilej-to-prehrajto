from __future__ import annotations

from .models import Candidate, Film, LanguageTier, MatchTier


CZECH_CODES = {"cs", "cz", "ces", "cze", "czech"}
SLOVAK_CODES = {"sk", "slk", "slo", "slovak"}


def language_tier(language: str | None) -> LanguageTier:
    normalized = (language or "").strip().lower()
    if normalized in CZECH_CODES:
        return LanguageTier.CZECH_AUDIO
    if normalized in SLOVAK_CODES:
        return LanguageTier.SLOVAK_AUDIO
    if normalized:
        return LanguageTier.FOREIGN_AUDIO
    return LanguageTier.UNKNOWN


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    acceptable = [
        candidate
        for candidate in candidates
        if candidate.match_tier in (MatchTier.STRONG, MatchTier.SOLID)
        and candidate.language_tier != LanguageTier.UNKNOWN
        and candidate.width > 0
    ]
    return sorted(
        acceptable,
        key=lambda candidate: (
            int(candidate.language_tier),
            -candidate.resolution_pixels,
            -(candidate.size_bytes or 0),
            candidate.source_id,
        ),
    )


def resolution_label(width: int, height: int = 0) -> str:
    if width >= 3840 or height >= 2160:
        return "4K"
    if width >= 2560 or height >= 1440:
        return "1440p"
    if width >= 1920 or height >= 1080:
        return "1080p"
    if width >= 1280 or height >= 720:
        return "720p"
    return "SD"


def display_name(film: Film, candidate: Candidate) -> str:
    base = f"{film.title} ({film.year})" if film.year else film.title
    base = f"{base} {resolution_label(candidate.width, candidate.height)}"
    original = (film.original_language or "").lower()
    if candidate.language_tier == LanguageTier.CZECH_AUDIO:
        return base if original in CZECH_CODES else f"{base} CZ Dabing"
    if candidate.language_tier == LanguageTier.SLOVAK_AUDIO:
        return f"{base} SK" if original in SLOVAK_CODES else f"{base} SK Dabing"
    return f"{base} CZ Titulky"

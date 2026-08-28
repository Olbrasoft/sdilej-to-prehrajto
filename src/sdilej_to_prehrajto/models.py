from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class LanguageTier(IntEnum):
    CZECH_AUDIO = 1
    SLOVAK_AUDIO = 2
    FOREIGN_AUDIO = 3
    UNKNOWN = 99


class MatchTier(StrEnum):
    STRONG = "strong"
    SOLID = "solid"
    AMBIGUOUS = "ambiguous"
    REJECT = "reject"


@dataclass(frozen=True)
class Film:
    cr_film_id: int
    slug: str
    title: str
    original_title: str | None
    year: int | None
    runtime_min: int | None
    original_language: str | None
    description: str = ""
    priority_rank: int | None = None
    priority_score: float = 0.0

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Film:
        return cls(
            cr_film_id=int(row["cr_film_id"]),
            slug=row.get("cr_slug") or row.get("slug") or str(row["cr_film_id"]),
            title=row["title"],
            original_title=row.get("original_title"),
            year=row.get("year"),
            runtime_min=row.get("runtime_min"),
            original_language=row.get("original_language"),
            description=row.get("description") or "",
            priority_rank=row.get("priority_rank"),
            priority_score=float(row.get("priority_score") or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    source_id: str
    url: str
    title: str
    size_bytes: int | None = None
    duration_sec: int | None = None
    width: int = 0
    height: int = 0
    language_tier: LanguageTier = LanguageTier.UNKNOWN
    audio_language: str | None = None
    language_probability: float | None = None
    language_evidence: str | None = None
    match_tier: MatchTier = MatchTier.REJECT
    match_evidence: dict[str, Any] = field(default_factory=dict)
    query: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    download_url: str | None = None
    sample_url: str | None = None

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Candidate:
        data = dict(row)
        language = data.get("language_tier", "unknown")
        if isinstance(language, str):
            data["language_tier"] = LanguageTier[language.upper()]
        match = data.get("match_tier", "reject")
        if isinstance(match, str):
            data["match_tier"] = MatchTier(match)
        return cls(**data)

    @property
    def resolution_pixels(self) -> int:
        return self.width * self.height

    def to_dict(self, *, sensitive: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["language_tier"] = self.language_tier.name.lower()
        data["match_tier"] = self.match_tier.value
        if not sensitive:
            data.pop("download_url", None)
            data.pop("sample_url", None)
        return data

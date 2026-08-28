from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import Candidate, Film, LanguageTier
from .prehrajto import PrehrajtoError, relay_upload
from .ranking import display_name, rank_candidates
from .state import StateStore, now_iso
from .subtitles import SubtitleQueue
from .sources import SelectedSourceStore


MAX_PILOT_FILMS = 10


def plan_sha(plan: list[dict]) -> str:
    approval_rows = [
        {
            "cr_film_id": row["film"]["cr_film_id"],
            "source_id": row["selected"]["source_id"],
            "width": row["selected"]["width"],
            "height": row["selected"]["height"],
            "audio_language": row["selected"]["audio_language"],
            "language_tier": row["selected"]["language_tier"],
            "display_name": row["display_name"],
        }
        for row in plan
    ]
    payload = json.dumps(
        approval_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SyncPipeline:
    def __init__(
        self,
        *,
        source_provider,
        source_session,
        target_session,
        state: StateStore,
        subtitle_queue: SubtitleQueue,
        selected_sources: SelectedSourceStore,
    ):
        self.source_provider = source_provider
        self.source_session = source_session
        self.target_session = target_session
        self.state = state
        self.subtitle_queue = subtitle_queue
        self.selected_sources = selected_sources
        self._selected: dict[int, Candidate] = {}

    def build_plan(
        self, films: list[Film], limit: int, *, max_scan: int | None = None
    ) -> list[dict]:
        if limit < 1:
            raise ValueError("Limit must be positive")
        plan: list[dict] = []
        inspected = 0
        for film in films:
            if self.state.uploaded(film.cr_film_id):
                continue
            if self.state.pending_prepared(film.cr_film_id):
                continue
            if self.state.deferred(film.cr_film_id):
                continue
            if max_scan is not None and inspected >= max_scan:
                break
            inspected += 1
            discovered = self.source_provider.discover(film)
            ranked = rank_candidates(discovered)
            if not ranked:
                self.state.record_attempt(
                    film.cr_film_id,
                    {
                        "status": "no_acceptable_source",
                        "permanent": False,
                        "reason": "No identity-, language-, and quality-verified source",
                    },
                )
                continue
            selected = ranked[0]
            self._selected[film.cr_film_id] = selected
            row = {
                "film": film.to_dict(),
                "selected": selected.to_dict(),
                "display_name": display_name(film, selected),
                "needs_czech_subtitles": selected.language_tier
                == LanguageTier.FOREIGN_AUDIO,
            }
            plan.append(row)
            self.state.record_plan(film.cr_film_id, row)
            if len(plan) >= limit:
                break
        return plan

    def execute(self, plan: list[dict]) -> None:
        for row in plan:
            film = Film.from_dict(row["film"])
            if self.state.uploaded(film.cr_film_id):
                continue
            candidate = self._selected[film.cr_film_id]
            try:
                result = relay_upload(
                    self.target_session,
                    self.source_session,
                    candidate,
                    row["display_name"],
                    film.description,
                    on_prepared=lambda video_id, size, film_id=film.cr_film_id: (
                        self.state.record_prepared(film_id, video_id, size)
                    ),
                )
            except Exception as error:
                target_video_id = (
                    error.target_video_id if isinstance(error, PrehrajtoError) else None
                )
                self.state.record_attempt(
                    film.cr_film_id,
                    {
                        "status": "upload_failed",
                        "source_id": candidate.source_id,
                        "reason": type(error).__name__,
                        "permanent": False,
                        "target_video_id": target_video_id,
                    },
                )
                continue

            upload = {
                "target_video_id": result.video_id,
                "display_name": row["display_name"],
                "source_id": candidate.source_id,
                "source_url": candidate.url,
                "source_filename": candidate.filename,
                "size_bytes": result.size_bytes,
                "width": candidate.width,
                "height": candidate.height,
                "audio_language": candidate.audio_language,
                "language_probability": candidate.language_probability,
                "language_tier": candidate.language_tier.name.lower(),
                "needs_czech_subtitles": row["needs_czech_subtitles"],
            }
            self.selected_sources.record(
                {
                    "cr_film_id": film.cr_film_id,
                    "source_id": candidate.source_id,
                    "source_url": candidate.url,
                    "source_filename": candidate.filename,
                    "width": candidate.width,
                    "height": candidate.height,
                    "audio_language": candidate.audio_language,
                    "language_probability": candidate.language_probability,
                    "language_tier": candidate.language_tier.name.lower(),
                    "match_tier": candidate.match_tier.value,
                    "display_name": row["display_name"],
                }
            )
            if row["needs_czech_subtitles"]:
                self.subtitle_queue.enqueue(
                    {
                        "cr_film_id": film.cr_film_id,
                        "target_video_id": result.video_id,
                        "display_name": row["display_name"],
                        "status": "pending",
                        "queued_at": now_iso(),
                    }
                )
            self.state.record_success(film.cr_film_id, upload)


def write_plan(path: Path, plan: list[dict]) -> str:
    digest = plan_sha(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"plan_sha": digest, "films": plan}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return digest


def write_report(path: Path, plan: list[dict], digest: str) -> None:
    lines = ["# Upload plan", "", f"Plan SHA-256: `{digest}`", ""]
    for index, row in enumerate(plan, start=1):
        film = row["film"]
        selected = row["selected"]
        lines.extend(
            [
                f"## {index}. {row['display_name']}",
                "",
                f"- CR film ID: {film['cr_film_id']}",
                f"- Source ID: {selected['source_id']}",
                f"- Match: {selected['match_tier']}",
                f"- Runtime: {selected.get('duration_sec')}",
                f"- Resolution: {selected.get('width')}x{selected.get('height')}",
                f"- Audio: {selected.get('audio_language')} ({selected.get('language_probability')})",
                f"- Czech subtitle follow-up: {row['needs_czech_subtitles']}",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .models import Candidate, Film, LanguageTier
from .prehrajto import (
    PrehrajtoError,
    relay_upload,
    rename_video,
    uploaded_video_confirmed,
    uploaded_video_count,
    uploaded_video_id_by_name,
)
from .ranking import (
    SELECTION_POLICY,
    display_name,
    minimum_bitrate_mbps,
    rank_candidates,
)
from .sdilej import DeepScanRequired, PremiumRequiredError, SdilejError
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
        self._completion_lock = threading.RLock()

    def _record_verified_source(
        self, film: Film, candidate: Candidate, selected_name: str
    ) -> None:
        existing = self.selected_sources.get(film.cr_film_id) or {}
        self.selected_sources.record(
            {
                "cr_film_id": film.cr_film_id,
                "source_id": candidate.source_id,
                "source_url": candidate.url,
                "source_title": candidate.title,
                "source_filename": candidate.filename,
                "size_bytes": candidate.size_bytes,
                "duration_sec": candidate.duration_sec,
                "width": candidate.width,
                "height": candidate.height,
                "audio_language": candidate.audio_language,
                "language_probability": candidate.language_probability,
                "language_evidence": candidate.language_evidence,
                "language_tier": candidate.language_tier.name.lower(),
                "match_tier": candidate.match_tier.value,
                "match_evidence": candidate.match_evidence,
                "query": candidate.query,
                "mime_type": candidate.mime_type,
                "video_codec": candidate.video_codec,
                "average_bitrate_mbps": candidate.average_bitrate_mbps,
                "minimum_bitrate_mbps": minimum_bitrate_mbps(candidate),
                "selection_policy": SELECTION_POLICY,
                "display_name": selected_name,
                "source_status": "verified",
                "upload_status": existing.get("upload_status", "pending"),
            }
        )

    def load_approved_plan(self, plan: list[dict]) -> None:
        """Load reviewed stable sources and refresh only their expiring URLs."""
        for row in plan:
            film = Film.from_dict(row["film"])
            if self.state.uploaded(film.cr_film_id):
                continue
            candidate = Candidate.from_dict(row["selected"])
            self._selected[film.cr_film_id] = candidate
            self.state.record_plan(film.cr_film_id, row)

    def build_plan(
        self,
        films: list[Film],
        limit: int,
        *,
        max_scan: int | None = None,
        verified_only: bool = False,
    ) -> list[dict]:
        if limit < 1:
            raise ValueError("Limit must be positive")
        plan: list[dict] = []
        inspected = 0
        for film in films:
            if self.state.uploaded(film.cr_film_id) or self.selected_sources.uploaded(
                film.cr_film_id
            ):
                continue
            # A killed runner can leave a durable lease behind. Do not spend a
            # plan slot on it only to have execute() reject it; keep scanning so
            # all upload shards receive runnable work from farther in the queue.
            if self.state.actively_claimed(film.cr_film_id):
                continue
            cached = self.selected_sources.candidate(film.cr_film_id)
            if self.state.deferred(
                film.cr_film_id,
                source_id=cached.source_id if cached is not None else None,
            ):
                continue
            # In queue-draining mode, films without a prepared source are not
            # inspected candidates. Counting them against max_scan can starve
            # verified sources lower in the prioritized backlog forever.
            if verified_only and cached is None:
                continue
            if max_scan is not None and inspected >= max_scan:
                break
            inspected += 1
            selected = None
            source_was_discovered = False
            if cached is not None:
                if verified_only:
                    selected = cached
                else:
                    try:
                        selected = self.source_provider.refresh_approved(cached)
                    except Exception:
                        selected = None
            ranked = []
            if selected is None:
                discovered = self.source_provider.discover(film)
                ranked = rank_candidates(discovered)
            if not ranked:
                if selected is None:
                    self.state.record_attempt(
                        film.cr_film_id,
                        {
                            "status": "no_acceptable_source",
                            "permanent": False,
                            "discovery_complete": True,
                            "selection_policy": SELECTION_POLICY,
                            "reason": "No identity-, language-, and quality-verified source",
                        },
                    )
                    continue
            else:
                selected = ranked[0]
                source_was_discovered = True
            assert selected is not None
            self._selected[film.cr_film_id] = selected
            row = {
                "film": film.to_dict(),
                "selected": selected.to_dict(),
                "display_name": display_name(film, selected),
                "needs_czech_subtitles": selected.language_tier
                == LanguageTier.FOREIGN_AUDIO,
            }
            if source_was_discovered:
                self._record_verified_source(film, selected, row["display_name"])
                self.state.persist_external("source")
            plan.append(row)
            self.state.record_plan(film.cr_film_id, row)
            if len(plan) >= limit:
                break
        return plan

    def prepare_sources(
        self,
        films: list[Film],
        limit: int,
        *,
        max_scan: int | None = None,
        deadline_monotonic: float | None = None,
        deep_scan_only: bool = False,
    ) -> list[dict]:
        """Continuously fill the verified-source manifest without uploading."""
        prepared: list[dict] = []
        inspected = 0
        for film in films:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                break
            if self.state.uploaded(film.cr_film_id) or self.selected_sources.uploaded(
                film.cr_film_id
            ):
                continue
            current_source = self.selected_sources.candidate(film.cr_film_id)
            if current_source is not None:
                continue
            if deep_scan_only:
                if not self.state.deep_scan_ready(
                    film.cr_film_id, selection_policy=SELECTION_POLICY
                ):
                    continue
            elif self.state.deferred(
                film.cr_film_id, selection_policy=SELECTION_POLICY
            ):
                continue
            if max_scan is not None and inspected >= max_scan:
                break
            inspected += 1
            if deep_scan_only:
                print(
                    f"deep_scan_started cr_film_id={film.cr_film_id}",
                    flush=True,
                )
            try:
                discovered = self.source_provider.discover(film)
            except PremiumRequiredError:
                # Missing fast-download entitlement affects every source.
                # Fail the workflow visibly instead of scanning the backlog.
                raise
            except DeepScanRequired as error:
                self.state.record_attempt(
                    film.cr_film_id,
                    {
                        "status": "source_deep_scan_needed",
                        "permanent": True,
                        "selection_policy": SELECTION_POLICY,
                        "reason": str(error),
                    },
                )
                continue
            except SdilejError as error:
                # A long-running preparation job must not lose hours of useful
                # work because one search request was disconnected. Record a
                # short, policy-specific defer and continue with the backlog.
                self.state.record_attempt(
                    film.cr_film_id,
                    {
                        "status": (
                            "source_deep_scan_failed"
                            if deep_scan_only
                            else "source_discovery_failed"
                        ),
                        "permanent": error.permanent,
                        "selection_policy": SELECTION_POLICY,
                        # SdilejError redacts authenticated query values. Keep
                        # the safe message so persistent failures can be
                        # diagnosed instead of all appearing as "SdilejError".
                        "reason": str(error),
                    },
                )
                if deep_scan_only:
                    print(
                        f"deep_scan_failed cr_film_id={film.cr_film_id} "
                        f"reason={type(error).__name__}",
                        flush=True,
                    )
                continue
            ranked = rank_candidates(discovered)
            if not ranked:
                self.state.record_attempt(
                    film.cr_film_id,
                    {
                        "status": "no_acceptable_source",
                        "permanent": False,
                        "discovery_complete": True,
                        "search_response_validated": True,
                        "selection_policy": SELECTION_POLICY,
                        "reason": "No identity-, language-, and quality-verified source",
                    },
                )
                continue
            selected = ranked[0]
            row = {
                "film": film.to_dict(),
                "selected": selected.to_dict(),
                "display_name": display_name(film, selected),
                "needs_czech_subtitles": selected.language_tier
                == LanguageTier.FOREIGN_AUDIO,
            }
            self._record_verified_source(film, selected, row["display_name"])
            self.state.persist_external("source")
            prepared.append(row)
            if len(prepared) >= limit:
                break
        return prepared

    def _upload_record(
        self,
        row: dict,
        candidate: Candidate,
        *,
        video_id: str,
        size_bytes: int,
        completion_evidence: str,
    ) -> dict:
        return {
            "target_video_id": video_id,
            "display_name": row["display_name"],
            "source_id": candidate.source_id,
            "source_url": candidate.url,
            "source_filename": candidate.filename,
            "size_bytes": size_bytes,
            "width": candidate.width,
            "height": candidate.height,
            "audio_language": candidate.audio_language,
            "language_probability": candidate.language_probability,
            "language_tier": candidate.language_tier.name.lower(),
            "needs_czech_subtitles": row["needs_czech_subtitles"],
            "completion_evidence": completion_evidence,
        }

    @staticmethod
    def _target_confirmed(target_session, video_id: str, name: str) -> bool:
        try:
            return uploaded_video_count(target_session) is not None and (
                uploaded_video_confirmed(target_session, video_id, name)
            )
        except Exception:
            return False

    @classmethod
    def _target_completed_and_named(
        cls, target_session, video_id: str, name: str
    ) -> bool:
        """Retry the final rename once transcoding no longer blocks it."""
        if cls._target_confirmed(target_session, video_id, name):
            return True
        try:
            rename_video(target_session, video_id, name)
        except Exception:
            return False
        return cls._target_confirmed(target_session, video_id, name)

    @staticmethod
    def _target_id_by_name(target_session, name: str) -> str | None:
        try:
            video_id = uploaded_video_id_by_name(target_session, name)
            if video_id and uploaded_video_count(target_session) is not None:
                return video_id
        except Exception:
            pass
        return None

    def _finish_success(
        self, film: Film, candidate: Candidate, row: dict, upload: dict
    ) -> None:
        with self._completion_lock:
            if row["needs_czech_subtitles"]:
                self.subtitle_queue.enqueue(
                    {
                        "cr_film_id": film.cr_film_id,
                        "target_video_id": upload["target_video_id"],
                        "display_name": row["display_name"],
                        "status": "pending",
                        "queued_at": now_iso(),
                    }
                )
            self.state.record_success(film.cr_film_id, upload)

    def _execute_shard(
        self,
        rows: list[dict],
        source_session,
        target_session,
        worker_id: str,
    ) -> None:
        for row in rows:
            film = Film.from_dict(row["film"])
            if self.state.uploaded(film.cr_film_id) or self.selected_sources.uploaded(
                film.cr_film_id
            ):
                continue
            if not self.state.claim_upload(film.cr_film_id, worker_id):
                print(f"upload_skipped=claimed cr_film_id={film.cr_film_id}", flush=True)
                continue
            candidate = self._selected[film.cr_film_id]
            existing_video_id = self._target_id_by_name(
                target_session, row["display_name"]
            )
            if existing_video_id:
                if self._target_completed_and_named(
                    target_session, existing_video_id, row["display_name"]
                ):
                    upload = self._upload_record(
                        row,
                        candidate,
                        video_id=existing_video_id,
                        size_bytes=int(candidate.size_bytes or 0),
                        completion_evidence="reconciled_exact_uploaded_name",
                    )
                    self._finish_success(film, candidate, row, upload)
                else:
                    self.state.record_target_processing(
                        film.cr_film_id,
                        video_id=existing_video_id,
                        size=int(candidate.size_bytes or 0),
                        source_id=candidate.source_id,
                    )
                continue
            checkpoint = self.state.snapshot(film.cr_film_id)
            prepared = checkpoint.get("prepared")
            if prepared:
                video_id = str(prepared["target_video_id"])
                if self._target_completed_and_named(
                    target_session, video_id, row["display_name"]
                ):
                    upload = self._upload_record(
                        row,
                        candidate,
                        video_id=video_id,
                        size_bytes=int(prepared["size_bytes"]),
                        completion_evidence="reconciled_statistics_and_uploaded_listing",
                    )
                    self._finish_success(film, candidate, row, upload)
                elif self._target_id_by_name(
                    target_session, row["display_name"]
                ) == video_id:
                    self.state.record_target_processing(
                        film.cr_film_id,
                        video_id=video_id,
                        size=int(prepared["size_bytes"]),
                        source_id=candidate.source_id,
                    )
                else:
                    self.state.record_upload_failure(
                        film.cr_film_id,
                        {
                            "status": "stale_prepared_released",
                            "source_id": candidate.source_id,
                            "target_video_id": video_id,
                            "reason": "Prepared video is absent from uploaded listing",
                            "permanent": False,
                        },
                    )
                continue
            refresh = getattr(self.source_provider, "refresh_approved", None)
            if refresh:
                try:
                    refreshed = refresh(candidate, session=source_session)
                except PremiumRequiredError as error:
                    self.state.record_upload_failure(
                        film.cr_film_id,
                        {
                            "status": "source_premium_required",
                            "source_id": candidate.source_id,
                            "reason": str(error),
                            "permanent": False,
                        },
                    )
                    raise
                except Exception as error:
                    self.state.record_upload_failure(
                        film.cr_film_id,
                        {
                            "status": "source_refresh_failed",
                            "source_id": candidate.source_id,
                            "reason": (
                                str(error)
                                if isinstance(error, SdilejError)
                                else type(error).__name__
                            ),
                            "permanent": False,
                        },
                    )
                    continue
                if (
                    refreshed.source_id != candidate.source_id
                    or refreshed.url != candidate.url
                ):
                    self.state.record_upload_failure(
                        film.cr_film_id,
                        {
                            "status": "source_identity_changed",
                            "source_id": candidate.source_id,
                            "permanent": True,
                        },
                    )
                    continue
                candidate = refreshed
            try:
                result = relay_upload(
                    target_session,
                    source_session,
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
                prepared = self.state.snapshot(film.cr_film_id).get("prepared") or {}
                target_video_id = target_video_id or prepared.get("target_video_id")
                if target_video_id and self._target_completed_and_named(
                    target_session, str(target_video_id), row["display_name"]
                ):
                    upload = self._upload_record(
                        row,
                        candidate,
                        video_id=str(target_video_id),
                        size_bytes=int(prepared.get("size_bytes") or candidate.size_bytes or 0),
                        completion_evidence="reconciled_after_relay_error",
                    )
                    self._finish_success(film, candidate, row, upload)
                elif target_video_id and self._target_id_by_name(
                    target_session, row["display_name"]
                ) == str(target_video_id):
                    self.state.record_target_processing(
                        film.cr_film_id,
                        video_id=str(target_video_id),
                        size=int(prepared.get("size_bytes") or candidate.size_bytes or 0),
                        source_id=candidate.source_id,
                    )
                else:
                    self.state.record_upload_failure(
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

            upload = self._upload_record(
                row,
                candidate,
                video_id=result.video_id,
                size_bytes=result.size_bytes,
                completion_evidence="relay_completed",
            )
            if result.completed:
                self._finish_success(film, candidate, row, upload)
            else:
                self.state.record_target_processing(
                    film.cr_film_id,
                    video_id=result.video_id,
                    size=result.size_bytes,
                    source_id=candidate.source_id,
                )

    def execute(
        self,
        plan: list[dict],
        *,
        session_pairs: list[tuple[object, object]] | None = None,
        refill_plan: Callable[[], list[dict]] | None = None,
        refill_interval_seconds: float = 15,
    ) -> None:
        pairs = session_pairs or [(self.source_session, self.target_session)]
        execution_id = uuid.uuid4().hex
        # A continuous run may start with only one verified source while the
        # producer is still filling the queue. Keep every configured worker
        # available so newly verified sources can be consumed immediately
        # instead of leaving the uploader single-threaded for the whole batch.
        worker_count = (
            len(pairs) if refill_plan is not None else min(len(pairs), len(plan))
        )
        if worker_count == 0:
            return
        pending = deque(plan)
        queue_condition = threading.Condition()
        in_flight = 0
        refilling = False

        def take_next_row() -> dict | None:
            nonlocal in_flight, refilling
            while True:
                refill_leader = False
                with queue_condition:
                    if pending:
                        in_flight += 1
                        return pending.popleft()
                    if refill_plan is None or in_flight == 0:
                        return None
                    if not refilling:
                        refilling = True
                        refill_leader = True
                    else:
                        queue_condition.wait()
                        continue
                if refill_leader:
                    refill_rows: list[dict] = []
                    try:
                        refill_rows = refill_plan()
                    except Exception as error:
                        print(
                            f"queue_refill_failed={type(error).__name__}",
                            flush=True,
                        )
                    if refill_rows:
                        with queue_condition:
                            pending.extend(refill_rows)
                            refilling = False
                            queue_condition.notify_all()
                        print(f"queue_refilled={len(refill_rows)}", flush=True)
                    else:
                        time.sleep(refill_interval_seconds)
                        with queue_condition:
                            refilling = False
                            queue_condition.notify_all()

        def execute_worker(
            source_session,
            target_session,
            worker_id: str,
        ) -> None:
            nonlocal in_flight
            while True:
                row = take_next_row()
                if row is None:
                    return
                try:
                    self._execute_shard(
                        [row], source_session, target_session, worker_id
                    )
                finally:
                    with queue_condition:
                        in_flight -= 1
                        queue_condition.notify_all()

        # Workers that empty the current snapshot keep polling the producer and
        # refill the shared queue while any slow transfer is still in flight.
        # This prevents one large tail item from idling the other five workers.
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    execute_worker,
                    source_session,
                    target_session,
                    f"{execution_id}-shard-{index}",
                )
                for index, (source_session, target_session) in enumerate(
                    pairs[:worker_count]
                )
            ]
            for future in futures:
                future.result()


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

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(
        self,
        path: Path,
        *,
        on_persist: Callable[[Path, str], None] | None = None,
    ):
        self.path = path
        self.on_persist = on_persist
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "films": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported state schema version")
        data.setdefault("films", {})
        return data

    def save(self, event: str) -> None:
        with self._lock:
            self.data["updated_at"] = now_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=self.path.name, dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            if self.on_persist:
                self.on_persist(self.path, event)

    def film(self, film_id: int) -> dict[str, Any]:
        return self.data["films"].setdefault(str(film_id), {"attempts": []})

    def tracked_films(self) -> int:
        with self._lock:
            return len(self.data["films"])

    def uploaded(self, film_id: int) -> bool:
        with self._lock:
            return bool(self.film(film_id).get("upload", {}).get("target_video_id"))

    def pending_prepared(self, film_id: int) -> bool:
        with self._lock:
            row = self.film(film_id)
            return bool(row.get("prepared") and not row.get("upload"))

    def actively_claimed(
        self,
        film_id: int,
        *,
        at: datetime | None = None,
    ) -> bool:
        """Return whether another upload attempt still owns a live lease."""
        with self._lock:
            claim = self.film(film_id).get("claim")
            if not claim or not claim.get("lease_expires_at"):
                return False
            expires_at = datetime.fromisoformat(claim["lease_expires_at"])
            return expires_at > (at or datetime.now(UTC))

    def snapshot(self, film_id: int) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self.film(film_id))

    def claim_upload(
        self,
        film_id: int,
        worker_id: str,
        *,
        lease: timedelta = timedelta(hours=6),
        at: datetime | None = None,
    ) -> bool:
        with self._lock:
            now = at or datetime.now(UTC)
            row = self.film(film_id)
            if row.get("upload", {}).get("target_video_id"):
                return False
            existing = row.get("claim")
            if existing:
                expires_at = datetime.fromisoformat(existing["lease_expires_at"])
                if expires_at > now:
                    return False
            row["claim"] = {
                "worker_id": worker_id,
                "claimed_at": now.isoformat(),
                "lease_expires_at": (now + lease).isoformat(),
            }
            self.save("claim")
            return True

    def deferred(
        self,
        film_id: int,
        *,
        source_id: str | None = None,
        selection_policy: str | None = None,
        at: datetime | None = None,
    ) -> bool:
        with self._lock:
            attempts = self.film(film_id).get("attempts", [])
            if not attempts:
                return False
            latest = attempts[-1]
            if latest.get("status") == "target_requires_review":
                return True
            if source_id is not None and latest.get("source_id") != source_id:
                return False
            if (
                selection_policy is not None
                and latest.get("selection_policy") != selection_policy
            ):
                return False
            if latest.get("permanent"):
                return True
            timestamp = latest.get("attempted_at")
            if not timestamp:
                return False
            attempted_at = datetime.fromisoformat(timestamp)
            cooldown = (
                timedelta(days=30)
                if latest.get("status") == "no_acceptable_source"
                else timedelta(hours=6)
            )
            return (at or datetime.now(UTC)) < attempted_at + cooldown

    def record_plan(self, film_id: int, plan: dict) -> None:
        with self._lock:
            self.film(film_id)["last_plan"] = {
                "source_id": plan["selected"]["source_id"],
                "display_name": plan["display_name"],
                "planned_at": now_iso(),
            }
            self.save("plan")

    def persist_external(self, event: str) -> None:
        with self._lock:
            if self.on_persist:
                self.on_persist(self.path, event)

    def record_prepared(self, film_id: int, video_id: str, size: int) -> None:
        with self._lock:
            self.film(film_id)["prepared"] = {
                "target_video_id": video_id,
                "size_bytes": size,
                "prepared_at": now_iso(),
            }
            self.save("prepared")

    def record_attempt(self, film_id: int, attempt: dict) -> None:
        with self._lock:
            self.film(film_id).setdefault("attempts", []).append(
                {**attempt, "attempted_at": now_iso()}
            )
            self.film(film_id)["attempts"] = self.film(film_id)["attempts"][-3:]
            self.save("attempt")

    def record_upload_failure(self, film_id: int, attempt: dict) -> None:
        with self._lock:
            row = self.film(film_id)
            row.setdefault("attempts", []).append(
                {**attempt, "attempted_at": now_iso()}
            )
            row["attempts"] = row["attempts"][-3:]
            row.pop("prepared", None)
            row.pop("claim", None)
            self.save("failure")

    def record_success(self, film_id: int, upload: dict) -> None:
        with self._lock:
            row = self.film(film_id)
            row["upload"] = {
                "target_video_id": upload["target_video_id"],
                "uploaded_at": now_iso(),
            }
            row.pop("last_plan", None)
            row.pop("prepared", None)
            row.pop("claim", None)
            self.save("success")

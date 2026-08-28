from __future__ import annotations

import json
import os
import tempfile
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
        self.data["updated_at"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
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

    def uploaded(self, film_id: int) -> bool:
        return bool(self.film(film_id).get("upload", {}).get("target_video_id"))

    def pending_prepared(self, film_id: int) -> bool:
        row = self.film(film_id)
        return bool(row.get("prepared") and not row.get("upload"))

    def deferred(self, film_id: int, *, at: datetime | None = None) -> bool:
        attempts = self.film(film_id).get("attempts", [])
        if not attempts:
            return False
        latest = attempts[-1]
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
        self.film(film_id)["last_plan"] = {**plan, "planned_at": now_iso()}
        self.save("plan")

    def record_prepared(self, film_id: int, video_id: str, size: int) -> None:
        self.film(film_id)["prepared"] = {
            "target_video_id": video_id,
            "size_bytes": size,
            "prepared_at": now_iso(),
        }
        self.save("prepared")

    def record_attempt(self, film_id: int, attempt: dict) -> None:
        self.film(film_id).setdefault("attempts", []).append(
            {**attempt, "attempted_at": now_iso()}
        )
        self.save("attempt")

    def record_success(self, film_id: int, upload: dict) -> None:
        row = self.film(film_id)
        row["upload"] = {**upload, "uploaded_at": now_iso()}
        row.pop("prepared", None)
        self.save("success")

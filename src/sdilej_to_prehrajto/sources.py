from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .models import Candidate
from .ranking import SELECTION_POLICY
from .state import now_iso


class SelectedSourceStore:
    """Reusable stable Sdilej detail URLs; never stores session download URLs."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._rows = self._load()

    def _load(self) -> dict[int, dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        if self.path.exists():
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        row = json.loads(line)
                        rows[int(row["cr_film_id"])] = row
        return rows

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def merge_jsonl(self, payload: str) -> int:
        """Merge a producer snapshot into memory without rewriting its manifest."""
        incoming: dict[int, dict[str, Any]] = {}
        for line in payload.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if "download_url" in row or "sample_url" in row:
                raise ValueError("Authenticated URLs must not be loaded from manifest")
            incoming[int(row["cr_film_id"])] = row
        with self._lock:
            changed = sum(self._rows.get(film_id) != row for film_id, row in incoming.items())
            self._rows.update(incoming)
            return changed

    def record(self, row: dict[str, Any]) -> None:
        with self._lock:
            if "download_url" in row or "sample_url" in row:
                raise ValueError("Ephemeral authenticated URLs must not be persisted")
            film_id = int(row["cr_film_id"])
            existing = self._rows.get(film_id, {})
            record = {
                **existing,
                **row,
                "cr_film_id": film_id,
                "verified_at": existing.get("verified_at", now_iso()),
                "updated_at": now_iso(),
            }
            self._rows[film_id] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def compact(self) -> None:
        """Atomically keep only the latest result row for every film."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for film_id in sorted(self._rows):
                        handle.write(
                            json.dumps(self._rows[film_id], ensure_ascii=False) + "\n"
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    def get(self, film_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get(int(film_id))
            return dict(row) if row else None

    def uploaded(self, film_id: int) -> bool:
        row = self.get(film_id)
        return bool(row and row.get("upload_status") == "success")

    def export_results(self, state_path: Path, output_path: Path) -> None:
        """Create the single final source-and-upload catalog."""
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {"films": {}}
        )
        results: list[dict[str, Any]] = []
        with self._lock:
            for film_id in sorted(self._rows):
                result = dict(self._rows[film_id])
                transfer = state.get("films", {}).get(str(film_id), {})
                upload = transfer.get("upload") or {}
                result["upload_status"] = (
                    "success" if upload.get("target_video_id") else "pending"
                )
                if upload.get("target_video_id"):
                    result["target_video_id"] = str(upload["target_video_id"])
                    result["uploaded_at"] = upload.get("uploaded_at")
                attempts = transfer.get("attempts") or []
                if attempts and result["upload_status"] != "success":
                    result["last_upload_error"] = attempts[-1]
                results.append(result)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", dir=output_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for result in results:
                    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, output_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def candidate(self, film_id: int) -> Candidate | None:
        row = self.get(film_id)
        if not row or row.get("selection_policy") != SELECTION_POLICY:
            return None
        return Candidate.from_dict(
            {
                "source_id": row["source_id"],
                "url": row["source_url"],
                "title": row.get("source_title")
                or row.get("source_filename")
                or row["source_url"],
                "size_bytes": row.get("size_bytes"),
                "duration_sec": row.get("duration_sec"),
                "width": row.get("width", 0),
                "height": row.get("height", 0),
                "language_tier": row.get("language_tier", "unknown"),
                "audio_language": row.get("audio_language"),
                "language_probability": row.get("language_probability"),
                "language_evidence": row.get("language_evidence"),
                "match_tier": row.get("match_tier", "reject"),
                "match_evidence": row.get("match_evidence", {}),
                "query": row.get("query"),
                "filename": row.get("source_filename"),
                "mime_type": row.get("mime_type"),
                "video_codec": row.get("video_codec"),
            }
        )

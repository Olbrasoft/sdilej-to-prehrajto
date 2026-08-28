from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import Candidate
from .state import now_iso


class SelectedSourceStore:
    """Reusable stable Sdilej detail URLs; never stores session download URLs."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[int, dict[str, Any]]:
        if not self.path.exists():
            return {}
        rows: dict[int, dict[str, Any]] = {}
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    rows[int(row["cr_film_id"])] = row
        return rows

    def record(self, row: dict[str, Any]) -> None:
        if "download_url" in row or "sample_url" in row:
            raise ValueError("Ephemeral authenticated URLs must not be persisted")
        rows = self._load()
        film_id = int(row["cr_film_id"])
        rows[film_id] = {**row, "cr_film_id": film_id, "verified_at": now_iso()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for key in sorted(rows):
                    handle.write(json.dumps(rows[key], ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def get(self, film_id: int) -> dict[str, Any] | None:
        return self._load().get(int(film_id))

    def candidate(self, film_id: int) -> Candidate | None:
        row = self.get(film_id)
        if not row:
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
            }
        )

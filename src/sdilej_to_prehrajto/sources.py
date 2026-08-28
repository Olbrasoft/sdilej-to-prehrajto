from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

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

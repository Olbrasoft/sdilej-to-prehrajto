from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


class SubtitleQueue:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _rows(self) -> dict[int, dict]:
        if not self.path.exists():
            return {}
        rows: dict[int, dict] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[int(row["cr_film_id"])] = row
        return rows

    def enqueue(self, row: dict) -> None:
        with self._lock:
            rows = self._rows()
            rows[int(row["cr_film_id"])] = row
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=self.path.name, dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for item in sorted(
                        rows.values(), key=lambda value: int(value["cr_film_id"])
                    ):
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

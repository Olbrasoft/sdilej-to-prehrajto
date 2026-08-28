from __future__ import annotations

import gzip
import json
from pathlib import Path

from .models import Film


def load_backlog(path: Path) -> list[Film]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [Film.from_dict(json.loads(line)) for line in handle if line.strip()]

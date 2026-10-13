import json
import math
import os
from pathlib import Path
import tempfile
import time


FULL_SCAN_INTERVAL = 7 * 86400


class ScanState:
    def __init__(self, path: str | Path, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time

    def should_full_scan(self, known_ids: set[int], requested: bool = False) -> bool:
        if requested or not known_ids:
            return True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            previous = data["last_full_scan"]
            if data.get("version") != 1 or type(previous) not in (int, float) or not math.isfinite(previous):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            return True
        elapsed = self.clock() - previous
        return elapsed < 0 or elapsed >= FULL_SCAN_INTERVAL

    def mark_complete(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".scan-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump({"version": 1, "last_full_scan": self.clock()}, output)
                output.write("\n")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

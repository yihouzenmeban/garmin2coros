from collections import Counter
from collections.abc import Callable
import os
import sys
import time
from typing import TextIO


PROGRESS_PREFIX = "同步进度："


class SyncProgress:
    def __init__(
        self, total: int, stream: TextIO | None = None,
        interactive: bool | None = None, clock: Callable[[], float] | None = None,
    ):
        if total < 0:
            raise ValueError("Progress total cannot be negative.")
        self.total = total
        self.completed = 0
        self.succeeded = 0
        self.failed = 0
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = interactive if interactive is not None else (
            os.environ.get("SYNC_PROGRESS_TTY") == "1" or self.stream.isatty()
        )
        self.clock = clock or time.monotonic
        self._render(self.clock())

    @property
    def percent(self) -> int:
        return self.completed * 100 // self.total if self.total else 100

    def _render(self, now: float) -> None:
        filled = self.percent // 5
        bar = "#" * filled + "-" * (20 - filled)
        print(
            f"{PROGRESS_PREFIX}[{bar}] {self.completed}/{self.total} ({self.percent}%) "
            f"成功 {self.succeeded} 失败 {self.failed}",
            file=self.stream, flush=True,
        )
        self._last_bucket = filled
        self._last_update = now

    def advance(self, succeeded: bool) -> None:
        if self.completed >= self.total:
            raise ValueError("Progress cannot exceed total.")
        self.completed += 1
        if succeeded:
            self.succeeded += 1
        else:
            self.failed += 1
        now = self.clock()
        refresh = now - self._last_update >= 0.2 if self.interactive else self.percent // 5 > self._last_bucket
        if refresh or self.completed == self.total:
            self._render(now)


class FailureSummary:
    def __init__(self):
        self.counts: Counter[tuple[str, str]] = Counter()

    def add(self, stage: str, detail: str) -> None:
        self.counts[(stage, detail)] += 1

    def write(self, stream: TextIO | None = None) -> None:
        output = stream if stream is not None else sys.stderr
        shown = 0
        for (stage, detail), count in self.counts.most_common(3):
            print(f"失败摘要：{stage}失败：{detail}（{count} 条）", file=output, flush=True)
            shown += count
        remaining = sum(self.counts.values()) - shown
        if remaining:
            print(f"其他失败：{remaining} 条", file=output, flush=True)

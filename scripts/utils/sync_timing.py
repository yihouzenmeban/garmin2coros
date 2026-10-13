from collections import defaultdict
from contextlib import contextmanager
import sys
import threading
import time


class StageTimings:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.durations = defaultdict(float)
        self.lock = threading.Lock()

    @contextmanager
    def measure(self, stage: str):
        started = self.clock()
        try:
            yield
        finally:
            elapsed = self.clock() - started
            with self.lock:
                self.durations[stage] += elapsed

    def write(self, stream=None):
        with self.lock:
            stages = [f"{stage}累计 {elapsed:.2f}s" for stage, elapsed in self.durations.items()]
        stages.append(f"总耗时 {self.clock() - self.started:.2f}s")
        print("阶段耗时：" + "；".join(stages), file=stream or sys.stdout, flush=True)

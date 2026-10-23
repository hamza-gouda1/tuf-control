"""One sampler for every source: collect() off the main thread, commit() on it.

collect() reads all cheap sources once and returns flat values plus per-source info;
commit() pushes the values into the shared Store. Readers keep their previous counters
for deltas, so collect() must not run concurrently with itself (the UI guards that).
"""
from __future__ import annotations

import time

from .cpu import CpuReader
from .gpu_intel import IntelGpu
from .gpu_nvidia import NvidiaGpu
from .memory import MemoryReader
from .network import Network
from .power import Power
from .ring import Store
from .sensors import Sensors
from .storage import Storage


class Sampler:
    def __init__(self, root: str = "/", fake: bool = False, capacity: int = 300) -> None:
        self.root = root
        self.fake = fake
        self.store = Store(capacity)
        self.cpu = CpuReader(root)
        self.mem = MemoryReader(root)
        self.sensors = Sensors(root)
        self.igpu = IntelGpu(root)
        self.dgpu = NvidiaGpu(root, fake=fake)
        self.disk = Storage(root)
        self.net = Network(root)
        self.power = Power(root)
        self.sources = [("cpu", self.cpu), ("mem", self.mem), ("sensors", self.sensors),
                        ("igpu", self.igpu), ("dgpu", self.dgpu), ("disk", self.disk),
                        ("net", self.net), ("battery", self.power)]
        self.info: dict = {}
        self.cost_ms = 0.0  # last collect() wall time, shown in About/PERFORMANCE

    def collect(self, now: float | None = None) -> tuple[float, dict, dict]:
        now = time.monotonic() if now is None else now
        t0 = time.perf_counter()
        vals: dict = {}
        info: dict = {"errors": {}}
        for name, src in self.sources:
            try:
                src.sample(now, vals, info)
            except Exception as exc:  # one broken source must not stop the others
                info["errors"][name] = f"{type(exc).__name__}: {exc}"
        self.cost_ms = (time.perf_counter() - t0) * 1000
        return now, vals, info

    def commit(self, now: float, vals: dict, info: dict) -> None:
        self.store.push(now, vals)
        self.info = info

    def sample(self, now: float | None = None) -> dict:
        """collect + commit in one go (tests, validation, CLI)."""
        now, vals, info = self.collect(now)
        self.commit(now, vals, info)
        return vals

    def close(self) -> None:
        self.dgpu.release()

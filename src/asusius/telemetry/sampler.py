# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""One sampler for every source: collect() off the main thread, commit() on it.

collect() reads all cheap sources once and returns flat values plus per-source info;
commit() pushes the values into the shared Store. Readers keep their previous counters
for deltas, so collect() must not run concurrently with itself (the UI guards that).
"""
from __future__ import annotations

import time

from .cpu import CpuReader
from .gpu_amd import AmdDiscreteGpu, AmdGpu, find_amd_cards
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
        intel = IntelGpu(root)
        nv = NvidiaGpu(root, fake=fake)
        amd_cards = find_amd_cards(root)
        igpu_pdev = intel.pdev
        if intel.card is not None:
            self.igpu = intel
        elif amd_cards and (nv.dev is not None or any(c["boot_vga"] for c in amd_cards)
                            or len(amd_cards) == 1):
            ig_card = next((c for c in amd_cards if c["boot_vga"]), amd_cards[0])
            self.igpu = AmdGpu(root, card_info=ig_card)
            igpu_pdev = ig_card["pdev"]
        else:
            self.igpu = intel
        if nv.dev is not None:
            self.dgpu = nv
        else:
            remaining_amd = [c for c in amd_cards if not igpu_pdev or c["pdev"] != igpu_pdev]
            if remaining_amd:
                self.dgpu = AmdDiscreteGpu(root, card_info=remaining_amd[0])
            else:
                self.dgpu = nv
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

"""Lightweight system telemetry from /proc, /sys and NVML (no third-party dependencies).

GTK-free: the UI drives `Sampler` from one timer; tests and tests/hardware run it directly.
"""
from .ring import Store
from .sampler import Sampler

__all__ = ["Sampler", "Store"]

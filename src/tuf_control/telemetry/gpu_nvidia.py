"""NVIDIA discrete GPU through NVML (ctypes on libnvidia-ml.so.1), nvidia-smi as fallback.

Power rule: the dGPU is only queried while its runtime PM state is "active". Any NVML
call or nvidia-smi run resumes a suspended GPU, so a sleeping GPU is reported as
sleeping and left alone. When it is awake but idle, the NVML session is closed and
polling backs off so the driver can put it back to sleep.
"""
from __future__ import annotations

import ctypes
import json

from .pci import display_devices, pci_name, runtime_state
from .util import join

IDLE_SAMPLES = 3  # consecutive idle samples before letting go
IDLE_BACKOFF_S = 30.0  # then one check per this long while it stays awake and idle
SMI_EVERY_S = 5.0  # nvidia-smi fallback rate limit
NVML_OK, NVML_NOT_SUPPORTED, NVML_INSUFFICIENT_SIZE = 0, 3, 7
CLOCK_GRAPHICS, CLOCK_SM, CLOCK_MEM, CLOCK_VIDEO = 0, 1, 2, 3


class _Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _MemV1(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class _MemV2(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("total", ctypes.c_ulonglong),
                ("reserved", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class _Proc(ctypes.Structure):  # nvmlProcessInfo_t (v2/v3 layout)
    _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong),
                ("gpuInstanceId", ctypes.c_uint), ("computeInstanceId", ctypes.c_uint)]


class _ProcUtil(ctypes.Structure):  # nvmlProcessUtilizationSample_t
    _fields_ = [("pid", ctypes.c_uint), ("timeStamp", ctypes.c_ulonglong),
                ("smUtil", ctypes.c_uint), ("memUtil", ctypes.c_uint),
                ("encUtil", ctypes.c_uint), ("decUtil", ctypes.c_uint)]


NOT_AVAILABLE_ULL = (1 << 64) - 1


class Nvml:
    """Minimal NVML binding: only the read-only calls this app uses."""

    def __init__(self, libname: str = "libnvidia-ml.so.1") -> None:
        self.lib = ctypes.CDLL(libname)
        self.open = False
        for name in ("nvmlInit_v2", "nvmlShutdown", "nvmlDeviceGetHandleByPciBusId_v2",
                     "nvmlDeviceGetName", "nvmlSystemGetDriverVersion",
                     "nvmlDeviceGetUtilizationRates", "nvmlDeviceGetMemoryInfo",
                     "nvmlDeviceGetClockInfo", "nvmlDeviceGetMaxClockInfo",
                     "nvmlDeviceGetTemperature", "nvmlDeviceGetPowerUsage",
                     "nvmlDeviceGetEnforcedPowerLimit", "nvmlDeviceGetFanSpeed",
                     "nvmlDeviceGetPerformanceState", "nvmlDeviceGetEncoderUtilization",
                     "nvmlDeviceGetDecoderUtilization"):
            getattr(self.lib, name).restype = ctypes.c_int
        self._mem_v2 = hasattr(self.lib, "nvmlDeviceGetMemoryInfo_v2")
        self._procs = [n for n in ("nvmlDeviceGetGraphicsRunningProcesses_v3",
                                   "nvmlDeviceGetComputeRunningProcesses_v3")
                       if hasattr(self.lib, n)]
        self._putil = hasattr(self.lib, "nvmlDeviceGetProcessUtilization")
        self._last_ts = 0

    def init(self) -> bool:
        if not self.open:
            self.open = self.lib.nvmlInit_v2() == NVML_OK
        return self.open

    def shutdown(self) -> None:
        if self.open:
            self.lib.nvmlShutdown()
            self.open = False

    def handle(self, pci_addr: str):
        h = ctypes.c_void_p()
        rc = self.lib.nvmlDeviceGetHandleByPciBusId_v2(pci_addr.encode(), ctypes.byref(h))
        return h if rc == NVML_OK else None

    def _uint(self, fn, *args) -> int | None:
        v = ctypes.c_uint()
        return v.value if fn(*args, ctypes.byref(v)) == NVML_OK else None

    def static(self, h) -> dict:
        buf = ctypes.create_string_buffer(96)
        name = buf.value.decode() if self.lib.nvmlDeviceGetName(h, buf, 96) == NVML_OK else None
        drv = ctypes.create_string_buffer(80)
        ver = drv.value.decode() if self.lib.nvmlSystemGetDriverVersion(drv, 80) == NVML_OK \
            else None
        return {
            "name": name, "driver_version": ver,
            "max_gr_mhz": self._uint(self.lib.nvmlDeviceGetMaxClockInfo, h, CLOCK_GRAPHICS),
            "max_mem_mhz": self._uint(self.lib.nvmlDeviceGetMaxClockInfo, h, CLOCK_MEM),
        }

    def query(self, h) -> dict:
        lib = self.lib
        out: dict = {}
        u = _Util()
        if lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == NVML_OK:
            out["util"], out["mem_util"] = float(u.gpu), float(u.memory)
        mem_done = False
        if self._mem_v2:
            m2 = _MemV2(version=ctypes.sizeof(_MemV2) | (2 << 24))
            if lib.nvmlDeviceGetMemoryInfo_v2(h, ctypes.byref(m2)) == NVML_OK:
                out["vram_used"], out["vram_total"] = m2.used, m2.total
                out["vram_reserved"] = m2.reserved
                mem_done = True
        if not mem_done:
            m = _MemV1()
            if lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(m)) == NVML_OK:
                out["vram_used"], out["vram_total"] = m.used, m.total
        out["gr_mhz"] = self._uint(lib.nvmlDeviceGetClockInfo, h, CLOCK_GRAPHICS)
        out["mem_mhz"] = self._uint(lib.nvmlDeviceGetClockInfo, h, CLOCK_MEM)
        out["video_mhz"] = self._uint(lib.nvmlDeviceGetClockInfo, h, CLOCK_VIDEO)
        out["temp"] = self._uint(lib.nvmlDeviceGetTemperature, h, 0)
        mw = self._uint(lib.nvmlDeviceGetPowerUsage, h)
        out["power"] = None if mw is None else mw / 1000
        lim = self._uint(lib.nvmlDeviceGetEnforcedPowerLimit, h)
        out["power_limit"] = None if lim is None else lim / 1000
        out["fan"] = self._uint(lib.nvmlDeviceGetFanSpeed, h)
        ps = ctypes.c_int()
        if lib.nvmlDeviceGetPerformanceState(h, ctypes.byref(ps)) == NVML_OK and ps.value < 32:
            out["pstate"] = ps.value
        for key, fn in (("enc", lib.nvmlDeviceGetEncoderUtilization),
                        ("dec", lib.nvmlDeviceGetDecoderUtilization)):
            v, period = ctypes.c_uint(), ctypes.c_uint()
            if fn(h, ctypes.byref(v), ctypes.byref(period)) == NVML_OK:
                out[key] = float(v.value)
        return out

    def processes(self, h) -> dict[int, dict]:
        """{pid: {'type': 'G'|'C'|'C+G', 'vram': bytes|None, 'sm': %|None}}."""
        out: dict[int, dict] = {}
        for fname in self._procs:
            kind = "C" if "Compute" in fname else "G"
            n = ctypes.c_uint(64)
            arr = (_Proc * 64)()
            if getattr(self.lib, fname)(h, ctypes.byref(n), arr) != NVML_OK:
                continue
            for i in range(min(n.value, 64)):
                p = arr[i]
                mem = None if p.usedGpuMemory == NOT_AVAILABLE_ULL else p.usedGpuMemory
                cur = out.get(p.pid)
                if cur:
                    cur["type"] = "C+G"
                    cur["vram"] = max(cur["vram"] or 0, mem or 0) or None
                else:
                    out[p.pid] = {"type": kind, "vram": mem, "sm": None}
        if self._putil and out:
            n = ctypes.c_uint(64)
            arr = (_ProcUtil * 64)()
            rc = self.lib.nvmlDeviceGetProcessUtilization(h, arr, ctypes.byref(n),
                                                          ctypes.c_ulonglong(self._last_ts))
            if rc == NVML_OK:
                newest = self._last_ts
                for i in range(min(n.value, 64)):
                    s = arr[i]
                    newest = max(newest, s.timeStamp)
                    if s.pid in out:
                        prev = out[s.pid]["sm"] or 0
                        out[s.pid]["sm"] = max(prev, float(s.smUtil))
                self._last_ts = newest
        return out


SMI_FIELDS = ("name,driver_version,utilization.gpu,utilization.memory,memory.used,"
              "memory.total,clocks.gr,clocks.mem,temperature.gpu,power.draw,"
              "enforced.power.limit,fan.speed,pstate")


def parse_smi(line: str) -> dict:
    """One CSV line of `nvidia-smi --query-gpu=SMI_FIELDS --format=csv,noheader,nounits`."""
    p = [x.strip() for x in line.split(",")]
    if len(p) < 13:
        return {}

    def num(s):
        try:
            return float(s)
        except ValueError:
            return None
    mib = 1 << 20
    used, total = num(p[4]), num(p[5])
    ps = p[12]
    return {"name": p[0], "driver_version": p[1], "util": num(p[2]), "mem_util": num(p[3]),
            "vram_used": None if used is None else int(used * mib),
            "vram_total": None if total is None else int(total * mib),
            "gr_mhz": num(p[6]), "mem_mhz": num(p[7]), "temp": num(p[8]), "power": num(p[9]),
            "power_limit": num(p[10]), "fan": num(p[11]),
            "pstate": int(ps[1:]) if ps.startswith("P") and ps[1:].isdigit() else None}


class NvidiaGpu:
    def __init__(self, root: str = "/", fake: bool = False) -> None:
        self.root, self.fake = root, fake
        devs = [d for d in display_devices(root) if d["vendor"] == "0x10de"]
        self.dev = devs[0] if devs else None
        self.nvml: Nvml | None = None
        self.backend = None
        self.reason = None
        self.static: dict = {}
        self.handle = None
        self._idle = 0
        self._hold_until = 0.0
        self._smi_t = -1e9
        self._smi_last: dict = {}
        self.procs: dict[int, dict] = {}
        self.last_q: dict | None = None  # the most recent reading, for the idle display
        self.last_q_t = 0.0
        if self.dev is None:
            self.reason = "no NVIDIA GPU on the PCI bus"
            return
        self.pci_label = pci_name(self.dev["vendor"] or "", self.dev["device"] or "")
        if fake:
            self.backend = "fake"
            return
        try:
            self.nvml = Nvml()
            self.backend = "nvml"
        except OSError:
            import shutil
            if shutil.which("nvidia-smi"):
                self.backend = "nvidia-smi"
            else:
                self.reason = "libnvidia-ml.so.1 and nvidia-smi not found (driver not installed?)"

    # -- helpers -----------------------------------------------------------------------
    def name(self) -> str:
        if self.static.get("name"):
            return self.static["name"]
        if self.dev is None:
            return "NVIDIA GPU"
        return f"NVIDIA {self.pci_label}" if self.pci_label else \
            f"NVIDIA GPU ({self.dev['vendor']}:{self.dev['device']})"

    def release(self) -> None:
        if self.nvml is not None:
            self.nvml.shutdown()
        self.handle = None

    def _query_nvml(self) -> dict | None:
        nv = self.nvml
        if not nv.init():
            self.reason = "nvmlInit failed"
            return None
        if self.handle is None:
            self.handle = nv.handle(self.dev["addr"])
            if self.handle is None:
                self.reason = f"NVML has no device at {self.dev['addr']}"
                nv.shutdown()
                return None
            if not self.static:
                self.static = nv.static(self.handle)
        q = nv.query(self.handle)
        self.procs = nv.processes(self.handle)
        return q

    def _query_smi(self, now: float) -> dict | None:
        if now - self._smi_t < SMI_EVERY_S:
            return None  # rate limited: keep the previous reading, no new sample
        self._smi_t = now
        from ..backends.util import run_cmd
        try:
            out = run_cmd(["nvidia-smi", f"--query-gpu={SMI_FIELDS}",
                           "--format=csv,noheader,nounits", "-i", self.dev["addr"]], timeout=3)
        except Exception as exc:  # optional source
            self.reason = f"nvidia-smi failed: {exc}"
            return None
        q = parse_smi(out.splitlines()[0]) if out.strip() else {}
        if q and not self.static:
            self.static = {"name": q.get("name"), "driver_version": q.get("driver_version")}
        self.procs = {}
        return q

    def _query_fake(self) -> dict | None:
        try:
            with open(join(self.root, "fake/nvml.json")) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        self.static = data.get("static", {})
        self.procs = {int(k): v for k, v in data.get("procs", {}).items()}
        return data.get("query")

    # -- per tick -----------------------------------------------------------------------
    def sample(self, now: float, vals: dict, info: dict) -> None:
        out: dict = {"present": self.dev is not None, "backend": self.backend}
        if self.dev is None or self.backend is None:
            out["reason"] = self.reason
            info["dgpu"] = out
            return
        rs, ps = runtime_state(self.dev["dir"])
        out.update({"runtime": rs, "power_state": ps, "addr": self.dev["addr"],
                    "name": self.name()})
        if rs == "error":  # the driver lost the GPU (NVRM rpc errors): do not poll it
            self.release()
            self._idle = 0
            self.procs = {}
            out["state"] = "lost"
            info["dgpu"] = out
            return
        if rs is not None and rs != "active":
            self.release()
            self._idle = 0
            self.procs = {}
            out["state"] = "sleeping"
            info["dgpu"] = out
            return
        if now < self._hold_until:
            out["state"] = "idle-released"
            out["next_check_s"] = self._hold_until - now
            out["last_q"], out["last_age_s"] = self.last_q, now - self.last_q_t
            out["static"] = self.static
            info["dgpu"] = out
            return
        if self.backend == "nvml":
            q = self._query_nvml()
        elif self.backend == "nvidia-smi":
            q = self._query_smi(now)
            if q is None and self._smi_last:
                out.update(self._smi_last)
        else:
            q = self._query_fake()
        if q:
            vals["dgpu.util"] = q.get("util")
            vals["dgpu.mem_util"] = q.get("mem_util")
            vals["dgpu.vram"] = q.get("vram_used")
            vals["dgpu.temp"] = q.get("temp")
            vals["dgpu.power"] = q.get("power")
            vals["dgpu.gr_mhz"] = q.get("gr_mhz")
            vals["dgpu.mem_mhz"] = q.get("mem_mhz")
            if q.get("enc") is not None:
                vals["dgpu.enc"], vals["dgpu.dec"] = q.get("enc"), q.get("dec")
            out["q"] = q
            self.last_q, self.last_q_t = q, now
            if self.backend == "nvidia-smi":
                self._smi_last = {"q": q}
            busy = (q.get("util") or 0) >= 1 or (q.get("mem_util") or 0) >= 5 or \
                any("C" in p["type"] for p in self.procs.values())
            self._idle = 0 if busy else self._idle + 1
            if self._idle >= IDLE_SAMPLES:
                self.release()
                self._idle = 0
                self._hold_until = now + IDLE_BACKOFF_S
                out["released"] = True
        out["state"] = "awake"
        out["static"] = self.static
        out["procs"] = self.procs
        if self.reason and not q:
            out["reason"] = self.reason
        info["dgpu"] = out

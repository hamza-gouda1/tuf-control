# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
from __future__ import annotations

import re
import time
from pathlib import Path

from ..core import (
    BackendUnavailable,
    BatteryHealth,
    Capability,
    CapStatus,
    FanCurve,
    FirmwareAttribute,
    GpuAction,
    GpuMode,
    GpuStatus,
    PowerLimit,
    Profile,
    Telemetry,
    Unsupported,
    ValidationError,
)
from .base import Backend
from .util import DirectWriter, FallbackWriter, Writer, read_text, run_cmd

BATTERY_RANGE = (20, 100)
_NV_QUERY = "temperature.gpu,power.draw,utilization.gpu"


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


class SysfsBackend(Backend):
    name = "sysfs"

    def __init__(self, root: str | Path = "/", writer: Writer | None = None,
                 use_nvidia_smi: bool = True) -> None:
        self.root = Path(root)
        # on the real root, root-only attributes fall back to the polkit helper (after EACCES)
        self.writer: Writer = writer or (
            FallbackWriter() if str(root) == "/" else DirectWriter())
        self.use_nvidia_smi = use_nvidia_smi
        self._rapl_prev: tuple[int, float] | None = None

    # -- paths ------------------------------------------------------------
    def _p(self, rel: str) -> Path:
        return self.root / rel

    @property
    def profile_path(self) -> Path:
        return self._p("sys/firmware/acpi/platform_profile")

    @property
    def armoury(self) -> Path:
        return self._p("sys/class/firmware-attributes/asus-armoury/attributes")

    @property
    def battery_dir(self) -> Path | None:
        base = self._p("sys/class/power_supply")
        for d in sorted(base.glob("BAT*")):
            return d
        return None

    @property
    def kbd_dir(self) -> Path:
        return self._p("sys/class/leds/asus::kbd_backlight")

    def _hwmon(self, name: str) -> Path | None:
        for d in sorted(self._p("sys/class/hwmon").glob("hwmon*")):
            if read_text(d / "name") == name:
                return d
        return None

    # -- probe ------------------------------------------------------------
    def probe(self) -> dict[Capability, CapStatus]:
        st: dict[Capability, CapStatus] = {}
        ok = CapStatus(True)
        st[Capability.PROFILE] = ok if self.profile_path.exists() else CapStatus(
            False, "platform_profile not present")
        if not self.armoury.is_dir():
            reason = "asus-armoury firmware attributes not present (kernel/driver)"
            st[Capability.POWER_LIMITS] = CapStatus(False, reason)
            st[Capability.FIRMWARE_ATTRIBUTES] = CapStatus(False, reason)
        else:
            st[Capability.FIRMWARE_ATTRIBUTES] = ok
            limits = self.get_limits()
            if not limits:
                st[Capability.POWER_LIMITS] = CapStatus(False, "no integer attributes exposed")
            elif not any(lim.bounded for lim in limits):
                st[Capability.POWER_LIMITS] = CapStatus(
                    False, "attributes present but firmware exposes no readable values "
                           "(the kernel driver has no power-limit table for this board)")
            else:
                st[Capability.POWER_LIMITS] = ok
        bat = self.battery_dir
        thr = bat is not None and (bat / "charge_control_end_threshold").exists()
        st[Capability.BATTERY_THRESHOLD] = ok if thr else CapStatus(
            False, "charge_control_end_threshold not present")
        full = bat is not None and (bat / "energy_full_design").exists()
        st[Capability.BATTERY_HEALTH] = ok if full else CapStatus(False, "battery energy not present")
        if (self.armoury / "gpu_mux_mode").exists() or (self.armoury / "dgpu_disable").exists():
            st[Capability.GPU_MODE] = CapStatus(True, "inferred from mux/dgpu attributes")
        else:
            st[Capability.GPU_MODE] = CapStatus(False, "no gpu_mux_mode/dgpu_disable attribute")
        st[Capability.KBD_BRIGHTNESS] = ok if (self.kbd_dir / "brightness").exists() else CapStatus(
            False, "asus::kbd_backlight not present")
        st[Capability.KBD_RGB] = CapStatus(
            False, "kbd_rgb_* files are write-only and root-only; use the D-Bus backend")
        st[Capability.FAN_CURVES] = ok if self._hwmon("asus_custom_fan_curve") else CapStatus(
            False, "asus_custom_fan_curve hwmon not present")
        st[Capability.TELEMETRY] = ok
        return st

    # -- profile ----------------------------------------------------------
    def _choices(self) -> dict[Profile, str]:
        raw = read_text(self.profile_path.with_name("platform_profile_choices")) or ""
        out: dict[Profile, str] = {}
        for tok in raw.split():
            try:
                out.setdefault(Profile.parse(tok), tok)
            except ValidationError:
                pass
        return out

    def get_profile(self) -> Profile:
        raw = read_text(self.profile_path)
        if raw is None:
            raise BackendUnavailable("platform_profile unreadable")
        return Profile.parse(raw)

    def set_profile(self, profile: Profile) -> None:
        choices = self._choices()
        if choices and profile not in choices:
            raise ValidationError(f"profile {profile.value} not offered by firmware")
        self.writer.write(self.profile_path, choices.get(profile, profile.value))

    # -- firmware attributes -------------------------------------------------
    def _attr_dirs(self) -> list[Path]:
        if not self.armoury.is_dir():
            return []
        return sorted(d for d in self.armoury.iterdir() if d.is_dir())

    def get_attributes(self) -> list[FirmwareAttribute]:
        if not self.armoury.is_dir():
            raise BackendUnavailable("asus-armoury not present")
        out = []
        for d in self._attr_dirs():
            typ = read_text(d / "type")
            if typ is None:
                continue  # e.g. pending_reboot
            cur_raw = read_text(d / "current_value")
            cur = _int(cur_raw) if typ == "integer" else cur_raw
            poss = (read_text(d / "possible_values") or "").split(";")
            out.append(FirmwareAttribute(
                d.name, typ, cur, [p for p in poss if p], read_text(d / "display_name") or ""))
        return out

    def get_limits(self) -> list[PowerLimit]:
        if not self.armoury.is_dir():
            raise BackendUnavailable("asus-armoury not present")
        out = []
        for d in self._attr_dirs():
            if read_text(d / "type") != "integer" or not (d / "min_value").exists():
                continue
            out.append(PowerLimit(
                name=d.name,
                current=_int(read_text(d / "current_value")),
                min=_int(read_text(d / "min_value")),
                max=_int(read_text(d / "max_value")),
                default=_int(read_text(d / "default_value")),
                increment=_int(read_text(d / "scalar_increment")) or 1,
                display_name=read_text(d / "display_name") or "",
            ))
        return out

    def set_limit(self, name: str, value: int) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValidationError(f"bad attribute name {name!r}")
        lim = next((x for x in self.get_limits() if x.name == name), None)
        if lim is None:
            raise ValidationError(f"unknown power limit {name!r}")
        lim.validate(value)
        self.writer.write(self.armoury / name / "current_value", str(value))

    # -- battery ------------------------------------------------------------
    def get_battery_threshold(self) -> int:
        bat = self.battery_dir
        v = _int(read_text(bat / "charge_control_end_threshold")) if bat else None
        if v is None:
            raise BackendUnavailable("charge_control_end_threshold unreadable")
        return v

    def set_battery_threshold(self, value: int) -> None:
        lo, hi = BATTERY_RANGE
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValidationError(f"battery threshold must be an integer in {lo}-{hi}")
        bat = self.battery_dir
        if bat is None:
            raise BackendUnavailable("no battery")
        self.writer.write(bat / "charge_control_end_threshold", str(value))

    def get_battery_health(self) -> BatteryHealth:
        bat = self.battery_dir
        full = _int(read_text(bat / "energy_full")) if bat else None
        design = _int(read_text(bat / "energy_full_design")) if bat else None
        if full is None or design is None:
            raise BackendUnavailable("battery energy values unreadable")
        return BatteryHealth(full, design, _int(read_text(bat / "cycle_count")))

    # -- gpu ----------------------------------------------------------------
    def get_gpu_mode(self) -> GpuMode:
        mux = _int(read_text(self.armoury / "gpu_mux_mode" / "current_value"))
        dis = _int(read_text(self.armoury / "dgpu_disable" / "current_value"))
        if mux is None and dis is None:
            raise BackendUnavailable("gpu attributes unreadable")
        if mux == 0:
            return GpuMode.ASUS_MUX_DGPU
        if dis == 1:
            return GpuMode.INTEGRATED
        return GpuMode.HYBRID

    def _dgpu_dir(self) -> Path | None:
        """The discrete GPU: an NVIDIA display controller, else a non-boot VGA device."""
        gpus = [d for d in sorted(self._p("sys/bus/pci/devices").glob("*"))
                if (read_text(d / "class") or "").startswith("0x03")]
        for d in gpus:
            if read_text(d / "vendor") == "0x10de":
                return d
        return next((d for d in gpus if read_text(d / "boot_vga") == "0"), None)

    def dgpu_runtime(self) -> str | None:
        """Runtime PM state of the discrete GPU without waking it."""
        d = self._dgpu_dir()
        return read_text(d / "power/runtime_status") if d else None

    def get_gpu_status(self) -> GpuStatus:
        mode = self.get_gpu_mode()
        supported = [GpuMode.HYBRID]
        if (self.armoury / "dgpu_disable").exists():
            supported.insert(0, GpuMode.INTEGRATED)
        if (self.armoury / "gpu_mux_mode").exists():
            supported.append(GpuMode.ASUS_MUX_DGPU)
        pending = read_text(self.armoury / "pending_reboot" / "pending_reboot") == "1"
        return GpuStatus(mode, supported, None, GpuAction.REBOOT if pending else None,
                         self.dgpu_runtime())

    def set_attribute(self, name: str, value: int | str) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValidationError(f"bad attribute name {name!r}")
        attr = next((a for a in self.get_attributes() if a.name == name), None)
        if attr is None:
            raise ValidationError(f"unknown attribute {name!r}")
        if attr.type == "integer":
            self.set_limit(name, int(value))
            return
        if attr.possible_values and str(value) not in attr.possible_values:
            raise ValidationError(f"{name}: {value} not one of {', '.join(attr.possible_values)}")
        self.writer.write(self.armoury / name / "current_value", str(value))

    # -- keyboard -----------------------------------------------------------
    def get_kbd_brightness(self) -> tuple[int, int]:
        cur = _int(read_text(self.kbd_dir / "brightness"))
        mx = _int(read_text(self.kbd_dir / "max_brightness"))
        if cur is None or mx is None:
            raise BackendUnavailable("keyboard backlight unreadable")
        return cur, mx

    def set_kbd_brightness(self, value: int) -> None:
        _, mx = self.get_kbd_brightness()
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= mx:
            raise ValidationError(f"keyboard brightness must be 0-{mx}")
        self.writer.write(self.kbd_dir / "brightness", str(value))

    # -- fan curves -----------------------------------------------------------
    _FAN_INDEX = {"CPU": 1, "GPU": 2, "MID": 3}

    def _check_active(self, profile: Profile | None) -> None:
        if profile is not None and profile is not self.get_profile():
            raise Unsupported("sysfs only exposes the active profile's fan curve")

    def get_fan_curves(self, profile: Profile | None = None) -> dict[str, FanCurve]:
        self._check_active(profile)
        hw = self._hwmon("asus_custom_fan_curve")
        if hw is None:
            raise BackendUnavailable("asus_custom_fan_curve not present")
        curves = {}
        for idx, label in ((1, "CPU"), (2, "GPU")):
            temps = [_int(read_text(hw / f"pwm{idx}_auto_point{i}_temp")) for i in range(1, 9)]
            pwms = [_int(read_text(hw / f"pwm{idx}_auto_point{i}_pwm")) for i in range(1, 9)]
            if None in temps or None in pwms:
                continue
            enabled = read_text(hw / f"pwm{idx}_enable") == "1"
            curves[label] = FanCurve.from_lists(label, temps, pwms, enabled)
        if not curves:
            raise BackendUnavailable("fan curve files unreadable")
        return curves

    def set_fan_curve(self, profile: Profile, curve: FanCurve) -> None:
        curve.validate()
        self._check_active(profile)
        hw = self._hwmon("asus_custom_fan_curve")
        idx = self._FAN_INDEX.get(curve.fan.upper())
        if hw is None or idx is None:
            raise BackendUnavailable(f"no custom fan curve for {curve.fan}")
        for i, pt in enumerate(curve.points, 1):
            self.writer.write(hw / f"pwm{idx}_auto_point{i}_temp", str(pt.temp))
            self.writer.write(hw / f"pwm{idx}_auto_point{i}_pwm", str(pt.pwm))
        # asus-wmi: 1 = custom curve on, 2 = firmware automatic
        self.writer.write(hw / f"pwm{idx}_enable", "1" if curve.enabled else "2")

    def reset_fan_curves(self, profile: Profile) -> None:
        self._check_active(profile)
        hw = self._hwmon("asus_custom_fan_curve")
        if hw is None:
            raise BackendUnavailable("asus_custom_fan_curve not present")
        for idx in (1, 2):
            if (hw / f"pwm{idx}_enable").exists():
                self.writer.write(hw / f"pwm{idx}_enable", "3")  # 3 = factory defaults

    # -- telemetry ------------------------------------------------------------
    def get_telemetry(self) -> Telemetry:
        t = Telemetry()
        for hw_name in ("coretemp", "k10temp", "acpitz"):
            hw = self._hwmon(hw_name)
            raw = _int(read_text(hw / "temp1_input")) if hw else None
            if raw is not None:
                t.cpu_temp_c = raw / 1000
                break
        for hw_name in ("asus", "acpi_fan"):
            hw = self._hwmon(hw_name)
            if hw is None:
                continue
            for f in sorted(hw.glob("fan*_input")):
                rpm = _int(read_text(f))
                if rpm is None:
                    continue
                label = read_text(f.with_name(f.name.replace("_input", "_label"))) \
                    or f.name.removesuffix("_input")
                t.fan_rpm[label] = rpm
            if t.fan_rpm:
                break
        bat = self.battery_dir
        if bat:
            p = _int(read_text(bat / "power_now"))
            t.battery_power_w = None if p is None else round(p / 1e6, 2)
            t.battery_status = read_text(bat / "status")
            t.battery_percent = _int(read_text(bat / "capacity"))
        try:
            t.profile = self.get_profile()
        except (BackendUnavailable, ValidationError):
            pass
        t.cpu_power_w = self._rapl_power()
        t.gpu_runtime = self.dgpu_runtime()
        # nvidia-smi wakes a suspended dGPU (and drains the battery): only ask when awake
        if self.use_nvidia_smi and t.gpu_runtime in (None, "active"):
            self._fill_gpu(t)
        return t

    def _rapl_power(self) -> float | None:
        """CPU package power from the RAPL energy counter (often root-only)."""
        e = _int(read_text(self._p("sys/class/powercap/intel-rapl:0/energy_uj")))
        if e is None:
            return None
        now = time.monotonic()
        prev, self._rapl_prev = self._rapl_prev, (e, now)
        if prev is None or now - prev[1] < 0.2 or e < prev[0]:
            return None
        return round((e - prev[0]) / 1e6 / (now - prev[1]), 1)

    @staticmethod
    def _fill_gpu(t: Telemetry) -> None:
        try:
            out = run_cmd(["nvidia-smi", f"--query-gpu={_NV_QUERY}",
                           "--format=csv,noheader,nounits"], timeout=2.0)
        except Exception:  # optional data source: any failure just means no GPU numbers
            return
        parts = [p.strip() for p in out.splitlines()[0].split(",")] if out.strip() else []
        def num(s):
            try:
                return float(s)
            except ValueError:
                return None
        if len(parts) == 3:
            t.gpu_temp_c, t.gpu_power_w, t.gpu_util_pct = (num(p) for p in parts)


__all__ = ["SysfsBackend", "Unsupported"]

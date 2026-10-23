"""Fake sysfs tree mimicking the real layout, for tests and CI."""
from __future__ import annotations

import json
from pathlib import Path

from ..core import (
    BackendUnavailable,
    Capability,
    CapStatus,
    FanCurve,
    GpuAction,
    GpuMode,
    GpuStatus,
    KbdEffect,
    Profile,
    Telemetry,
)
from .sysfs import SysfsBackend

DEFAULT_CURVES = {
    1: ([39, 48, 51, 55, 62, 71, 80, 97], [28, 53, 65, 116, 158, 180, 217, 255]),
    2: ([40, 50, 57, 64, 67, 72, 74, 97], [31, 71, 109, 146, 184, 212, 255, 255]),
}
# asusd AuraModeNum values offered by a TUF single-zone keyboard
FAKE_AURA_MODES = [0, 1, 2, 10]

# name -> (type, current, default, min, max, possible_values, display_name)
_ATTRS = {
    "ppt_pl1_spl": ("integer", 80, 80, 5, 115, None, "Set the CPU slow package limit"),
    "ppt_pl2_sppt": ("integer", 100, 100, 5, 135, None, "Set the CPU fast package limit"),
    "nv_dynamic_boost": ("integer", 15, 15, 5, 25, None, "Set the Nvidia dynamic boost limit"),
    "nv_temp_target": ("integer", 87, 87, 75, 87, None, "Set the Nvidia max thermal limit"),
    "nv_tgp": ("integer", 0, 0, 0, 25, None, "Set the additional TGP on top of the base TGP"),
    "nv_base_tgp": ("integer", 80, None, None, None, None, "Read the base TGP value"),
    "charge_mode": ("enumeration", 0, None, None, None, "0;1;2", "Show the current mode of charging"),
    "dgpu_disable": ("enumeration", 0, None, None, None, "0;1", "Disable the dGPU"),
    "gpu_mux_mode": ("enumeration", 1, None, None, None, "0;1", "Set the GPU display MUX mode"),
    "panel_overdrive": ("enumeration", 1, None, None, None, "0;1", "Set the panel refresh overdrive"),
    "screen_auto_brightness": ("enumeration", 0, None, None, None, "0;1", "Panel auto brightness"),
}


def _w(path: Path, text: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n")


def build_fake_tree(root: str | Path, *, ppt_readable: bool = True,
                    profile: str = "balanced", dgpu_runtime: str = "active") -> Path:
    """Create a fake root containing a sys/ tree. Returns the root."""
    root = Path(root)
    _w(root / "sys/firmware/acpi/platform_profile", profile)
    _w(root / "sys/firmware/acpi/platform_profile_choices", "quiet balanced performance")
    base = root / "sys/class/firmware-attributes/asus-armoury/attributes"
    _w(base / "pending_reboot/pending_reboot", 0)
    for name, (typ, cur, dflt, lo, hi, poss, disp) in _ATTRS.items():
        d = base / name
        _w(d / "type", typ)
        _w(d / "display_name", disp)
        _w(d / "current_value", cur)
        if poss:
            _w(d / "possible_values", poss)
        if typ == "integer" and name != "nv_base_tgp":
            _w(d / "scalar_increment", 1)
            if ppt_readable:
                _w(d / "default_value", dflt)
                _w(d / "min_value", lo)
                _w(d / "max_value", hi)
            else:  # real hardware with the PPT group disabled: files exist, reads fail
                for f in ("default_value", "min_value", "max_value"):
                    (d / f).mkdir(parents=True, exist_ok=True)  # reading a dir raises OSError
                (d / "current_value").unlink()
                (d / "current_value").mkdir()
    bat = root / "sys/class/power_supply/BAT0"
    for f, v in (("charge_control_end_threshold", 90), ("energy_full", 79172000),
                 ("energy_full_design", 90004000), ("power_now", 34870000),
                 ("status", "Discharging"), ("capacity", 51), ("cycle_count", 0)):
        _w(bat / f, v)
    led = root / "sys/class/leds/asus::kbd_backlight"
    _w(led / "brightness", 3)
    _w(led / "max_brightness", 3)
    hw = root / "sys/class/hwmon"
    _w(hw / "hwmon7/name", "coretemp")
    _w(hw / "hwmon7/temp1_input", 52000)
    _w(hw / "hwmon8/name", "asus")
    _w(hw / "hwmon8/fan1_input", 4200)
    _w(hw / "hwmon8/fan1_label", "cpu_fan")
    _w(hw / "hwmon8/fan2_input", 3900)
    _w(hw / "hwmon8/fan2_label", "gpu_fan")
    fc = hw / "hwmon9"
    _w(fc / "name", "asus_custom_fan_curve")
    for idx, (temps, pwms) in DEFAULT_CURVES.items():
        _w(fc / f"pwm{idx}_enable", 1)
        for i, (t, p) in enumerate(zip(temps, pwms), 1):
            _w(fc / f"pwm{idx}_auto_point{i}_temp", t)
            _w(fc / f"pwm{idx}_auto_point{i}_pwm", p)
    _w(root / "sys/class/powercap/intel-rapl:0/energy_uj", 1_000_000_000)
    for dev, vendor, boot in (("0000:00:02.0", "0x8086", 1), ("0000:01:00.0", "0x10de", 0)):
        d = root / "sys/bus/pci/devices" / dev
        _w(d / "class", "0x030000")
        _w(d / "vendor", vendor)
        _w(d / "boot_vga", boot)
        _w(d / "power/runtime_status", "active" if boot else dgpu_runtime)
    # stand-ins for daemons the fake backend replaces (supergfxd, asusd aura, nvidia-smi)
    fake = root / "fake"
    _w(fake / "nvidia-smi.csv", "46, 12.40, 3")
    _w(fake / "gfx.json", json.dumps({"mode": int(GpuMode.HYBRID), "pending_mode": None,
                                      "pending_action": None}))
    _w(fake / "aura.json", json.dumps({"mode": 0, "zone": 0, "colour1": [142, 42, 68],
                                       "colour2": [201, 174, 134], "speed": 1, "direction": 0,
                                       "supported_modes": FAKE_AURA_MODES}))
    return root


class FakeBackend(SysfsBackend):
    """Sysfs backend bound to a fake tree; never invokes external programs."""

    name = "fake"

    def __init__(self, root: str | Path, writer=None) -> None:
        super().__init__(root, writer=writer, use_nvidia_smi=False)

    # Daemon stand-ins live under <root>/fake as JSON; they never leave the fake tree.
    def _fake(self, name: str) -> Path:
        return self.root / "fake" / name

    def _load(self, name: str) -> dict:
        try:
            return json.loads(self._fake(name).read_text())
        except (OSError, ValueError):
            raise BackendUnavailable(f"fake {name} missing") from None

    def _save(self, name: str, data: dict) -> None:
        self._fake(name).write_text(json.dumps(data))

    def probe(self):
        st = super().probe()
        if self._fake("aura.json").exists():
            st[Capability.KBD_RGB] = CapStatus(True)
        return st

    def get_telemetry(self) -> Telemetry:
        t = super().get_telemetry()
        if t.gpu_runtime in (None, "active"):
            try:
                parts = [float(x) for x in self._fake("nvidia-smi.csv").read_text().split(",")]
                t.gpu_temp_c, t.gpu_power_w, t.gpu_util_pct = parts[:3]
            except (OSError, ValueError):
                pass
        return t

    def get_gpu_mode(self) -> GpuMode:
        try:
            return GpuMode(self._load("gfx.json")["mode"])
        except BackendUnavailable:
            return super().get_gpu_mode()

    def get_gpu_status(self) -> GpuStatus:
        st = super().get_gpu_status()
        try:
            g = self._load("gfx.json")
        except BackendUnavailable:
            return st
        st.pending_mode = GpuMode(g["pending_mode"]) if g.get("pending_mode") is not None else None
        st.pending_action = (GpuAction(g["pending_action"])
                             if g.get("pending_action") is not None else None)
        return st

    def set_gpu_mode(self, mode: GpuMode) -> GpuAction:
        g = self._load("gfx.json")
        current = GpuMode(g["mode"])
        if mode is current:
            g.update(pending_mode=None, pending_action=None)
            self._save("gfx.json", g)
            return GpuAction.NOTHING
        mux = GpuMode.ASUS_MUX_DGPU in (mode, current)
        action = GpuAction.REBOOT if mux else GpuAction.LOGOUT
        g.update(pending_mode=int(mode), pending_action=int(action))
        self._save("gfx.json", g)
        return action

    def get_kbd_rgb(self) -> dict:
        a = self._load("aura.json")
        data = {k: a[k] for k in ("mode", "zone", "colour1", "colour2", "speed", "direction")}
        cur, mx = self.get_kbd_brightness()
        return {"path": "fake", "led_mode": a["mode"], "mode_data": data, "brightness": cur,
                "supported_modes": a.get("supported_modes", FAKE_AURA_MODES),
                "supported_brightness": list(range(mx + 1))}

    def set_kbd_effect(self, effect: KbdEffect) -> None:
        effect.validate()
        a = self._load("aura.json")
        a.update(mode=effect.mode, zone=effect.zone, colour1=list(effect.colour1),
                 colour2=list(effect.colour2), speed=effect.speed, direction=effect.direction)
        self._save("aura.json", a)

    def reset_fan_curves(self, profile: Profile) -> None:
        self._check_active(profile)
        for idx, (temps, pwms) in DEFAULT_CURVES.items():
            self.set_fan_curve(profile, FanCurve.from_lists(("CPU", "GPU")[idx - 1], temps, pwms,
                                                            enabled=False))

    def one_shot_full_charge(self) -> None:
        self.set_battery_threshold(100)

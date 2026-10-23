"""Battery and AC adapter from /sys/class/power_supply."""
from __future__ import annotations

import os

from .util import join, listdir, read_int, read_str


def battery_watts(d: str) -> float | None:
    """Instantaneous power in W: power_now, else current_now x voltage_now."""
    p = read_int(os.path.join(d, "power_now"))
    if p is not None:
        return abs(p) / 1e6
    i = read_int(os.path.join(d, "current_now"))
    v = read_int(os.path.join(d, "voltage_now"))
    if i is not None and v is not None:
        return abs(i) * v / 1e12
    return None


class Power:
    def __init__(self, root: str = "/") -> None:
        self.root = root
        base = join(root, "sys/class/power_supply")
        self.batteries, self.adapters = [], []
        for n in listdir(base):
            t = read_str(os.path.join(base, n, "type"))
            if t == "Battery" and read_str(os.path.join(base, n, "scope")) != "Device":
                self.batteries.append(os.path.join(base, n))
            elif t == "Mains":
                self.adapters.append(os.path.join(base, n))

    def sample(self, now: float, vals: dict, info: dict) -> None:
        out = {"present": bool(self.batteries)}
        online = [read_int(os.path.join(a, "online")) for a in self.adapters]
        out["ac_online"] = any(o == 1 for o in online) if online else None
        if not self.batteries:
            info["battery"] = out
            return
        d = self.batteries[0]
        w = battery_watts(d)
        status = read_str(os.path.join(d, "status"))
        vals["bat.power"] = w
        vals["bat.percent"] = read_int(os.path.join(d, "capacity"))
        volt = read_int(os.path.join(d, "voltage_now"))
        vals["bat.voltage"] = None if volt is None else volt / 1e6
        e_now = read_int(os.path.join(d, "energy_now"))
        e_full = read_int(os.path.join(d, "energy_full"))
        e_design = read_int(os.path.join(d, "energy_full_design"))
        if e_now is None:  # charge_* in uAh: convert with the design voltage
            vmin = read_int(os.path.join(d, "voltage_min_design")) or volt
            c = [read_int(os.path.join(d, f)) for f in ("charge_now", "charge_full",
                                                         "charge_full_design")]
            if vmin and c[0] is not None:
                e_now, e_full, e_design = (None if x is None else x * vmin // 1_000_000
                                           for x in c)
        out.update({
            "name": os.path.basename(d), "status": status,
            "percent": vals["bat.percent"], "watts": w, "voltage": vals["bat.voltage"],
            "energy_now": e_now, "energy_full": e_full, "energy_design": e_design,
            "cycles": read_int(os.path.join(d, "cycle_count")),
            "limit": read_int(os.path.join(d, "charge_control_end_threshold")),
            "model": read_str(os.path.join(d, "model_name")),
            "technology": read_str(os.path.join(d, "technology")),
        })
        hours = None
        if w and w > 0.05 and e_now is not None:
            if status == "Discharging":
                hours = e_now / 1e6 / w
            elif status == "Charging" and e_full:
                hours = max(0, e_full - e_now) / 1e6 / w
        out["hours"] = hours  # at the current draw; a rough guide, not a prediction
        info["battery"] = out

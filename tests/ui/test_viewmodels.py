"""UI logic without a display: view-models, validators, curve editing, display config."""
import tempfile
import unittest
from pathlib import Path

from tuf_control.backends.fake import FakeBackend, build_fake_tree
from tuf_control.controller import Controller
from tuf_control.core import (
    BackendUnavailable,
    FanCurve,
    GpuAction,
    GpuMode,
    KbdEffect,
    PowerLimit,
    PrivilegeRequired,
    Profile,
    ValidationError,
)
from tuf_control.ui import viewmodels as vm

CPU = FanCurve.from_lists("CPU", [39, 48, 51, 55, 62, 71, 80, 97],
                          [28, 53, 65, 116, 158, 180, 217, 255])


class HistoryTests(unittest.TestCase):
    def test_window_and_order(self):
        h = vm.History(window_s=10)
        for t in range(20):
            h.push(float(t), t * 2)
        self.assertEqual(len(h), 11)  # 9..19 inclusive
        h.push(5.0, 99)  # older than last: ignored
        h.push(20.0, None)  # missing reading: ignored
        self.assertEqual(h.last, 38)
        xs = [x for x, _ in h.normalized(19.0)]
        self.assertAlmostEqual(xs[-1], 1.0)
        self.assertTrue(all(0.0 <= x <= 1.0 for x in xs))

    def test_bounds_min_span_and_floor(self):
        h = vm.History()
        self.assertEqual(h.bounds(0, 10), (0, 10))
        h.push(0, 50)
        h.push(1, 51)
        lo, hi = h.bounds(0, 10)
        self.assertLess(lo, 50)
        self.assertGreater(hi, 51)
        self.assertGreaterEqual(hi - lo, 10)
        h2 = vm.History()
        h2.push(0, 1)
        h2.push(1, 2)
        self.assertEqual(h2.bounds(0, 10)[0], 0)

    def test_smoothing(self):
        pts = [(0, 0), (0.5, 10), (1, 0)]
        self.assertEqual(vm.smooth_values(pts)[1], (0.5, 10 / 3))
        segs = vm.smooth_path([(0, 0), (1, 1), (2, 0)])
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[-1][-2:], (2, 0))


class FormattingTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(vm.fmt_num(None), vm.DASH)
        self.assertEqual(vm.fmt_num(34.87, 1), "34.9")
        self.assertEqual(vm.fmt_rpm(4200), "4 200")

    def test_errors_are_short_and_honest(self):
        self.assertIn("administrator", vm.humanize_error(PrivilegeRequired("x")))
        self.assertTrue(vm.humanize_error(ValidationError("bad")).startswith("Rejected"))
        self.assertTrue(vm.humanize_error(BackendUnavailable("no asusd"))
                        .endswith("no asusd"))

    def test_battery_icon(self):
        self.assertEqual(vm.battery_icon(51, "Discharging"), "battery-level-50-symbolic")
        self.assertEqual(vm.battery_icon(77, "Charging"),
                         "battery-level-80-charging-symbolic")
        self.assertEqual(vm.battery_icon(100, "Full"), "battery-level-100-charged-symbolic")
        self.assertEqual(vm.battery_icon(None, None), "ac-adapter-symbolic")
        self.assertEqual(vm.power_state_text("Charging", 40), "40% · charging")


class PowerLimitTests(unittest.TestCase):
    def test_unavailable_uses_firmware_reason(self):
        caps = {"available": False, "unavailable": {
            "dbus": "asusd reports no PPT values (-1); EnablePptGroup off or unsupported",
            "sysfs": "attributes present but ..."}}
        v = vm.power_limits_view(caps, None)
        self.assertFalse(v.available)
        self.assertIn("EnablePptGroup", v.reason)
        self.assertTrue(v.reason[0].isupper())

    def test_rows_only_for_bounded_limits(self):
        lims = [PowerLimit("ppt_pl1_spl", 80, 5, 115, 80, 1),
                PowerLimit("nv_tgp", None, None, None)]
        v = vm.power_limits_view({"available": True}, lims)
        self.assertTrue(v.available)
        self.assertEqual([r.name for r in v.rows], ["ppt_pl1_spl"])
        self.assertEqual(v.rows[0].title, "CPU sustained power")
        self.assertEqual(vm.power_limits_view({"available": True},
                                               [lims[1]]).available, False)

    def test_snap(self):
        self.assertEqual(vm.snap_to_step(7.4, 5, 115, 5), 5)
        self.assertEqual(vm.snap_to_step(8, 5, 115, 5), 10)
        self.assertEqual(vm.snap_to_step(500, 5, 115, 1), 115)


class FanCurveEditorTests(unittest.TestCase):
    def setUp(self):
        self.m = vm.FanCurveModel(CPU)

    def test_drag_clamps_temp_between_neighbours(self):
        self.assertEqual(self.m.drag(3, 200, 120), (61, 120))  # next temp 62 -> 61
        self.assertEqual(self.m.drag(3, -50, 120)[0], 52)  # prev 51 -> 52
        self.m.to_curve()  # still valid

    def test_drag_keeps_pwm_monotonic(self):
        self.assertEqual(self.m.drag(2, 51, 255)[1], 116)  # next pwm
        self.assertEqual(self.m.drag(2, 51, 0)[1], 53)  # prev pwm
        self.assertEqual(self.m.drag(0, 0, -10), (20, 0))  # global bounds
        self.assertEqual(self.m.drag(7, 500, 999), (110, 255))
        self.m.to_curve()

    def test_end_points_leave_room(self):
        m = vm.FanCurveModel(FanCurve.from_lists("GPU", list(range(20, 28)), [0] * 8))
        t_lo, t_hi, _, _ = m.limits_for(0)
        self.assertEqual((t_lo, t_hi), (20, 20))

    def test_dirty_revert_commit(self):
        self.assertFalse(self.m.dirty)
        self.m.drag(4, 60, 150)
        self.assertTrue(self.m.dirty)
        self.m.revert()
        self.assertFalse(self.m.dirty)
        self.m.enabled = False
        self.assertTrue(self.m.dirty)
        self.m.commit()
        self.assertFalse(self.m.dirty)
        self.assertFalse(self.m.to_curve().enabled)

    def test_hit_testing_and_geometry_roundtrip(self):
        g = vm.CurveGeometry(600, 300)
        for t, p in self.m.points:
            x, y = g.to_px(t, p)
            t2, p2 = g.to_data(x, y)
            self.assertAlmostEqual(t, t2)
            self.assertAlmostEqual(p, p2)
        x, y = g.to_px(62, 158)
        t, p = g.to_data(x + 3, y - 2)
        self.assertEqual(self.m.nearest(t, p, 16, g.scale), 4)
        self.assertIsNone(self.m.nearest(20, 255, 16, g.scale))

    def test_pct(self):
        self.assertEqual(vm.pct_to_pwm(100), 255)
        self.assertEqual(vm.pct_to_pwm(150), 255)
        self.assertAlmostEqual(vm.pwm_to_pct(255), 100)


class BatteryGpuKeyboardTests(unittest.TestCase):
    def test_charge_limit(self):
        self.assertEqual(vm.validate_charge_limit(79.6), 80)
        for bad in (19, 101):
            with self.assertRaises(ValidationError):
                vm.validate_charge_limit(bad)
        self.assertIn("lifespan", vm.charge_limit_hint(60))
        self.assertEqual(vm.health_label(88), "Good")

    def test_gpu_requirements(self):
        self.assertEqual(vm.gpu_switch_requirement(GpuMode.HYBRID, GpuMode.ASUS_MUX_DGPU),
                         "reboot")
        self.assertEqual(vm.gpu_switch_requirement(GpuMode.ASUS_MUX_DGPU, GpuMode.HYBRID),
                         "reboot")
        self.assertEqual(vm.gpu_switch_requirement(GpuMode.HYBRID, GpuMode.INTEGRATED),
                         "logout")
        self.assertIsNone(vm.gpu_switch_requirement(GpuMode.HYBRID, GpuMode.HYBRID))
        self.assertEqual(vm.gpu_action_text(GpuAction.REBOOT, GpuMode.ASUS_MUX_DGPU),
                         "Restart to switch to Ultimate")
        self.assertIsNone(vm.gpu_action_text(None))

    def test_colours_and_effects(self):
        self.assertEqual(vm.hex_to_rgb("#8E2A44"), (142, 42, 68))
        self.assertEqual(vm.hex_to_rgb("fff"), (255, 255, 255))
        self.assertEqual(vm.rgb_to_hex((201, 174, 134)), "#C9AE86")
        with self.assertRaises(ValidationError):
            vm.hex_to_rgb("#12")
        eff = vm.effect_from_rgb_data({"mode": 1, "colour1": [1, 2, 3], "speed": 0xF5})
        self.assertEqual((eff.mode, eff.colour1), (1, (1, 2, 3)))
        self.assertEqual(vm.speed_index(eff.speed), 2)
        static = KbdEffect(0, colour1=(255, 0, 0))
        self.assertEqual(vm.preview_colour(static, 3.0), (1.0, 0.0, 0.0))
        self.assertEqual(vm.preview_colour(static, 3.0, brightness=0), (0.0, 0.0, 0.0))
        a = vm.preview_colour(KbdEffect(2), 0.0)
        b = vm.preview_colour(KbdEffect(2), 2.0)
        self.assertNotEqual(a, b)  # colour cycle moves
        with self.assertRaises(ValidationError):
            KbdEffect(0, colour1=(300, 0, 0)).validate()


class FrameGateTests(unittest.TestCase):
    def test_limits_to_fifteen_fps(self):
        g = vm.FrameGate()
        t, frames = 0.0, 0
        while t < 10.0:  # polled at 240 Hz
            frames += g.due(t)
            t += 1 / 240
        self.assertTrue(140 <= frames <= 152, frames)

    def test_dirty_flag_and_idle_backoff(self):
        g = vm.FrameGate(settle_frames=3)
        self.assertTrue(g.observe((1, 2, 3)))
        self.assertFalse(g.observe((1, 2, 3)))
        self.assertAlmostEqual(g.interval, 1 / 15)
        for _ in range(3):
            g.observe((1, 2, 3))
        self.assertAlmostEqual(g.interval, 0.25)  # settled: 4 Hz
        self.assertTrue(g.observe((9, 9, 9)))
        self.assertAlmostEqual(g.interval, 1 / 15)  # a change wakes it
        g.observe((9, 9, 9))
        g.wake()
        self.assertTrue(g.observe((9, 9, 9)))  # forced redraw

    def test_slow_frames_drop_to_ten_fps(self):
        g = vm.FrameGate()
        for _ in range(10):
            g.add_cost(0.05)
        self.assertAlmostEqual(g.interval, 0.1)
        for _ in range(40):
            g.add_cost(0.002)
        self.assertAlmostEqual(g.interval, 1 / 15)

    def test_only_time_dependent_effects_animate(self):
        self.assertFalse(vm.is_animated(KbdEffect(0)))
        self.assertTrue(all(vm.is_animated(KbdEffect(m)) for m in (1, 2, 3, 10, 12)))
        for m in (0, 4, 5):  # modes whose preview colour ignores t
            e = KbdEffect(m, colour1=(10, 20, 30))
            self.assertEqual(vm.preview_colour(e, 0.0), vm.preview_colour(e, 7.3))


def mutter_state():
    def mode(rate, cur=False, variable=False):
        props = {"is-current": cur}
        if variable:
            props["refresh-rate-mode"] = "variable"
        return (f"1920x1080@{rate}", 1920, 1080, rate, 1.0, [1.0], props)
    builtin = (("eDP-1", "BOE", "x", "0"), [mode(144.0, True), mode(60.0), mode(59.94),
               mode(144.0, variable=True), ("1280x720@60", 1280, 720, 60.0, 1.0, [1.0], {})],
               {"is-builtin": True, "display-name": "Built-in display"})
    ext = (("HDMI-1", "DEL", "y", "1"), [("2560x1440@60", 2560, 1440, 60.0, 1.0, [1.0],
                                         {"is-current": True})], {"display-name": "Dell"})
    logical = [(0, 0, 1.0, 0, True, [builtin[0]], {}), (1920, 0, 1.0, 0, False, [ext[0]], {})]
    return (7, [builtin, ext], logical, {})


class DisplayConfigTests(unittest.TestCase):
    def test_parse_and_options(self):
        mons = vm.parse_display_state(mutter_state())
        b = mons[0]
        self.assertTrue(b.builtin)
        self.assertTrue(b.vrr_supported)
        self.assertEqual([m.label for m in b.refresh_options()], ["144 Hz", "60 Hz", "59.94 Hz"])
        self.assertEqual(b.current.refresh, 144.0)

    def test_apply_config_changes_only_one_monitor(self):
        cfg = vm.build_apply_config(mutter_state(), "eDP-1", "1920x1080@60.0")
        self.assertEqual(cfg[0][5], [("eDP-1", "1920x1080@60.0", {})])
        self.assertEqual(cfg[1][5], [("HDMI-1", "2560x1440@60", {})])
        self.assertEqual(cfg[1][:5], (1920, 0, 1.0, 0, False))
        with self.assertRaises(ValidationError):
            vm.build_apply_config(mutter_state(), "DP-9", "x")


class ControllerUiOperations(unittest.TestCase):
    """The controller calls the UI uses, against the fake tree only."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_fake_tree(Path(self._tmp.name) / "root")
        self.ctl = Controller([FakeBackend(self.root)])

    def test_fan_curve_write_and_reset(self):
        curves, _ = self.ctl.get_fan_curves()
        m = vm.FanCurveModel(curves["CPU"])
        m.drag(4, 60, 140)
        self.ctl.set_fan_curve(Profile.BALANCED, m.to_curve())
        again, _ = self.ctl.get_fan_curves()
        self.assertEqual((again["CPU"].points[4].temp, again["CPU"].points[4].pwm), (60, 140))
        with self.assertRaises(BackendUnavailable):  # sysfs: active profile only
            self.ctl.get_fan_curves(Profile.QUIET)
        self.ctl.reset_fan_curves(Profile.BALANCED)
        self.assertEqual(self.ctl.get_fan_curves()[0]["CPU"].points[4].temp, 62)
        bad = FanCurve.from_lists("CPU", [50] * 8, [0] * 8)
        with self.assertRaises(ValidationError):
            self.ctl.set_fan_curve(Profile.BALANCED, bad)

    def test_gpu_switch_is_pending_until_reboot(self):
        res = self.ctl.set_gpu_mode(GpuMode.ASUS_MUX_DGPU)
        self.assertIs(res.value, GpuAction.REBOOT)
        st, _ = self.ctl.get_gpu_status()
        self.assertIs(st.mode, GpuMode.HYBRID)
        self.assertIs(st.pending_mode, GpuMode.ASUS_MUX_DGPU)
        dry = self.ctl.set_gpu_mode(GpuMode.INTEGRATED, dry_run=True)
        self.assertFalse(dry.applied)

    def test_keyboard_attribute_and_battery(self):
        self.ctl.set_kbd_effect(KbdEffect(1, colour1=(1, 2, 3)))
        self.assertEqual(self.ctl.get_kbd_rgb()[0]["mode_data"]["colour1"], [1, 2, 3])
        self.ctl.set_kbd_brightness(1)
        self.assertEqual(self.ctl.get_kbd_brightness()[0], (1, 3))
        self.ctl.set_attribute("panel_overdrive", 0)
        od = next(a for a in self.ctl.get_attributes()[0] if a.name == "panel_overdrive")
        self.assertEqual(od.current, "0")
        with self.assertRaises(ValidationError):
            self.ctl.set_attribute("panel_overdrive", 7)
        self.ctl.one_shot_full_charge()
        self.assertEqual(self.ctl.get_battery_threshold()[0], 100)

    def test_suspended_dgpu_is_not_polled(self):
        root = build_fake_tree(Path(self._tmp.name) / "r2", dgpu_runtime="suspended")
        t = FakeBackend(root).get_telemetry()
        self.assertEqual(t.gpu_runtime, "suspended")
        self.assertIsNone(t.gpu_temp_c)


if __name__ == "__main__":
    unittest.main()

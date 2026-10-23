import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tuf_control.backends.fake import FakeBackend, build_fake_tree
from tuf_control.controller import FAKE_ROOT_ENV, Controller
from tuf_control.core import ValidationError
from tuf_control.effects import audio as A
from tuf_control.effects.ambient import (
    PortalDenied,
    ScreenCastFlow,
    ScreenColourSource,
    TokenStore,
    average_rgb,
)
from tuf_control.effects.base import EffectStack, Inputs
from tuf_control.effects.colour import Smoother, gradient, hex_to_rgb
from tuf_control.effects.notify import NotificationQueue, parse_notify
from tuf_control.effects.plugins import (
    AccentEffect,
    AudioEffect,
    NotificationFlash,
    ProfileEffect,
    TemperatureEffect,
)
from tuf_control.effects.registry import EFFECTS, coerce_params, parse_kv
from tuf_control.effects.runner import RESTORE_CONFIG, EffectsRunner, KbdWriter
from tuf_control.effects.theme import theme_accent, theme_variant


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def tone(freq, n, rate=A.RATE, amp=0.5):
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(n)]


class ColourTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(hex_to_rgb("#8E2A44"), (142, 42, 68))
        self.assertEqual(hex_to_rgb("fff"), (255, 255, 255))
        with self.assertRaises(ValidationError):
            hex_to_rgb("#12")
        self.assertEqual(gradient([(0, 0, 0), (100, 100, 100), (200, 0, 0)], 0.75),
                         (150, 50, 50))
        s = Smoother(0.5)
        self.assertEqual(s.step((0, 0, 0), 0.1), (0, 0, 0))
        mid = s.step((200, 0, 0), 0.5)
        self.assertTrue(110 < mid[0] < 140)  # 1 - e^-1 of the way
        for _ in range(40):
            last = s.step((200, 0, 0), 0.1)
        self.assertEqual(last, (200, 0, 0))


class PluginTests(unittest.TestCase):
    def test_params_schema_and_coercion(self):
        self.assertEqual(coerce_params("audio", parse_kv(["gain=2", "band=mid"])),
                         {"gain": 2.0, "band": "mid"})
        for bad in (["gain=9"], ["band=sub"], ["nope=1"], ["peak=#12"]):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                coerce_params("audio", parse_kv(bad))
        self.assertEqual(coerce_params("notifications", {"apps": "a, b"})["apps"], ["a", "b"])
        for cls in EFFECTS.values():
            sch = cls.schema()
            self.assertTrue(sch["title"] and sch["description"])
            cls({}, Inputs()).tick(0.05)  # every effect runs with no inputs at all

    def test_temperature_gradient(self):
        tel = SimpleNamespace(cpu_temp_c=45.0, gpu_temp_c=None)
        e = TemperatureEffect({"low": 40, "high": 90}, Inputs(telemetry=lambda: tel))
        cool = e.tick(0.1)
        self.assertEqual(cool, gradient([hex_to_rgb("#2f6fdf"), hex_to_rgb("#c9ae86"),
                                         hex_to_rgb("#e0182d")], 0.1))
        tel.cpu_temp_c = 95.0
        for _ in range(60):
            hot = e.tick(0.1)
        self.assertEqual(hot, hex_to_rgb("#e0182d"))
        tel.gpu_temp_c = 50.0
        e2 = TemperatureEffect({"source": "gpu"}, Inputs(telemetry=lambda: tel))
        self.assertLess(e2.tick(0.1)[0], 120)

    def test_profile_and_accent(self):
        prof = ["quiet"]
        e = ProfileEffect({}, Inputs(profile=lambda: prof[0]))
        self.assertEqual(e.tick(0), hex_to_rgb("#2f6fdf"))
        prof[0] = "performance"
        self.assertEqual(e.tick(0), hex_to_rgb("#8e2a44"))
        prof[0] = None
        self.assertIsNone(e.tick(0))
        a = AccentEffect({"follow_theme": True}, Inputs(theme_accent=lambda: (163, 40, 58)))
        self.assertEqual(a.tick(0.1), (163, 40, 58))
        b = AccentEffect({"breathe": True, "period": 2}, Inputs())
        lows = [b.tick(0.1)[0] for _ in range(20)]
        self.assertLess(min(lows), 40)
        self.assertGreater(max(lows), 130)

    def test_notification_overlay_wins_then_passes_through(self):
        q = NotificationQueue()
        inputs = Inputs(notifications=q, theme_accent=lambda: None)
        stack = EffectStack([(0, AccentEffect({"colour": "#000080"}, inputs)),
                             (100, NotificationFlash({"duration_ms": 400, "flashes": 1,
                                                      "apps": ["Firefox"]}, inputs))])
        self.assertEqual(stack.tick(0.05), ((0, 0, 128), "accent"))
        q.push({"app": "Slack"})  # filtered out
        self.assertEqual(stack.tick(0.05)[1], "accent")
        q.push({"app": "firefox"})
        stack.tick(0.05)
        rgb, who = stack.tick(0.15)
        self.assertEqual(who, "notifications")
        self.assertGreater(rgb[0], 200)
        for _ in range(10):
            rgb, who = stack.tick(0.05)
        self.assertEqual(who, "accent")

    def test_parse_notify(self):
        self.assertEqual(parse_notify(("App", 0, "", "Hi", "b", [], {"urgency": 2}, -1)),
                         {"app": "App", "summary": "Hi", "urgency": 2})
        self.assertEqual(parse_notify(("", 0, "", "Hi", "b", [],
                                       {"desktop-entry": "org.x"}, -1))["app"], "org.x")
        self.assertIsNone(parse_notify(("too", "short")))

    def test_theme_accent(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            env = {k: os.environ.pop(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")
                   if k in os.environ}
            self.addCleanup(os.environ.update, env)
            self.assertEqual(theme_variant(home, lambda: "'Merlot-Day'"), "merlot-day")
            self.assertIsNone(theme_accent(home, lambda: "'Yaru'"))
            (home / ".local/state/bordeaux-gnome").mkdir(parents=True)
            (home / ".local/state/bordeaux-gnome/variant").write_text("oxblood-noir\n")
            self.assertEqual(theme_accent(home, lambda: None), (0xA3, 0x28, 0x3A))
            (home / ".config/bordeaux-gnome").mkdir(parents=True)
            (home / ".config/bordeaux-gnome/variant").write_text("bordeaux")
            self.assertEqual(theme_accent(home, lambda: None), (0x8E, 0x2A, 0x44))


class AudioTests(unittest.TestCase):
    def test_bands_numpy_and_goertzel_agree(self):
        for freq, band in ((80, "bass"), (900, "mid"), (3600, "treble")):
            blk = tone(freq, A.BLOCK)
            for use_np in ((True, False) if A._np is not None else (False,)):
                e = A.band_energies(blk, use_numpy=use_np)
                with self.subTest(freq=freq, numpy=use_np):
                    self.assertEqual(max(e, key=e.get), band)

    def test_envelope_beats_and_silence(self):
        an = A.AudioAnalyzer(use_numpy=False)
        an.feed(tone(80, A.BLOCK * 4))
        loud = an.level("bass")
        self.assertGreater(loud, 0.8)
        self.assertLess(an.level("treble"), 0.5)
        an.feed([0.0] * A.BLOCK * 20)
        self.assertLess(an.level("bass"), 0.15)
        data = b"".join(int(v * 32767).to_bytes(2, "little", signed=True)
                        for v in tone(80, A.BLOCK * 2))
        an.feed_s16le(data)
        self.assertGreater(an.level("full"), 0.5)

    def test_capture_command_and_process(self):
        self.assertIn("--device=@DEFAULT_MONITOR@",
                      A.capture_command(lambda c: "/usr/bin/" + c if c == "parec" else None))
        self.assertEqual(A.capture_command(lambda c: "/x" if c == "pw-record" else None)[0],
                         "pw-record")
        self.assertIsNone(A.capture_command(lambda c: None))
        # a fake capture process: Python writing a WAV header + an 80 Hz tone to stdout
        import sys
        script = ("import sys,math;o=sys.stdout.buffer;o.write(b'RIFF'+bytes(40));"
                  f"o.write(b''.join(int(16000*math.sin(2*math.pi*80*i/{A.RATE}))"
                  ".to_bytes(2,'little',signed=True) for i in range(6000)));o.flush()")
        cap = A.AudioCapture(A.AudioAnalyzer(use_numpy=False), [sys.executable, "-c", script])
        cap.start()
        cap.thread.join(5)
        self.assertGreater(cap.level("bass"), 0.5)
        cap.stop()
        eff = AudioEffect({"gain": 1.0}, Inputs(audio=cap))
        self.assertGreater(eff.tick(0.05)[0], 60)


class FakeBus:
    def __init__(self, deny=False, token_out="tok-2"):
        self.calls, self.deny, self.token_out = [], deny, token_out

    def request(self, method, args, options):
        self.calls.append((method, args, dict(options)))
        if method == "CreateSession":
            return {"session_handle": "/org/freedesktop/portal/desktop/session/1_1/x"}
        if method == "Start":
            if self.deny:
                raise PortalDenied("no")
            return {"streams": [(42, {})], "restore_token": self.token_out}
        return {}

    def open_pipewire_remote(self, session):
        return 7

    def close_session(self, session):
        self.calls.append(("Close", [session], {}))


class AmbientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tok = Path(tmp.name) / "screencast-token"

    def test_average_skips_dark_pixels(self):
        frame = bytes([0, 0, 0] * 8 + [200, 40, 40] * 8)  # 4x4: top half black
        self.assertEqual(average_rgb(frame, 4, 4, boost=1.0), (200, 40, 40))
        self.assertIsNone(average_rgb(bytes(48), 4, 4))
        padded = bytes([10, 200, 10] * 2 + [9, 9]) * 2  # stride 8 for width 2
        self.assertEqual(average_rgb(padded, 2, 2, stride=8, boost=1.0), (10, 200, 10))

    def test_portal_flow_persists_restore_token(self):
        bus = FakeBus()
        fd, node = ScreenCastFlow(bus, TokenStore(self.tok)).open()
        self.assertEqual((fd, node), (7, 42))
        sel = bus.calls[1][2]
        self.assertEqual((sel["types"], sel["persist_mode"]), (1, 2))
        self.assertNotIn("restore_token", sel)
        self.assertEqual(self.tok.read_text().strip(), "tok-2")
        bus2 = FakeBus(token_out="tok-3")
        ScreenCastFlow(bus2, TokenStore(self.tok)).open()
        self.assertEqual(bus2.calls[1][2]["restore_token"], "tok-2")  # no dialog this time
        self.assertEqual(self.tok.read_text().strip(), "tok-3")

    def test_denied_clears_token_and_reports(self):
        TokenStore(self.tok).set("old")
        src = ScreenColourSource(self.tok, bus_factory=lambda: FakeBus(deny=True))
        src.run_sync()
        self.assertEqual(src.status, "denied")
        self.assertFalse(self.tok.exists())

    def test_frames_to_colour(self):
        frames = [None, (bytes([0, 0, 255] * 16), 4, 4, 12)]
        src = ScreenColourSource(self.tok, bus_factory=FakeBus,
                                 frames_factory=lambda fd, node: iter(frames), boost=1.0)
        src.run_sync()
        self.assertEqual(src.latest(), (0, 0, 255))
        self.assertEqual(src.status, "idle")  # stream ended
        bad = ScreenColourSource(self.tok, bus_factory=lambda: (_ for _ in ()).throw(
            OSError("no session bus")))
        bad.run_sync()
        self.assertEqual(bad.status, "unavailable")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = build_fake_tree(Path(tmp.name) / "root")
        os.environ[FAKE_ROOT_ENV] = str(self.root)
        self.addCleanup(os.environ.pop, FAKE_ROOT_ENV, None)
        self.ctl = Controller([FakeBackend(self.root)])
        self.clock = Clock()

    def aura(self):
        return json.loads((self.root / "fake/aura.json").read_text())

    def test_rate_limit_delta_and_backlight_pause(self):
        w = KbdWriter(self.ctl, max_hz=5, min_delta=4, clock=self.clock)
        w.save()
        self.assertTrue(w.push((10, 10, 10)))
        self.clock.t += 0.1
        self.assertFalse(w.push((90, 10, 10)))   # 5 Hz limit
        self.clock.t += 0.2
        self.assertFalse(w.push((11, 10, 10)))   # too small a change
        self.assertTrue(w.push((90, 10, 10)))
        self.assertEqual(self.aura()["colour1"], [90, 10, 10])
        self.assertEqual(self.aura()["colour2"], [201, 174, 134])  # rest of the mode kept
        self.ctl.set_kbd_brightness(0)
        self.clock.t += 3
        self.assertFalse(w.push((200, 0, 0)))    # backlight off: no writes
        self.assertTrue(w.backlight_off)
        w.restore()
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])

    def test_runner_lifecycle_override_and_restore(self):
        tel = SimpleNamespace(cpu_temp_c=95.0, gpu_temp_c=None)
        r = EffectsRunner(self.ctl, clock=self.clock,
                          sources={"telemetry": lambda: tel, "profile": lambda: "quiet",
                                   "theme": lambda: None})
        r.configure({"active": "temperature", "params": {}, "fps": 30, "max_write_hz": 10,
                     "smoothing_s": 0, "notifications": {"enabled": False}})
        for _ in range(30):
            self.clock.t += 0.1
            r.step(0.1)
        self.assertEqual(self.aura()["colour1"], list(hex_to_rgb("#e0182d")))
        self.assertEqual(r.frame_rate(), 8)
        self.assertIn("dry run", r.set_override({"name": "profile", "params": {}}, True))
        r.set_override({"name": "profile", "params": {}})
        self.clock.t += 0.5
        r.step(0.1)
        self.assertEqual(self.aura()["colour1"], list(hex_to_rgb("#2f6fdf")))
        self.assertTrue(r.status()["override"])
        r.set_override({"name": None, "params": {}})  # automation: effects off
        r.step(0.1)
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])  # hardware mode restored
        self.assertFalse(r.status()["running"])
        r.set_override(RESTORE_CONFIG)
        self.clock.t += 0.5
        r.step(0.1)
        self.assertEqual(r.status()["effect"], "temperature")
        r.shutdown()
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])

    def test_write_failures_back_off(self):
        class Broken(Controller):
            def set_kbd_effect(self, effect, dry_run=False):
                from tuf_control.core import BackendUnavailable
                raise BackendUnavailable("asusd gone")
        ctl = Broken([FakeBackend(self.root)])
        r = EffectsRunner(ctl, clock=self.clock, sources={"theme": lambda: None})
        r.configure({"active": "accent", "params": {}, "smoothing_s": 0,
                     "notifications": {"enabled": False}})
        r.step(0.1)
        self.assertIn("asusd gone", r.status()["error"])
        self.assertGreater(r._paused_until, self.clock.t)

    def test_threaded_run_stops(self):
        import threading
        r = EffectsRunner(self.ctl, sources={"theme": lambda: None})
        r.configure({"active": "accent", "params": {"accent": {"colour": "#00ff00"}},
                     "smoothing_s": 0, "notifications": {"enabled": False}})
        stop = threading.Event()
        t = threading.Thread(target=r.run, args=(stop,))
        t.start()
        time.sleep(0.4)
        stop.set()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.aura()["colour1"], [0, 255, 0])
        r.shutdown()
        self.assertEqual(self.aura()["colour1"], [142, 42, 68])


@unittest.skipUnless(shutil.which("dbus-run-session") and shutil.which("gdbus"),
                     "needs dbus-run-session and gdbus")
class NotificationMonitorBusTest(unittest.TestCase):
    def test_become_monitor_on_private_bus(self):
        code = (
            "import subprocess,time\n"
            "from tuf_control.effects.notify import NotificationMonitor\n"
            "m=NotificationMonitor();m.start();assert m.error is None, m.error\n"
            "subprocess.run(['gdbus','call','--session','--dest',"
            "'org.freedesktop.Notifications','--object-path','/org/freedesktop/Notifications',"
            "'--method','org.freedesktop.Notifications.Notify','Mail','0','','New mail','b',"
            "'[]','{}','5000','--timeout','1'],capture_output=True)\n"
            "time.sleep(0.4);ev=m.drain();m.stop();print(ev[0]['app'])\n")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        out = subprocess.run(["dbus-run-session", "--", "python3", "-c", code], env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "Mail", out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()

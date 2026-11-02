# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The GSettings wrapper behind the Touchpad & gestures page, without a display and
without the real settings: a temporary HOME with the keyfile backend, the extension's
schema compiled into a temporary directory, and explicit schema sources for the
missing cases. The owner's dconf is never touched and the page is never instantiated."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="tuf-gestures-test-"))
_HOME = _TMP / "home"
_CONFIG = _HOME / ".config"
_DATA = _HOME / ".local" / "share"
for _d in (_CONFIG, _DATA):
    _d.mkdir(parents=True)
_OLD_ENV = {k: os.environ.get(k) for k in
            ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "GSETTINGS_BACKEND")}
os.environ["HOME"] = str(_HOME)
os.environ["XDG_CONFIG_HOME"] = str(_CONFIG)
os.environ["XDG_DATA_HOME"] = str(_DATA)
os.environ["GSETTINGS_BACKEND"] = "keyfile"  # the real dconf is never touched

# the extension's schema, compiled the way install.sh does it (test_install.sh:114-133)
_EXT_XML = (_ROOT / "extension" / "tuf-control-quick@hamza-gouda" / "schemas" /
            "org.gnome.shell.extensions.tuf-control-quick.gschema.xml")
_EXT_SCHEMAS = _TMP / "ext-schemas"
_OTHER_SCHEMAS = _TMP / "other-schemas"  # schemas, but not the extension's: "not installed"
for _d in (_EXT_SCHEMAS, _OTHER_SCHEMAS):
    _d.mkdir()
(_OTHER_SCHEMAS / "com.example.other.gschema.xml").write_text(
    '<schemalist><schema id="com.example.other" path="/com/example/other/">'
    '<key name="on" type="b"><default>false</default></key></schema></schemalist>')
shutil.copy(_EXT_XML, _EXT_SCHEMAS)
for _d in (_EXT_SCHEMAS, _OTHER_SCHEMAS):
    subprocess.run(["glib-compile-schemas", str(_d)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

from gi.repository import Gio  # noqa: E402  (only once the environment is in place)

from tuf_control import touchpad  # noqa: E402

TOUCHPAD_KEYS = ("send-events", "tap-to-click", "natural-scroll", "speed", "click-method")
GESTURE_KEYS = touchpad.GESTURE_KEYS + ("gesture-enabled", "gesture-launch-app")
OWN_APP_ID = "io.github.hamzagouda.TufControl.desktop"


def ext_source():
    """A schema source with the extension's schema, as an explicit stand-in for
    GSETTINGS_SCHEMA_DIR (keeps the default source out of the picture)."""
    return Gio.SettingsSchemaSource.new_from_directory(str(_EXT_SCHEMAS), None, False)


def other_source():
    """A schema source that has schemas but not the extension's: how a machine without
    tuf-control-quick looks."""
    return Gio.SettingsSchemaSource.new_from_directory(str(_OTHER_SCHEMAS), None, False)


def reset_all():
    """Back to schema defaults: the keyfile backend is per process, so tests reset
    every key they touch instead of relying on a fresh store."""
    pad = touchpad.TouchpadSettings()
    for k in TOUCHPAD_KEYS:
        pad.settings.reset(k)
    gest = touchpad.GestureSettings(source=ext_source())
    for k in GESTURE_KEYS:
        gest.settings.reset(k)


def tearDownModule():
    for k, v in _OLD_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_TMP, ignore_errors=True)


class ExtensionPresence(unittest.TestCase):
    """The page's 'installed / not installed' state row rests on this."""

    def test_found_with_an_explicit_source(self):
        gest = touchpad.GestureSettings(source=ext_source())
        self.assertTrue(gest.available)
        self.assertIsNotNone(gest.settings)

    def test_missing_from_a_source_without_the_schema(self):
        gest = touchpad.GestureSettings(source=other_source())
        self.assertFalse(gest.available)
        self.assertIsNone(gest.settings)
        for call in (gest.enabled, gest.launch_app,
                     lambda: gest.set_enabled(True),
                     lambda: gest.set_launch_app("x.desktop"),
                     lambda: gest.binding("gesture-pinch-in"),
                     lambda: gest.set_binding("gesture-pinch-in", "overview")):
            self.assertRaises(touchpad.SchemaMissing, call)

    def test_found_in_the_extension_directory(self):
        """The layout install.sh creates: the schema compiled in place under the
        extension, where the default source never looks (install.sh:163-172)."""
        ext_dir = _DATA / "gnome-shell" / "extensions" / touchpad.EXTENSION_UUID
        shutil.copytree(_EXT_SCHEMAS, ext_dir / "schemas", dirs_exist_ok=True)
        try:
            gest = touchpad.GestureSettings()
            self.assertTrue(gest.available)
        finally:
            shutil.rmtree(ext_dir)
        self.assertFalse(touchpad.GestureSettings().available)  # gone again: not installed


class GestureKeys(unittest.TestCase):
    def setUp(self):
        reset_all()
        self.gest = touchpad.GestureSettings(source=ext_source())

    def test_defaults_follow_the_schema(self):
        self.assertFalse(self.gest.enabled())
        for key in touchpad.GESTURE_KEYS:
            self.assertEqual(self.gest.binding(key), "none", key)
        self.assertEqual(self.gest.launch_app(), "")
        self.assertTrue(self.gest.hides_power_mode())  # our tile replaces GNOME's

    def test_writes_read_back_and_reach_the_store(self):
        self.gest.set_enabled(True)
        self.assertTrue(self.gest.enabled())
        self.gest.set_binding("gesture-swipe4-up", "overview")
        self.assertEqual(self.gest.binding("gesture-swipe4-up"), "overview")
        self.gest.set_binding("gesture-pinch-out", "launch-app")
        self.assertEqual(self.gest.binding("gesture-pinch-out"), "launch-app")
        self.gest.set_launch_app(OWN_APP_ID)
        self.assertEqual(self.gest.launch_app(), OWN_APP_ID)
        self.gest.set_launch_app("")  # the page's "None" entry
        self.assertEqual(self.gest.launch_app(), "")
        self.gest.set_hides_power_mode(False)
        self.assertFalse(self.gest.hides_power_mode())
        self.gest.set_hides_power_mode(True)
        # a second, independent Settings object reads the same store
        again = touchpad.GestureSettings(source=ext_source())
        self.assertEqual(again.binding("gesture-swipe4-up"), "overview")
        # and the keyfile backend wrote to the temporary config, not the real one
        keyfile = _CONFIG / "glib-2.0" / "settings" / "keyfile"
        self.assertTrue(keyfile.is_file())
        self.assertIn("gesture-swipe4-up", keyfile.read_text())

    def test_rejects_unknown_keys_and_actions(self):
        self.assertRaises(ValueError, self.gest.set_binding, "gesture-other", "none")
        self.assertRaises(ValueError, self.gest.set_binding, "gesture-pinch-in", "explode")
        self.assertRaises(ValueError, self.gest.binding, "not-a-key")
        self.assertEqual(self.gest.binding("gesture-pinch-in"), "none")  # writes left alone

    def test_every_gesture_key_is_a_schema_key(self):
        schema = ext_source().lookup(touchpad.EXTENSION_SCHEMA, False)
        for key in touchpad.GESTURE_KEYS:
            self.assertIsNotNone(schema.get_key(key), key)

    def test_hide_power_mode_when_the_installed_schema_is_older(self):
        """The app can be newer than the installed extension: its compiled schema then
        lacks hide-power-mode, and touching a missing key is a hard abort — the wrapper
        must answer None / refuse instead (the page hides the row)."""
        old = _TMP / "old-ext-schemas"
        old.mkdir(exist_ok=True)
        (old / "org.gnome.shell.extensions.tuf-control-quick.gschema.xml").write_text(
            _EXT_XML.read_text().split("    <key name=\"hide-power-mode\"")[0] +
            "  </schema>\n</schemalist>\n")
        subprocess.run(["glib-compile-schemas", str(old)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        src = Gio.SettingsSchemaSource.new_from_directory(str(old), None, False)
        gest = touchpad.GestureSettings(source=src)
        self.assertTrue(gest.available)
        self.assertIsNone(gest.hides_power_mode())
        self.assertFalse(gest.set_hides_power_mode(True))


class TouchpadKeys(unittest.TestCase):
    def setUp(self):
        reset_all()
        self.pad = touchpad.TouchpadSettings()

    def test_defaults_follow_the_schema(self):
        for key, getter in (("send-events", self.pad.send_events),
                            ("tap-to-click", self.pad.tap_to_click),
                            ("natural-scroll", self.pad.natural_scroll),
                            ("speed", self.pad.speed),
                            ("click-method", self.pad.click_method)):
            self.assertEqual(getter(), self.pad.settings.get_default_value(key).unpack(), key)

    def test_writes_read_back(self):
        self.pad.set_send_events("disabled-on-external-mouse")
        self.assertEqual(self.pad.send_events(), "disabled-on-external-mouse")
        self.pad.set_send_events("disabled")
        self.assertEqual(self.pad.send_events(), "disabled")
        self.pad.set_send_events("enabled")
        self.assertEqual(self.pad.send_events(), "enabled")
        self.pad.set_tap_to_click(True)
        self.assertTrue(self.pad.tap_to_click())
        self.pad.set_natural_scroll(True)
        self.assertTrue(self.pad.natural_scroll())
        self.pad.set_speed(-0.5)
        self.assertAlmostEqual(self.pad.speed(), -0.5)
        self.pad.set_speed(0.0)
        self.assertEqual(self.pad.speed(), 0.0)
        self.pad.set_click_method("areas")
        self.assertEqual(self.pad.click_method(), "areas")

    def test_click_method_choices_come_from_the_schema(self):
        choices = self.pad.click_method_choices()
        self.assertIn("default", choices)
        self.assertIn("areas", choices)
        for choice in choices:  # whatever the installed schema allows, the wrapper writes
            self.pad.set_click_method(choice)

    def test_rejects_unknown_values(self):
        self.assertRaises(ValueError, self.pad.set_send_events, "when-idle")
        self.assertRaises(ValueError, self.pad.set_speed, 1.5)
        self.assertRaises(ValueError, self.pad.set_speed, -1.5)
        self.assertRaises(ValueError, self.pad.set_speed, "fast")
        # GNOME 46 has default/none/areas/fingers; "buttons" was the ask's shorthand
        self.assertRaises(ValueError, self.pad.set_click_method, "buttons")
        self.assertEqual(self.pad.click_method(), "default")  # writes left alone

    def test_missing_schema_raises_on_construction(self):
        self.assertRaises(touchpad.SchemaMissing, touchpad.TouchpadSettings,
                          source=other_source())


if __name__ == "__main__":
    unittest.main()

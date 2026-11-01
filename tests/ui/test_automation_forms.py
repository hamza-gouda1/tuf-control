# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display-free logic behind the Automation page and the Effects section."""
import unittest
from types import SimpleNamespace

from tuf_control.automation import forms
from tuf_control.automation.rules import Rule
from tuf_control.effects.preview import EffectPreview


class FormsTests(unittest.TestCase):
    def test_every_trigger_builds_a_valid_rule(self):
        for key, _title, _icon in forms.TRIGGERS:
            trig = forms.build_trigger(key, apps="steam, Hades.exe\ngamescope",
                                       days=["sat", "sun"], profile="quiet")
            acts = [forms.default_action(k) for k in forms.ACTIONS
                    if not (trig["type"] == "profile" and k == "profile")]
            r = Rule.from_dict({"name": key, "trigger": trig, "actions": acts})
            self.assertEqual(forms.trigger_key(r.trigger.to_dict()), key)
            self.assertTrue(forms.trigger_icon(trig).endswith("-symbolic"))
        self.assertEqual(forms.build_trigger("app", apps="a,, b")["names"], ["a", "b"])

    def test_presets_valid_and_filtered(self):
        for p in forms.PRESETS:
            Rule.from_dict(p)
        have = [Rule.from_dict(forms.PRESETS[1])]
        names = [p["name"] for p in forms.preset_rules(have)]
        self.assertNotIn(forms.PRESETS[1]["name"], names)
        self.assertEqual(forms.unique_id("Games", {"games", "games-2"}), "games-3")

    def test_log_line(self):
        title, sub = forms.log_line({"time": "2026-10-23T15:34:02", "kind": "apply",
                                     "rule": "low", "detail": "set profile quiet via fake",
                                     "dry_run": True}, {"low": "Low battery"})
        self.assertEqual(title, "Dry run: set profile quiet via fake")
        self.assertEqual(sub, "15:34 · Applied · Low battery")


class PreviewTests(unittest.TestCase):
    def test_preview_runs_every_effect_without_hardware(self):
        tel = SimpleNamespace(cpu_temp_c=70.0, gpu_temp_c=None)
        pv = EffectPreview(telemetry=lambda: tel, profile=lambda: "balanced")
        for name in ("temperature", "audio", "ambient", "profile", "accent"):
            pv.configure(name, {}, {"enabled": False})
            seen = {pv.tick(0.05) for _ in range(40)}
            self.assertNotIn(None, seen, name)
        pv.configure("audio", {}, {})
        levels = {pv.tick(0.05)[0] for _ in range(30)}
        self.assertGreater(max(levels) - min(levels), 40)  # the synthetic beat pulses
        pv.configure("accent", {"colour": "#000000"},
                     {"enabled": True, "colour": "#ffffff", "duration_ms": 600, "flashes": 1})
        for _ in range(10):
            pv.tick(0.05)
        pv.flash()
        bright = max(pv.tick(0.05)[0] for _ in range(8))
        self.assertGreater(bright, 150)
        pv.configure(None, {})
        self.assertEqual(pv.tick(0.05), pv.colour)  # holds the last colour when off


if __name__ == "__main__":
    unittest.main()

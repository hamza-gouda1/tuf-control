# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Software keyboard effects for single-zone RGB keyboards, run by `asusiusd`.

base     - Effect / Param / Inputs / EffectStack (the plugin interface)
plugins  - temperature, audio, ambient, profile, accent, notification flash
runner   - stack -> smoothing -> rate-limited Controller writes, restore on stop
audio    - monitor-source capture + numpy FFT or pure-Python Goertzel bands
ambient  - xdg-desktop-portal ScreenCast + GStreamer frame average
notify   - org.freedesktop.Notifications via BecomeMonitor
theme    - Bordeaux GNOME accent lookup (read-only)
"""

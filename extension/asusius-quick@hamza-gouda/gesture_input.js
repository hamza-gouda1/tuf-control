// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {DesktopDrag} from './drag_desktop.js';
import {AppDrag} from './drag_switcher.js';

const MEDIA_KEYS = {
    'volume-up': Clutter.KEY_AudioRaiseVolume,
    'volume-down': Clutter.KEY_AudioLowerVolume,
    'play-pause': Clutter.KEY_AudioPlay,
    'media-next': Clutter.KEY_AudioNext,
    'media-prev': Clutter.KEY_AudioPrev,
};

/** Bind pure gesture state to the existing Shell actors and input devices. */
export function createGestureInput(main) {
    const focus = () => global.display.focus_window;
    const actor = window => window.get_compositor_private();
    const windows = () => global.workspace_manager.get_active_workspace().list_windows();
    const switchable = new Set([Meta.WindowType.NORMAL, Meta.WindowType.DIALOG, Meta.WindowType.MODAL_DIALOG]);
    const eligible = window => switchable.has(window.get_window_type()) && !window.skip_taskbar;
    const activeMonitorIndex = () => focus()?.get_monitor() ?? main.layoutManager.primaryIndex;
    const monitor = () => main.layoutManager.monitors[activeMonitorIndex()] ?? main.layoutManager.primaryMonitor;
    const tracker = Shell.WindowTracker.get_default();
    const appWindows = () => {
        const focused = focus();
        const activeMonitor = activeMonitorIndex();
        const byApp = new Map();
        for (const window of windows().sort((a, b) => a.get_stable_sequence() - b.get_stable_sequence())) {
            if (!eligible(window) || window.get_monitor() !== activeMonitor)
                continue;
            const app = tracker.get_window_app(window)?.get_id() ?? window;
            if (window === focused || !byApp.has(app))
                byApp.set(app, window);
        }
        return [...byApp.values()];
    };
    const shared = {
        focus,
        actor,
        activate: window => main.activateWindow(window, global.get_current_time()),
        animationsEnabled: () => St.Settings.get().enable_animations,
        skipEffect: windowActor => main.wm.skipNextEffect(windowActor),
        // Always use onStopped (runs on interruption as well as completion) so interrupted eases cannot wedge drags.
        settle: (windowActor, properties, done) => windowActor.ease({...properties,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC, onStopped: done}),
    };
    const desktop = new DesktopDrag({...shared, windows,
        eligible: window => eligible(window) && window.can_minimize() && !window.is_on_all_workspaces(),
        tuckOffset: (window, windowActor) => {
            const box = main.layoutManager.monitors[window.get_monitor()] ?? main.layoutManager.primaryMonitor;
            const [px, py] = windowActor.get_pivot_point();
            return {
                x: box.x + box.width / 2 - (windowActor.x + windowActor.translation_x + px * windowActor.width),
                y: box.y + box.height - 48 - (windowActor.y + windowActor.translation_y + py * windowActor.height),
            };
        },
    });
    const group = global.window_group;
    const clipActors = (...actors) => {
        const monitors = main.layoutManager.monitors;
        const box = monitors.length > 1 ? monitor() : null;
        if (!box)
            return null;
        const tracked = actors.map(winActor => {
            const hadClip = winActor.has_clip?.() ?? false;
            const clip = hadClip ? winActor.get_clip() : null;
            const rect = Array.isArray(clip) && clip.length === 4 ? clip : null;
            const sync = () => {
                const sx = winActor.scale_x || 1;
                const sy = winActor.scale_y || 1;
                const tx = winActor.translation_x || 0;
                const ty = winActor.translation_y || 0;
                let x1 = (box.x - (winActor.x + tx)) / sx;
                let y1 = (box.y - (winActor.y + ty)) / sy;
                let x2 = x1 + box.width / sx;
                let y2 = y1 + box.height / sy;
                if (rect) {
                    x1 = Math.max(x1, rect[0]);
                    y1 = Math.max(y1, rect[1]);
                    x2 = Math.min(x2, rect[0] + rect[2]);
                    y2 = Math.min(y2, rect[1] + rect[3]);
                }
                winActor.set_clip(x1, y1, Math.max(0, x2 - x1), Math.max(0, y2 - y1));
            };
            const sig = winActor.connect('notify::translation-x', sync);
            sync();
            return {winActor, hadClip, rect, sig};
        });
        return goneActor => {
            for (const {winActor, hadClip, rect, sig} of tracked) {
                winActor.disconnect(sig);
                if (winActor === goneActor)
                    continue;
                if (hadClip && rect)
                    winActor.set_clip(...rect);
                else
                    winActor.remove_clip();
            }
        };
    };
    const apps = new AppDrag({...shared,
        windows: appWindows,
        width: () => monitor()?.width ?? global.screen_width,
        stack: () => group.get_children(),
        raise: windowActor => group.set_child_above_sibling(windowActor, null),
        clipActors,
        restoreStack: order => {
            const current = group.get_children();
            const currentSet = new Set(current);
            const orderSet = new Set(order);
            for (const windowActor of order) {
                if (currentSet.has(windowActor))
                    group.set_child_above_sibling(windowActor, null);
            }
            // Keep any windows created mid-drag on top instead of burying them.
            for (const windowActor of current) {
                if (!orderSet.has(windowActor))
                    group.set_child_above_sibling(windowActor, null);
            }
        },
    });

    const prepareOverview = () => {
        apps.cancel();
        desktop.restore(false);
    };
    let keyboard = null;
    return {
        desktop,
        apps,
        prepareOverview,
        dragAllowed: () => main.actionMode === Shell.ActionMode.NORMAL &&
            !main.overview.visible && !main.overview.animationInProgress,
        actionAllowed: () => !!(main.actionMode & (Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW)),
        mediaAllowed: () => !!(main.actionMode & (Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW)),
        switchApp: step => {
            desktop.restore();
            apps.cancel();
            if (apps.begin(step))
                apps.end(true);
        },
        media: action => {
            const key = MEDIA_KEYS[action];
            if (!key)
                return;
            try {
                keyboard ??= Clutter.get_default_backend().get_default_seat()
                    .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
                const now = GLib.get_monotonic_time();
                keyboard.notify_keyval(now, key, Clutter.KeyState.PRESSED);
                keyboard.notify_keyval(now, key, Clutter.KeyState.RELEASED);
            } catch {
                keyboard = null;
            }
        },
        destroy: () => {
            keyboard = null;
        },
    };
}

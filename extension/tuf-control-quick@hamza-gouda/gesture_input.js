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
};

/** Bind pure gesture state to the existing Shell actors and input devices. */
export function createGestureInput(main) {
    const focus = () => global.display.focus_window;
    const actor = window => window.get_compositor_private();
    const windows = () => global.workspace_manager.get_active_workspace().list_windows();
    const switchable = new Set([Meta.WindowType.NORMAL, Meta.WindowType.DIALOG, Meta.WindowType.MODAL_DIALOG]);
    const eligible = window => switchable.has(window.get_window_type()) && !window.skip_taskbar;
    const monitor = () => main.layoutManager.monitors[
        focus()?.get_monitor() ?? main.layoutManager.primaryIndex] ?? main.layoutManager.primaryMonitor;
    const tracker = Shell.WindowTracker.get_default();
    const appWindows = () => {
        const focused = focus();
        const byApp = new Map();
        for (const window of windows().sort((a, b) => a.get_stable_sequence() - b.get_stable_sequence())) {
            if (!eligible(window) || window.get_monitor() !== focused?.get_monitor())
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
        settle: (windowActor, properties, done) => windowActor.ease({...properties,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC, onComplete: done}),
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
    const apps = new AppDrag({...shared,
        windows: appWindows,
        width: () => monitor()?.width ?? global.screen_width,
        stack: () => group.get_children(),
        raise: windowActor => group.set_child_above_sibling(windowActor, null),
        restoreStack: order => {
            const current = new Set(group.get_children());
            for (const windowActor of order) {
                if (current.has(windowActor))
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
        desktop, apps, prepareOverview,
        dragAllowed: () => main.actionMode === Shell.ActionMode.NORMAL &&
            !main.overview.visible && !main.overview.animationInProgress,
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
            keyboard ??= Clutter.get_default_backend().get_default_seat()
                .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
            keyboard.notify_keyval(GLib.get_monotonic_time(), key, Clutter.KeyState.PRESSED);
            keyboard.notify_keyval(GLib.get_monotonic_time(), key, Clutter.KeyState.RELEASED);
        },
        destroy: () => {
            keyboard = null;
        },
    };
}

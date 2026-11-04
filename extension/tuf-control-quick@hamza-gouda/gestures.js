// Copyright (C) Hamza Gouda
// SPDX-License-Identifier: GPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Hamza Gouda
//
// Touchpad gestures: a watcher on the stage that sees the shell's touchpad
// swipes and pinches go by and runs the action each completed gesture is bound
// to in the settings. Nothing is ever consumed — every event propagates, so
// the shell's own 3-finger swipes keep working; we only observe. The decision
// maths lives in gesture_logic.js (pure, tested with gjs -m). The shell side
// takes Main, Meta and Clutter through parameters, so this module also loads
// outside the shell (tests).

import Gio from 'gi://Gio';

import {GestureTracker, PHASE_BEGIN, PHASE_END, SWIPE_DISTANCE, DOMINANCE,
        resolveAction} from './gesture_logic.js';

/**
 * The action implementations GestureWatcher dispatches to: `main` is the
 * shell's Main module, `meta` the Meta namespace and `settings` the
 * extension's Gio.Settings (launch-app reads the desktop id from it).
 * `overviewVisible` is not an action but the check the watcher makes first for
 * 3-finger down, which the shell handles itself while the overview is up.
 */
export function shellActions(main, meta, settings) {
    const openApp = id => {
        if (!id)
            return;
        try {
            const info = Gio.DesktopAppInfo.new(id);
            if (info)
                info.launch([], global.create_app_launch_context(0, -1));
            else
                main.notifyError('TUF Control', `The app “${id}” is not installed.`);
        } catch (e) {
            main.notifyError('TUF Control', `Could not start the app: ${e.message}`);
        }
    };

    const manager = global.workspace_manager;

    // The windows of every workspace, in workspace order, each window once
    // (windows on all workspaces would repeat); focus the one after the
    // currently focused one, around the end of the list.
    const nextWindow = () => {
        const seen = new Set(), windows = [];
        const switchable = new Set([meta.WindowType.NORMAL, meta.WindowType.DIALOG,
            meta.WindowType.MODAL_DIALOG]);
        for (let i = 0; i < manager.get_n_workspaces(); i++) {
            for (const win of manager.get_workspace_by_index(i).list_windows()) {
                if (!seen.has(win) && !win.skip_taskbar && switchable.has(win.get_window_type())) {
                    seen.add(win);
                    windows.push(win);
                }
            }
        }
        if (!windows.length)
            return;
        const at = windows.indexOf(global.display.focus_window);
        windows[(at + 1) % windows.length].activate(global.get_current_time());
    };

    const switchWorkspace = step => manager.get_active_workspace()
        .get_neighbor(step > 0 ? meta.MotionDirection.DOWN : meta.MotionDirection.UP)
        .activate(global.get_current_time());

    // Only change visible, minimizable windows on the active workspace. Keep
    // the exact set so a second invocation restores all of them, never windows
    // the user had already minimized. Forget destroyed windows immediately.
    let hidden = new Map();
    let lastFocused = null;
    const restoreDesktop = () => {
        const windows = [...hidden.keys()];
        for (const [win, id] of hidden) {
            win.disconnect(id);
            if (win.minimized)
                win.unminimize();
        }
        hidden.clear();
        if (windows.includes(lastFocused))
            lastFocused.activate(global.get_current_time());
        lastFocused = null;
    };

    return {
        overviewVisible: () => main.overview.visible,
        // the watcher consults this before returning the last app in the overview
        showDesktopCanReturn: () => hidden.size > 0,
        destroy: restoreDesktop,
        overview: () => main.overview.toggle(),
        // The way the dash's show-apps button opens the app grid.
        'app-grid': () => main.overview.showApps(),
        'show-desktop': () => {
            if (hidden.size) {
                restoreDesktop();
                return;
            }
            lastFocused = global.display.focus_window;
            for (const win of manager.get_active_workspace().list_windows()) {
                if (win.minimized || win.skip_taskbar || !win.can_minimize()
                        || win.is_on_all_workspaces())
                    continue;
                const id = win.connect('unmanaged', () => {
                    hidden.delete(win);
                    if (lastFocused === win)
                        lastFocused = null;
                });
                hidden.set(win, id);
                win.minimize();
            }
        },
        'switch-windows': nextWindow,
        'prev-workspace': () => switchWorkspace(-1),
        'next-workspace': () => switchWorkspace(1),
        'launch-app': () => openApp(settings.get_string('gesture-launch-app')),
    };
}

/**
 * Watches touchpad swipes and pinches on `stage` (global.stage) and runs the
 * action each completed gesture is bound to in `settings`. The settings are
 * read when a gesture completes, so changing a binding applies at once.
 *
 * With 'gesture-enabled' off the handler does nothing at all, and disable()
 * removes it again. `clutter` is the Clutter namespace; a parameter only so
 * tests can run the watcher without the shell's typelib path.
 */
export class GestureWatcher {
    constructor({settings, stage, actions, clutter, tracker, log = null, debug = null}) {
        this._settings = settings;
        this._stage = stage;
        this._actions = actions;
        this._clutter = clutter;
        this._tracker = tracker ?? new GestureTracker();
        this._log = log ?? (msg => console.log(`[tuf-gesture] ${msg}`));
        this._debug = debug ?? (msg => console.debug(`[tuf-gesture] ${msg}`));
        this._handler = 0;
    }

    enable() {
        if (this._handler)
            return;
        this._handler = this._stage.connect('captured-event',
            (stage, event) => this._event(event));
    }

    disable() {
        if (!this._handler)
            return;
        this._stage.disconnect(this._handler);
        this._handler = 0;
        this._tracker.reset(); // forget a half-finished gesture
        this._actions.destroy?.();
    }

    _event(event) {
        // Switched off: do nothing at all, not even track.
        if (!this._settings.get_boolean('gesture-enabled'))
            return this._clutter.EVENT_PROPAGATE;

        const type = event.type();
        const pinch = type === this._clutter.EventType.TOUCHPAD_PINCH;
        if (type !== this._clutter.EventType.TOUCHPAD_SWIPE && !pinch)
            return this._clutter.EVENT_PROPAGATE;

        const [dx, dy] = pinch
            ? [event.get_gesture_pinch_angle_delta(), event.get_gesture_pinch_scale()]
            : event.get_gesture_motion_delta();
        // not every surface reports an unaccelerated delta; tests' fakes do not either
        const [udx, udy] = event.get_gesture_motion_delta_unaccelerated?.() ?? [0, 0];
        // Mutter 14 names it finger_count (get_touchpad_gesture_fingers does not exist)
        const fingers = event.get_touchpad_gesture_finger_count();
        const phase = event.get_gesture_phase();
        // snapshot before the END call: the tracker resets on END, so after it the
        // accumulated travel is gone (that blind spot cost us one diagnostic round)
        const before = phase === PHASE_END ? this._tracker.stats() : null;
        const gesture = pinch
            ? this._tracker.pinch(phase, fingers, dx, dy)
            : this._tracker.swipe(phase, fingers, dx, dy);
        if (phase === PHASE_BEGIN || phase === PHASE_END)
            this._debug(`swipe fingers=${fingers} phase=${phase === PHASE_BEGIN ? 'begin' : 'end'}`);
        if (gesture) {
            const ran = this._fire(gesture);
            this._log(`completed ${gesture} -> ${ran ?? 'no action bound'}`);
        } else if (phase === PHASE_END) {
            this._debug(`no gesture: dx=${before.dx} dy=${before.dy} ` +
                        `(unaccelerated ${udx.toFixed(1)}/${udy.toFixed(1)}; ` +
                        `needs ${SWIPE_DISTANCE}px on one axis, ${DOMINANCE}x the other)`);
        }
        return this._clutter.EVENT_PROPAGATE;
    }

    _fire(gesture) {
        // 3-finger down while the overview is up: the shell closes the overview
        // itself, and when "Show desktop" was the thing that minimised the app,
        // the same swipe brings it back — down reads as a toggle from either
        // side. Any other binding stays out of the shell's way.
        if (gesture === 'swipe3-down' && this._actions.overviewVisible()) {
            if (resolveAction(this._settings, gesture) === 'show-desktop'
                    && this._actions.showDesktopCanReturn())
                this._actions['show-desktop']();
            return null;
        }
        const action = resolveAction(this._settings, gesture);
        if (action)
            this._actions[action]();
        return action;
    }
}

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

import {DesktopToggle} from './desktop.js';

import {GestureTracker, PHASE_BEGIN, PHASE_END, SWIPE_DISTANCE, DOMINANCE,
        resolveAction} from './gesture_logic.js';

/**
 * The action implementations GestureWatcher dispatches to: `main` is the
 * shell's Main module, `meta` the Meta namespace and `settings` the
 * extension's Gio.Settings (launch-app reads the desktop id from it).
 * `overviewVisible` is not an action but the check the watcher makes first for
 * 3-finger down, which the shell handles itself while the overview is up.
 */
export function shellActions(main, meta, settings, motion = null) {
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

    const actor = w => w.get_compositor_private?.();
    const desktop = new DesktopToggle({
        windows: () => manager.get_active_workspace().list_windows(),
        eligible: w => !w.skip_taskbar && w.can_minimize() && !w.is_on_all_workspaces()
            && (!w.get_compositor_private || !!actor(w)),
        focus: () => global.display.focus_window,
        activate: w => w.activate(global.get_current_time()),
        getOpacity: w => actor(w)?.opacity ?? 255,
        opacity: (w, value) => { if (actor(w)) actor(w).opacity = value; },
        cancel: w => actor(w)?.remove_transition('opacity'),
        fade: (w, value, done) => {
            if (motion && actor(w))
                motion.fade(actor(w), value, done);
            else
                done();
        },
        minimize: w => { main.wm?.skipNextEffect(actor(w)); w.minimize(); },
        unminimize: w => { main.wm?.skipNextEffect(actor(w)); w.unminimize(); },
    });
    let pending = 0;
    const cancelPending = () => {
        if (pending)
            main.overview.disconnect(pending);
        pending = 0;
    };
    const toggleDesktop = () => {
        if (pending) {
            cancelPending();
            return;
        }
        if (main.overview.visible || main.overview.animationInProgress) {
            pending = main.overview.connect('hidden', () => {
                cancelPending();
                desktop.toggle();
            });
            main.overview.hide();
        } else {
            desktop.toggle();
        }
    };

    return {
        overviewVisible: () => main.overview.visible,
        // the watcher consults this before returning the last app in the overview
        showDesktopCanReturn: () => desktop.canReturn,
        destroy: () => { cancelPending(); desktop.destroy(); },
        overview: () => main.overview.toggle(),
        'app-grid': () => main.overview.showApps(),
        'show-desktop': toggleDesktop,
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
        this._startedOverview = false;
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
        this._startedOverview = false;
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
        if (phase === PHASE_BEGIN)
            this._startedOverview = this._actions.overviewVisible();
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
        if (gesture === 'swipe3-down' && (this._startedOverview || this._actions.overviewVisible())) {
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

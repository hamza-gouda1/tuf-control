// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda

import {PHASE_BEGIN, PHASE_UPDATE, PHASE_END, SWIPE_DISTANCE, DOMINANCE, resolveAction} from './gesture_logic.js';

const LOCK_DISTANCE = 10;
const DESKTOP_DISTANCE = 200;
const APP_DISTANCE = 280;
const VOLUME_DISTANCE = 28;
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const elapsed = (now, before) => (now - before) >>> 0;

/** A captured swipe keeps its direction and owner until release or cancellation. */
export class GestureSession {
    constructor(settings, actions, input) {
        this._settings = settings;
        this._actions = actions;
        this._input = input;
        this._swipe = null;
        this._hold = null;
    }

    cancel() {
        this._hold = null;
        const swipe = this._swipe;
        this._swipe = null;
        if (!swipe)
            return;
        swipe.drag?.end(false);
    }

    hold(phase, fingers, time) {
        if (phase === PHASE_BEGIN) {
            this._hold = fingers === 4 && this._input.mediaAllowed()
                ? resolveAction(this._settings, 'hold4') : null;
            this._holdStarted = time;
            return this._hold !== null;
        }
        const action = this._hold;
        this._hold = null;
        if (!action)
            return false;
        const duration = elapsed(time, this._holdStarted);
        if (phase === PHASE_END && fingers === 4 && duration >= 100 && duration <= 1500
            && this._input.mediaAllowed())
            this._actions[action]?.();
        return true;
    }

    swipe(phase, fingers, dx, dy, time) {
        if (phase === PHASE_BEGIN) {
            this.cancel();
            if (fingers !== 3 && fingers !== 4)
                return false;
            const bindings = Object.fromEntries(
                (fingers === 3 ? ['left', 'right', 'down'] : ['up', 'down']).map(direction =>
                    [direction, resolveAction(this._settings, `swipe${fingers}-${direction}`)]));
            if (fingers === 3) {
                if (!Object.values(bindings).some(Boolean) || !this._input.dragAllowed())
                    return false;
            } else if (!this._input.mediaAllowed() || !Object.values(bindings).some(
                action => action === 'volume-up' || action === 'volume-down')) {
                return false;
            }
            this._swipe = {fingers, bindings, dx: 0, dy: 0, progress: 0,
                velocity: 0, time, volumeSteps: 0};
            return false; // let native handlers reset their state at BEGIN
        }

        const swipe = this._swipe;
        if (!swipe)
            return false;
        if (swipe.fingers !== fingers
            || !(fingers === 3 ? this._input.dragAllowed() : this._input.mediaAllowed())) {
            this.cancel();
            return true;
        }
        if (phase !== PHASE_UPDATE) {
            this._swipe = null;
            const commit = phase === PHASE_END && (
                swipe.progress >= 0.35 ||
                (swipe.progress >= 0.12 && swipe.velocity >= 0.45 && elapsed(time, swipe.time) <= 100));
            if (swipe.drag)
                swipe.drag.end(commit);
            else if (phase === PHASE_END && fingers === 3 && swipe.action) {
                const horizontal = swipe.direction === 'left' || swipe.direction === 'right';
                const primary = Math.abs(horizontal ? swipe.dx : swipe.dy);
                const cross = Math.abs(horizontal ? swipe.dy : swipe.dx);
                if (primary >= SWIPE_DISTANCE && primary >= DOMINANCE * cross && swipe.progress > 0)
                    this._actions[swipe.action]?.();
            }
            return true;
        }

        swipe.dx += dx;
        swipe.dy += dy;
        if (!swipe.direction) {
            const x = Math.abs(swipe.dx), y = Math.abs(swipe.dy);
            if (Math.max(x, y) < LOCK_DISTANCE || Math.max(x, y) < 1.25 * Math.min(x, y))
                return true;
            swipe.direction = x > y ? (swipe.dx < 0 ? 'left' : 'right')
                : (swipe.dy < 0 ? 'up' : 'down');
            const desktopSwipe = fingers === 3 && swipe.bindings.down === 'show-desktop' &&
                (swipe.direction === 'up' || swipe.direction === 'down');
            swipe.action = desktopSwipe ? null : swipe.bindings[swipe.direction];
            if (desktopSwipe) {
                this._input.apps.cancel();
                if (this._input.desktop.begin(swipe.direction === 'up'))
                    swipe.drag = this._input.desktop;
            }
            // A second down swipe leaves the desktop showing. Up goes to GNOME
            // unless the extension has windows to restore.
            if (fingers === 3 && !swipe.action && !swipe.drag &&
                    !(desktopSwipe && swipe.direction === 'down')) {
                this._swipe = null;
                this._input.prepareOverview();
                return false;
            }
            if (fingers === 4 && !['volume-up', 'volume-down'].includes(swipe.action)) {
                this._swipe = null;
                return false;
            }
            if (['prev-window', 'next-window'].includes(swipe.action)) {
                this._input.desktop.restore();
                this._input.apps.cancel();
                swipe.drag = this._input.apps;
                swipe.drag.begin(swipe.action === 'next-window' ? 1 : -1);
            }
        }

        if (fingers === 4) {
            const steps = clamp(Math.trunc(swipe.dy / VOLUME_DISTANCE), -20, 20);
            const delta = clamp(steps - swipe.volumeSteps, -4, 4);
            swipe.volumeSteps = steps;
            const action = swipe.bindings[delta < 0 ? 'up' : 'down'];
            if (action === 'volume-up' || action === 'volume-down') {
                for (let step = 0; step < Math.abs(delta); step++)
                    this._actions[action]?.();
            }
            return true;
        }

        const horizontal = swipe.direction === 'left' || swipe.direction === 'right';
        const sign = swipe.direction === 'left' || swipe.direction === 'up' ? -1 : 1;
        swipe.progress = clamp(sign * (horizontal ? swipe.dx : swipe.dy)
            / (horizontal ? APP_DISTANCE : DESKTOP_DISTANCE), 0, 1);
        swipe.velocity = sign * (horizontal ? dx : dy) / Math.max(1, elapsed(time, swipe.time));
        swipe.time = time;
        swipe.drag?.update(swipe.progress);
        return true;
    }
}

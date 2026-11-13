// Copyright (C) Hamza Gouda
// SPDX-License-Identifier: GPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Hamza Gouda
//
// Pure helpers (no gi imports): what a stream of touchpad swipe and pinch
// events means. Clutter delivers per-event deltas at UPDATE, so the tracker
// accumulates them and only decides at END. A swipe counts when the dominant
// axis travelled SWIPE_DISTANCE logical px and carried DOMINANCE times the
// other axis; a pinch when the accumulated scale change reached PINCH_SCALE.
// Tested with gjs -m.

export const GESTURES = [
    'swipe3-down', 'swipe3-left', 'swipe3-right', 'swipe4-up', 'swipe4-down', 'swipe4-left', 'swipe4-right',
    'pinch-in', 'pinch-out', 'hold4',
];

// Everything a gesture can be bound to; 'none' disables it. These are the
// allowed values of the gesture-* settings keys.
export const ACTIONS = [
    'none', 'overview', 'app-grid', 'show-desktop', 'switch-windows',
    'prev-workspace', 'next-workspace', 'launch-app',
    'prev-window', 'next-window', 'volume-up', 'volume-down', 'play-pause',
    'media-next', 'media-prev',
];

// Measured on the owner's FX608JHR: a natural three-finger swipe down accumulates
// ~73 px (dx ~25), so the floor sits well under that yet far above accidental
// three-finger contact drifting across the pad.
export const SWIPE_DISTANCE = 50; // logical px a swipe must travel
export const PINCH_SCALE = 0.25;  // total scale change a pinch must reach
export const DOMINANCE = 1.4;     // dominant axis vs the other axis

// Clutter.TouchpadGesturePhase (values verified against mutter 46)
export const PHASE_BEGIN = 0;
export const PHASE_UPDATE = 1;
export const PHASE_END = 2;
export const PHASE_CANCEL = 3;

/**
 * Turns (phase, fingers, dx, dy) calls into completed gesture names.
 *
 * Swipes and pinches are separate paths: for a pinch, dx is the angle delta
 * (ignored) and dy the cumulative scale, so pinch() keeps the latest scale minus 1.0. BEGIN
 * starts a gesture from zero, anything but a matching UPDATE is dropped, and
 * END returns the gesture — or null — and resets, so consecutive gestures do
 * not add up.
 */
export class GestureTracker {
    constructor() {
        this.reset();
    }

    swipe(phase, fingers, dx, dy) {
        return this._event(false, phase, fingers, dx, dy);
    }

    pinch(phase, fingers, dx, dy) {
        return this._event(true, phase, fingers, dx, dy);
    }

    /** Forgets a half-finished gesture (the watcher does this on disable). */
    reset() {
        this._active = false;
        this._pinch = false;
        this._fingers = 0;
        this._dx = 0;
        this._dy = 0;
    }

    /** The accumulated state, for diagnostics (what a swipe actually carried). */
    stats() {
        return {active: this._active, pinch: this._pinch, fingers: this._fingers,
                dx: Math.round(this._dx), dy: Math.round(this._dy)};
    }

    _event(pinch, phase, fingers, dx, dy) {
        if (phase === PHASE_BEGIN) {
            this._active = true;
            this._pinch = pinch;
            this._fingers = fingers;
            this._dx = 0;
            this._dy = 0;
            return null;
        }
        if (phase === PHASE_UPDATE) {
            if (!this._matches(pinch, fingers)) {
                this.reset();
                return null;
            }
            if (pinch)
                this._dy = Number.isFinite(dy) && dy > 0 ? dy - 1 : 0;
            else {
                this._dx += dx;
                this._dy += dy;
            }
            return null;
        }
        const gesture = phase === PHASE_END && this._matches(pinch, fingers)
            ? this._decide() : null;
        this.reset();
        return gesture;
    }

    _matches(pinch, fingers) {
        return this._active && this._pinch === pinch && this._fingers === fingers;
    }

    _decide() {
        if (this._pinch) {
            if (Math.abs(this._dy) < PINCH_SCALE)
                return null;
            return this._dy > 0 ? 'pinch-out' : 'pinch-in';
        }
        const absDx = Math.abs(this._dx), absDy = Math.abs(this._dy);
        if (this._fingers === 3) {
            // Down only: the shell itself handles 3-finger up and horizontal.
            if (this._dy >= SWIPE_DISTANCE && this._dy >= DOMINANCE * absDx)
                return 'swipe3-down';
            return null;
        }
        if (this._fingers === 4) {
            if (absDx >= absDy) {
                if (absDx >= SWIPE_DISTANCE && absDx >= DOMINANCE * absDy)
                    return this._dx > 0 ? 'swipe4-right' : 'swipe4-left';
            } else if (absDy >= SWIPE_DISTANCE && absDy >= DOMINANCE * absDx) {
                return this._dy > 0 ? 'swipe4-down' : 'swipe4-up';
            }
        }
        return null;
    }
}

/**
 * The action bound to `gesture` in `settings` right now, or null when gestures
 * are switched off, the binding is 'none' or the value is not an action.
 * Reading the settings as each gesture completes keeps the bindings live: a
 * change applies to the next gesture, with no restart. `settings` only needs
 * get_boolean() and get_string() (Gio.Settings has both).
 */
export function resolveAction(settings, gesture) {
    if (!settings.get_boolean('gesture-enabled'))
        return null;
    const action = settings.get_string(`gesture-${gesture}`);
    return ACTIONS.includes(action) && action !== 'none' ? action : null;
}

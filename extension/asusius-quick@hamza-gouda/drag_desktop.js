// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda

const POSE_PROPERTIES = ['translation_x', 'translation_y', 'scale_x', 'scale_y', 'opacity'];
const TUCK_SCALE = 0.20;

/** Own only the windows involved in the current desktop toggle. */
export class DesktopDrag {
    constructor(deps) {
        this._d = deps;
        this._windows = new Map();
        this._hidden = false;
        this._focus = null;
        this._gesture = null;
        this._settle = null;
        this._destroyed = false;
    }

    get canReturn() { return this._hidden && this._windows.size > 0; }
    get active() { return this._gesture !== null; }

    _forget(window, record) {
        this._windows.delete(window);
        window.disconnect(record.signal);
    }

    _finishSettle() {
        const settle = this._settle;
        if (settle) {
            this._settle = null;
            this._apply(settle.hidden, settle.focus);
        }
    }

    begin(expectedReturning = null) {
        if (this._destroyed)
            return false;
        this._finishSettle();
        if (this._gesture)
            this.end(false, false);
        for (const [window, record] of this._windows) {
            if (!window.minimized)
                this._forget(window, record);
        }
        const returning = this.canReturn;
        if (expectedReturning !== null && returning !== expectedReturning)
            return false;
        if (!returning) {
            this._hidden = false;
            this._focus = this._d.focus();
            for (const window of this._d.windows()) {
                if (window.minimized || !this._d.eligible(window))
                    continue;
                const actor = this._d.actor(window);
                if (!actor)
                    continue;
                const record = {actor, pose: null, offset: null};
                record.signal = window.connect('unmanaged', () => {
                    this._forget(window, record);
                    if (window === this._focus)
                        this._focus = null;
                    const pending = this._settle?.remaining;
                    if (pending?.delete(window) && pending.size === 0)
                        this._finishSettle();
                });
                this._windows.set(window, record);
            }
        }
        if (!this._windows.size)
            return false;
        const animated = this._d.animationsEnabled();
        this._gesture = {returning, animated, progress: 0};
        if (animated) {
            for (const [window, record] of this._windows) {
                const {actor} = record;
                record.pose = Object.fromEntries(POSE_PROPERTIES.map(key => [key, actor[key]]));
                record.offset = this._d.tuckOffset(window, actor);
            }
        }
        if (returning && animated) {
            this.update(0);
            for (const [window, {actor}] of this._windows) {
                this._d.skipEffect(actor);
                window.unminimize();
            }
        }
        return true;
    }

    _target(record, gesture, progress) {
        const hiddenAmount = gesture.returning ? 1 - progress : progress;
        const scale = 1 - (1 - TUCK_SCALE) * hiddenAmount;
        const {pose, offset} = record;
        // Contract the whole layout toward the same point on each monitor.
        // Using the same pose for hide and return preserves the path and stacking.
        return {
            translation_x: pose.translation_x + offset.x * (1 - scale),
            translation_y: pose.translation_y + offset.y * (1 - scale),
            scale_x: pose.scale_x * scale,
            scale_y: pose.scale_y * scale,
            opacity: Math.round(pose.opacity * (1 - hiddenAmount ** 2)),
        };
    }

    update(progress) {
        const gesture = this._gesture;
        if (!gesture)
            return;
        gesture.progress = Math.max(0, Math.min(1, Number(progress) || 0));
        if (gesture.animated)
            for (const record of this._windows.values())
                Object.assign(record.actor, this._target(record, gesture, gesture.progress));
    }

    end(commit, animate = true) {
        const gesture = this._gesture;
        if (!gesture)
            return;
        this._gesture = null;
        const hidden = gesture.returning ? !commit : commit;
        const focus = gesture.returning && commit;
        if (!animate || !gesture.animated || !this._d.animationsEnabled() || !this._windows.size) {
            this._apply(hidden, focus);
            return;
        }
        const progress = commit ? 1 : 0;
        const remaining = Math.abs(progress - gesture.progress);
        if (remaining === 0) {
            this._apply(hidden, focus);
            return;
        }
        const duration = Math.round(65 + 95 * remaining);
        const settle = {hidden, focus, remaining: new Set(this._windows.keys())};
        this._settle = settle;
        for (const [window, record] of this._windows) {
            this._d.settle(record.actor, {...this._target(record, gesture, progress), duration}, () => {
                if (this._settle === settle && settle.remaining.delete(window) && !settle.remaining.size)
                    this._finishSettle();
            });
        }
    }

    _apply(hidden, focus) {
        for (const [window, record] of this._windows) {
            const {actor} = record;
            if (record.pose)
                for (const key of POSE_PROPERTIES)
                    actor.remove_transition(key.replace('_', '-'));
            if (hidden !== window.minimized) {
                this._d.skipEffect(actor);
                if (hidden)
                    window.minimize();
                else
                    window.unminimize();
            }
            if (record.pose) {
                Object.assign(actor, record.pose);
                record.pose = null;
                record.offset = null;
            }
            if (!hidden)
                this._forget(window, record);
        }
        this._hidden = hidden;
        if (!hidden) {
            if (focus && this._focus && !this._focus.minimized)
                this._d.activate(this._focus);
            this._focus = null;
        }
    }

    toggle() {
        if (this.begin())
            this.end(true);
    }

    restore(focus = true) {
        this._gesture = null;
        this._settle = null;
        this._apply(false, focus);
    }

    destroy() {
        this.restore(false);
        this._destroyed = true;
    }
}

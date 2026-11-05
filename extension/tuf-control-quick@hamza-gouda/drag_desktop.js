// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda

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

    begin() {
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
        if (!returning) {
            this._hidden = false;
            this._focus = this._d.focus();
            for (const window of this._d.windows()) {
                if (window.minimized || !this._d.eligible(window))
                    continue;
                const actor = this._d.actor(window);
                if (!actor)
                    continue;
                const record = {actor, opacity: actor.opacity, y: actor.translation_y};
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
        this._gesture = {returning, animated, progress: 0, distance: this._d.height() * 0.18};
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
        return {
            opacity: Math.round(record.opacity * (1 - hiddenAmount)),
            translation_y: record.y + (gesture.returning ? -1 : 1) * gesture.distance * hiddenAmount,
        };
    }

    update(progress) {
        const gesture = this._gesture;
        if (!gesture)
            return;
        gesture.progress = Math.max(0, Math.min(1, Number(progress) || 0));
        if (gesture.animated) {
            const amount = gesture.returning ? 1 - gesture.progress : gesture.progress;
            const offset = (gesture.returning ? -1 : 1) * gesture.distance * amount;
            for (const record of this._windows.values()) {
                record.actor.translation_y = record.y + offset;
                record.actor.opacity = Math.round(record.opacity * (1 - amount));
            }
        }
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
        const duration = Math.round(90 + 70 * Math.abs(progress - gesture.progress));
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
            actor.remove_transition('opacity');
            actor.remove_transition('translation-y');
            if (hidden !== window.minimized) {
                this._d.skipEffect(actor);
                if (hidden)
                    window.minimize();
                else
                    window.unminimize();
            }
            actor.translation_y = record.y;
            actor.opacity = record.opacity;
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
        this.restore();
        this._destroyed = true;
    }
}

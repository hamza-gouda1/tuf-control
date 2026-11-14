// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda

// Animate existing actors only: no clones, textures, timers or frame callbacks.
export class DesktopToggle {
    constructor(deps) {
        this._d = deps;
        this._hidden = new Map();
        this._returning = new Map();
        this._focus = null;
    }

    get canReturn() { return this._hidden.size > 0; }

    _forget(map, w, record) {
        if (map.get(w) !== record)
            return;
        map.delete(w);
        w.disconnect(record.id);
    }

    _track(map, w, opacity) {
        const record = {opacity};
        record.id = w.connect('unmanaged', () => {
            map.delete(w);
            if (this._focus === w)
                this._focus = null;
        });
        map.set(w, record);
        return record;
    }

    _finishReturning() {
        for (const [w, r] of this._returning) {
            this._forget(this._returning, w, r);
            this._d.cancel(w);
            this._d.opacity(w, r.opacity);
        }
    }

    toggle() {
        if (this.canReturn) {
            this.restore();
            return;
        }
        this._finishReturning();
        this._focus = this._d.focus();
        for (const w of this._d.windows()) {
            if (w.minimized || !this._d.eligible(w))
                continue;
            const record = this._track(this._hidden, w, this._d.getOpacity(w));
            this._d.fade(w, 0, () => {
                if (this._hidden.get(w) !== record)
                    return; // an interrupted fade must never minimize a returned window
                this._d.minimize(w);
                this._d.opacity(w, record.opacity);
            });
        }
    }

    restore(animate = true, focus = true) {
        for (const [w, r] of this._hidden) {
            this._forget(this._hidden, w, r);
            this._d.cancel(w);
            if (!animate) {
                if (w.minimized)
                    this._d.unminimize(w);
                this._d.opacity(w, r.opacity);
                continue;
            }
            const from = w.minimized ? 0 : this._d.getOpacity(w);
            const returning = this._track(this._returning, w, r.opacity);
            this._d.opacity(w, from);
            if (w.minimized)
                this._d.unminimize(w);
            this._d.fade(w, r.opacity, () => {
                if (this._returning.get(w) !== returning)
                    return;
                this._forget(this._returning, w, returning);
                this._d.opacity(w, r.opacity);
            });
        }
        if (focus && this._focus)
            this._d.activate(this._focus);
        this._focus = null;
    }

    destroy() {
        this.restore(false, false);
        this._finishReturning();
    }
}

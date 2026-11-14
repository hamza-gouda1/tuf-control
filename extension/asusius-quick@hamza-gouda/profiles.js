// Copyright (C) Hamza Gouda
// SPDX-License-Identifier: GPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Hamza Gouda
//
// Pure helpers (no gi imports): profile names, ids, labels and the model that
// picks between asusd and power-profiles-daemon. Tested with gjs -m.

export const PROFILES = ['quiet', 'balanced', 'performance'];

export const LABELS = {
    quiet: 'Quiet',
    balanced: 'Balanced',
    performance: 'Performance',
};

export const ICONS = {
    quiet: 'power-profile-power-saver-symbolic',
    balanced: 'power-profile-balanced-symbolic',
    performance: 'power-profile-performance-symbolic',
};

// asusd xyz.ljones.Platform.PlatformProfile (u)
const ASUSD_IDS = {balanced: 0, performance: 1, quiet: 2};
const ASUSD_NAMES = {0: 'balanced', 1: 'performance', 2: 'quiet'};

export const asusdId = profile => ASUSD_IDS[profile] ?? null;
export const asusdProfile = id => ASUSD_NAMES[id] ?? null;

// power-profiles-daemon uses power-saver for quiet
export const ppdName = profile => (profile === 'quiet' ? 'power-saver' : profile);
export function ppdProfile(name) {
    if (name === 'power-saver')
        return 'quiet';
    return PROFILES.includes(name) ? name : null;
}

/** asusd PlatformProfileChoices (au) -> profile names, in display order. */
export function asusdChoices(ids) {
    const have = new Set((ids ?? []).map(asusdProfile).filter(Boolean));
    return PROFILES.filter(p => have.has(p));
}

// supergfxd GfxMode numbering
const GPU_NAMES = ['Hybrid', 'Integrated', 'NVIDIA (no modeset)', 'VFIO', 'eGPU', 'Dedicated (MUX)'];
export const gpuModeName = mode => GPU_NAMES[mode] ?? null;

export function subtitleFor(profile, backend) {
    if (!profile)
        return backend ? 'Unknown' : 'Unavailable';
    return LABELS[profile];
}

export const chargeLabel = pct =>
    (Number.isInteger(pct) && pct >= 20 && pct <= 100 ? `Charge limit: ${pct}%` : null);

/** Whether the Quick Settings tile should render as checked for `profile` and `choices`. */
export function isToggleActive(profile, choices) {
    if (!profile)
        return false;
    if ((choices ?? PROFILES).includes('performance'))
        return profile === 'performance';
    return profile !== 'balanced';
}

/** Next profile to request when clicking the Quick Settings tile, or null if immutable. */
export function pickToggleTarget(profile, choices) {
    const avail = choices?.length ? choices : PROFILES;
    if (avail.includes('performance')) {
        if (profile === 'performance')
            return avail.includes('balanced') ? 'balanced' : avail.find(c => c !== 'performance') ?? null;
        return 'performance';
    }
    if (profile && profile !== 'balanced')
        return avail.includes('balanced') ? 'balanced' : null;
    return avail.find(c => c !== 'balanced') ?? null;
}

/** Minimal signal helper so tests can verify nothing is left connected. */
export class Emitter {
    constructor() {
        this._cbs = new Map();
        this._next = 1;
    }

    connect(cb) {
        const id = this._next++;
        this._cbs.set(id, cb);
        return id;
    }

    disconnect(id) {
        this._cbs.delete(id);
    }

    emit() {
        for (const cb of [...this._cbs.values()])
            cb();
    }

    get size() {
        return this._cbs.size;
    }

    clear() {
        this._cbs.clear();
    }
}

/**
 * Chooses the first available source (asusd before power-profiles-daemon) and
 * re-emits whenever the active source, its profile or its choices change.
 *
 * A source is {available, profile, choices, connect(cb), disconnect(id),
 * set(profile, done), destroy()}.
 */
export class ProfileModel extends Emitter {
    constructor(sources) {
        super();
        this._sources = sources; // [{name, source}]
        this._ids = sources.map(s => s.source.connect(() => this.emit()));
        this._destroyed = false;
    }

    get active() {
        return this._sources.find(s => s.source.available) ?? null;
    }

    get backend() {
        return this.active?.name ?? null;
    }

    get profile() {
        return this.active?.source.profile ?? null;
    }

    get choices() {
        const c = this.active?.source.choices;
        return c?.length ? c : [...PROFILES];
    }

    set(profile, done = () => {}) {
        const a = this.active;
        if (this._destroyed || !a || !this.choices.includes(profile)) {
            done(new Error('profile not available'));
            return;
        }
        a.source.set(profile, done);
    }

    destroy() {
        if (this._destroyed)
            return;
        this._destroyed = true;
        this._sources.forEach((s, i) => {
            s.source.disconnect(this._ids[i]);
            s.source.destroy();
        });
        this.clear();
    }
}

/**
 * Announces profile changes that happen after startup (Fn+F5 cycles them in
 * asusd, other apps may set them too) through `show(profile)`, typically an
 * OSD. The first profile a backend reports is only a baseline, and so is any
 * switch of backend; a burst of changes within `delay` ms shows only the last.
 *
 * timers = {schedule(ms, fn) -> handle, cancel(handle)}
 */
export class ProfileAnnouncer {
    constructor(model, {schedule, cancel, show, delay = 300, now = () => Date.now()}) {
        this._model = model;
        this._schedule = schedule;
        this._cancel = cancel;
        this._show = show;
        this._delay = delay;
        this._now = now;
        this._backend = model.backend;
        this._last = model.profile;
        this._pending = null;
        this._suppressed = null;
        this.shown = 0;
        this._id = model.connect(() => this._changed());
    }

    /** Suppress the OSD briefly when our own Quick Settings UI just requested `profile`. */
    suppress(profile, windowMs = 2000) {
        this._suppressed = {profile, until: this._now() + windowMs};
    }

    clearSuppression() {
        this._suppressed = null;
    }

    _changed() {
        const backend = this._model.backend, profile = this._model.profile;
        if (backend !== this._backend) {
            this._backend = backend;
            this._last = profile;
            this._suppressed = null;
            this._stop();
            return;
        }
        if (!profile)
            return;
        if (this._last === null) {
            this._last = profile;
            this._suppressed = null;
            return;
        }
        if (this._suppressed) {
            const matches = this._suppressed.profile === profile && this._now() <= this._suppressed.until;
            this._suppressed = null;
            if (matches) {
                this._last = profile;
                this._stop();
                return;
            }
        }
        if (profile === this._last && this._pending === null)
            return; // something else changed (choices, charge limit)
        this._last = profile;
        this._stop();
        this._pending = this._schedule(this._delay, () => {
            this._pending = null;
            this.shown++;
            this._show(this._last);
        });
    }

    _stop() {
        if (this._pending !== null) {
            this._cancel(this._pending);
            this._pending = null;
        }
    }

    get pending() {
        return this._pending !== null;
    }

    destroy() {
        if (!this._model)
            return;
        this._stop();
        this._suppressed = null;
        this._model.disconnect(this._id);
        this._model = null;
    }
}

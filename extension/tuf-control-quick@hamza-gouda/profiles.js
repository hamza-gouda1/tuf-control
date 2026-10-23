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

// Copyright (C) Hamza Gouda
// SPDX-License-Identifier: GPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Hamza Gouda
//
// System-bus sources built on Gio.DBusProxy. Everything is asynchronous, so the
// shell never blocks on asusd, supergfxd or power-profiles-daemon.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {
    Emitter, asusdChoices, asusdId, asusdProfile, ppdName, ppdProfile,
} from './profiles.js';

const ASUSD = 'xyz.ljones.Asusd';
const PLATFORM = 'xyz.ljones.Platform';
const PPD_UPOWER = 'org.freedesktop.UPower.PowerProfiles';
const PPD_UPOWER_PATH = '/org/freedesktop/UPower/PowerProfiles';
const PPD_HADESS = 'net.hadess.PowerProfiles';
const PPD_HADESS_PATH = '/net/hadess/PowerProfiles';
const SUPERGFX = 'org.supergfxctl.Daemon';

/**
 * Watches one bus name and keeps a proxy to one object while it is owned.
 * Subclasses implement _read(proxy), _reset() and optional _signal(name).
 */
class ProxySource extends Emitter {
    constructor(name, path, iface) {
        super();
        this._name = name;
        this._path = path;
        this._iface = iface;
        this._proxy = null;
        this._proxyIds = [];
        this._generation = 0;
        this._destroyed = false;
        this._cancellable = new Gio.Cancellable();
        this.available = false;
        this._watchId = Gio.bus_watch_name(Gio.BusType.SYSTEM, name,
            Gio.BusNameWatcherFlags.NONE,
            () => this._appeared(), () => this._vanished());
    }

    _appeared() {
        if (this._destroyed)
            return;
        const generation = ++this._generation;
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, null,
            this._name, this._path, this._iface, this._cancellable, (_o, res) => {
                try {
                    const proxy = Gio.DBusProxy.new_for_bus_finish(res);
                    this._acceptProxy(proxy, generation);
                } catch {
                    return; // cancelled on destroy/vanish, or the service left again
                }
            });
    }

    _acceptProxy(proxy, generation) {
        // Name loss/reappearance and disable can beat asynchronous construction.
        // A late result must never reconnect handlers or resurrect a source.
        if (this._destroyed || generation !== this._generation)
            return false;
        this._drop();
        this._proxy = proxy;
        this._proxyIds.push(proxy.connect('g-properties-changed', () => this._update()));
        this._proxyIds.push(proxy.connect('g-signal', (_p, _s, signal) => this._signal(signal)));
        this._update();
        return true;
    }

    _vanished() {
        ++this._generation;
        this._cancellable.cancel();
        if (!this._destroyed)
            this._cancellable = new Gio.Cancellable();
        this._drop();
        this.available = false;
        this._reset();
        this.emit();
    }

    _drop() {
        if (this._proxy) {
            this._proxyIds.forEach(id => this._proxy.disconnect(id));
            this._proxyIds = [];
            this._proxy = null;
        }
    }

    _update() {
        if (!this._proxy)
            return;
        const ok = this._read(this._proxy);
        if (!ok)
            this._reset();
        this.available = ok;
        this.emit();
    }

    _signal(_name) {}
    _reset() {}

    _cached(prop) {
        return this._proxy?.get_cached_property(prop)?.deepUnpack() ?? null;
    }

    _setProperty(iface, prop, variant, done) {
        if (this._destroyed || !this._proxy) {
            done(new Error('service not running'));
            return;
        }
        const generation = this._generation;
        this._proxy.call('org.freedesktop.DBus.Properties.Set',
            new GLib.Variant('(ssv)', [iface, prop, variant]),
            Gio.DBusCallFlags.NONE, 5000, this._cancellable, (p, res) => {
                if (this._destroyed || generation !== this._generation) {
                    if (!this._destroyed)
                        done(new Error('service disconnected'));
                    return;
                }
                try {
                    p.call_finish(res);
                    done(null);
                } catch (e) {
                    done(e);
                }
            });
    }

    destroy() {
        this._destroyed = true;
        ++this._generation;
        this._cancellable.cancel();
        if (this._watchId) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = 0;
        }
        this._drop();
        this.clear();
    }
}

export class AsusdProfileSource extends ProxySource {
    constructor() {
        super(ASUSD, '/xyz/ljones', PLATFORM);
        this.profile = null;
        this.choices = [];
        this.chargeLimit = null;
    }

    _read(proxy) {
        const id = proxy.get_cached_property('PlatformProfile')?.unpack();
        if (id === undefined)
            return false;
        this.profile = asusdProfile(id);
        this.choices = asusdChoices(this._cached('PlatformProfileChoices'));
        this.chargeLimit = proxy.get_cached_property('ChargeControlEndThreshold')?.unpack() ?? null;
        return true;
    }

    _reset() {
        this.profile = null;
        this.choices = [];
        this.chargeLimit = null;
    }

    set(profile, done) {
        const id = asusdId(profile);
        if (id === null) {
            done(new Error('unknown profile'));
            return;
        }
        this._setProperty(PLATFORM, 'PlatformProfile', new GLib.Variant('u', id), done);
    }
}

class PpdEndpointSource extends ProxySource {
    constructor(name, path, iface) {
        super(name, path, iface);
        this.profile = null;
        this.choices = [];
    }

    _read(proxy) {
        const name = proxy.get_cached_property('ActiveProfile')?.unpack();
        if (name === undefined)
            return false;
        this.profile = ppdProfile(name);
        let names = [];
        try {
            names = (proxy.get_cached_property('Profiles')?.deepUnpack() ?? [])
                .map(p => p.Profile?.deepUnpack?.() ?? p.Profile)
                .map(ppdProfile).filter(Boolean);
        } catch {
            names = [];
        }
        this.choices = ['quiet', 'balanced', 'performance'].filter(p => names.includes(p));
        return true;
    }

    _reset() {
        this.profile = null;
        this.choices = [];
    }

    set(profile, done) {
        this._setProperty(this._iface, 'ActiveProfile', new GLib.Variant('s', ppdName(profile)), done);
    }
}

/**
 * Watches both the UPower bus name (power-profiles-daemon >= 0.22 / GNOME 47+)
 * and the legacy net.hadess.PowerProfiles name (GNOME <= 46), preferring the
 * former when both are present.
 */
export class PpdProfileSource extends Emitter {
    constructor() {
        super();
        this._endpoints = [
            new PpdEndpointSource(PPD_UPOWER, PPD_UPOWER_PATH, PPD_UPOWER),
            new PpdEndpointSource(PPD_HADESS, PPD_HADESS_PATH, PPD_HADESS),
        ];
        this._ids = this._endpoints.map(ep => ep.connect(() => this.emit()));
    }

    get _active() {
        return this._endpoints.find(ep => ep.available) ?? null;
    }

    get available() {
        return this._active !== null;
    }

    get profile() {
        return this._active?.profile ?? null;
    }

    get choices() {
        return this._active?.choices ?? [];
    }

    get _watchId() {
        return this._endpoints.some(ep => ep._watchId) ? 1 : 0;
    }

    get _proxy() {
        return this._active?._proxy ?? null;
    }

    set(profile, done) {
        const ep = this._active;
        if (!ep) {
            done(new Error('service not running'));
            return;
        }
        ep.set(profile, done);
    }

    destroy() {
        this._endpoints.forEach((ep, i) => {
            ep.disconnect(this._ids[i]);
            ep.destroy();
        });
        this._ids = [];
        this.clear();
    }
}

/** supergfxd current mode (method call, refreshed on its NotifyGfx signal). */
export class GpuModeSource extends ProxySource {
    constructor() {
        super(SUPERGFX, '/org/supergfxctl/Gfx', SUPERGFX);
        this.mode = null;
    }

    _read() {
        this._fetch();
        return this.mode !== null;
    }

    _fetch() {
        const proxy = this._proxy;
        if (!proxy || this._destroyed)
            return;
        const generation = this._generation;
        proxy.call('Mode', null, Gio.DBusCallFlags.NONE, 3000, this._cancellable,
            (p, res) => {
                try {
                    const mode = p.call_finish(res).deepUnpack()[0];
                    if (this._destroyed || generation !== this._generation || this._proxy !== proxy)
                        return;
                    if (mode !== this.mode) {
                        this.mode = mode;
                        this.available = true;
                        this.emit();
                    }
                } catch {
                    // Not fatal: the readout just stays hidden.
                }
            });
    }

    _signal(name) {
        if (name === 'NotifyGfx')
            this._fetch();
    }

    _reset() {
        this.mode = null;
    }
}

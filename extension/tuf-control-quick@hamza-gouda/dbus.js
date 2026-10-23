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
const PPD = 'net.hadess.PowerProfiles';
const SUPERGFX = 'org.supergfxctl.Daemon';

/**
 * Watches one bus name and keeps a proxy to one object while it is owned.
 * Subclasses implement _refresh() (read cached state) and _bound(proxy).
 */
class ProxySource extends Emitter {
    constructor(name, path, iface) {
        super();
        this._name = name;
        this._path = path;
        this._iface = iface;
        this._proxy = null;
        this._proxyIds = [];
        this._cancellable = new Gio.Cancellable();
        this.available = false;
        this._watchId = Gio.bus_watch_name(Gio.BusType.SYSTEM, name,
            Gio.BusNameWatcherFlags.NONE,
            () => this._appeared(), () => this._vanished());
    }

    _appeared() {
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, null,
            this._name, this._path, this._iface, this._cancellable, (_o, res) => {
                try {
                    this._proxy = Gio.DBusProxy.new_for_bus_finish(res);
                } catch {
                    return; // cancelled on destroy, or the service left again
                }
                this._proxyIds.push(this._proxy.connect('g-properties-changed', () => this._update()));
                this._proxyIds.push(this._proxy.connect('g-signal', (_p, _s, signal) => this._signal(signal)));
                this._update();
            });
    }

    _vanished() {
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
        this.available = this._read(this._proxy);
        this.emit();
    }

    _signal(_name) {}
    _reset() {}

    _cached(prop) {
        return this._proxy?.get_cached_property(prop)?.deepUnpack() ?? null;
    }

    _setProperty(iface, prop, variant, done) {
        if (!this._proxy) {
            done(new Error('service not running'));
            return;
        }
        this._proxy.call('org.freedesktop.DBus.Properties.Set',
            new GLib.Variant('(ssv)', [iface, prop, variant]),
            Gio.DBusCallFlags.NONE, 5000, this._cancellable, (p, res) => {
                try {
                    p.call_finish(res);
                    done(null);
                } catch (e) {
                    if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        done(e);
                }
            });
    }

    destroy() {
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

export class PpdProfileSource extends ProxySource {
    constructor() {
        super(PPD, '/net/hadess/PowerProfiles', PPD);
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
        this._setProperty(PPD, 'ActiveProfile', new GLib.Variant('s', ppdName(profile)), done);
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
        this._proxy?.call('Mode', null, Gio.DBusCallFlags.NONE, 3000, this._cancellable,
            (p, res) => {
                try {
                    const mode = p.call_finish(res).deepUnpack()[0];
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

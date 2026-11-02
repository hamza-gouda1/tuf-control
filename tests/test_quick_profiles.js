// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda
// Pure-logic and lifecycle tests for the Quick Settings extension.
// Run: gjs -m tests/test_quick_profiles.js

import System from 'system';
import GLib from 'gi://GLib';
import * as P from '../extension/tuf-control-quick@hamza-gouda/profiles.js';

let failed = 0, passed = 0;
function ok(cond, msg) {
    if (cond) {
        passed++;
    } else {
        failed++;
        print(`FAIL ${msg}`);
    }
}
const eq = (a, b, msg) => ok(JSON.stringify(a) === JSON.stringify(b), `${msg}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);

// --- mappings (ids verified against asusd 6.3 introspection)
eq([P.asusdId('balanced'), P.asusdId('performance'), P.asusdId('quiet')], [0, 1, 2], 'asusd ids');
eq([P.asusdProfile(0), P.asusdProfile(1), P.asusdProfile(2), P.asusdProfile(3)],
    ['balanced', 'performance', 'quiet', null], 'asusd names');
eq(P.asusdChoices([3, 2, 0, 1]), ['quiet', 'balanced', 'performance'], 'choices order, id 3 ignored');
eq(P.asusdChoices([0]), ['balanced'], 'partial choices');
eq(P.asusdChoices(null), [], 'null choices');
eq([P.ppdName('quiet'), P.ppdName('balanced')], ['power-saver', 'balanced'], 'ppd names');
eq([P.ppdProfile('power-saver'), P.ppdProfile('performance'), P.ppdProfile('x')], ['quiet', 'performance', null], 'ppd parse');
eq([P.gpuModeName(0), P.gpuModeName(5), P.gpuModeName(6)], ['Hybrid', 'Dedicated (MUX)', null], 'gpu names');
eq([P.chargeLabel(80), P.chargeLabel(5), P.chargeLabel(null)], ['Charge limit: 80%', null, null], 'charge label');
eq([P.subtitleFor('quiet', 'asusd'), P.subtitleFor(null, 'asusd'), P.subtitleFor(null, null)],
    ['Quiet', 'Unknown', 'Unavailable'], 'subtitle');

// --- model with fake sources
class FakeSource extends P.Emitter {
    constructor(profile = null, choices = []) {
        super();
        this.available = profile !== null;
        this.profile = profile;
        this.choices = choices;
        this.destroyed = false;
        this.sets = [];
    }

    set(p, done) {
        this.sets.push(p);
        done(null);
    }

    destroy() {
        this.destroyed = true;
        this.clear();
    }
}

const asusd = new FakeSource('balanced', ['quiet', 'balanced', 'performance']);
const ppd = new FakeSource('performance', ['quiet', 'balanced']);
const model = new P.ProfileModel([{name: 'asusd', source: asusd}, {name: 'ppd', source: ppd}]);
let events = 0;
const sub = model.connect(() => events++);

eq([model.backend, model.profile], ['asusd', 'balanced'], 'asusd preferred');
asusd.profile = 'performance';
asusd.emit();
eq([events, model.profile], [1, 'performance'], 'external change reflected');

asusd.available = false;
asusd.emit();
eq([model.backend, model.profile, model.choices], ['ppd', 'performance', ['quiet', 'balanced']], 'fallback to ppd');
asusd.available = true;
asusd.emit();
eq(model.backend, 'asusd', 'asusd returns');

let err = 'unset';
model.set('quiet', e => { err = e; });
eq([asusd.sets, err], [['quiet'], null], 'set goes to active source');
model.set('bogus', e => { err = e; });
ok(err instanceof Error && asusd.sets.length === 1, 'unknown profile rejected');

model.disconnect(sub);
ok(model.size === 0, 'listener removed');
model.destroy();
ok(asusd.destroyed && ppd.destroyed, 'sources destroyed');
ok(asusd.size === 0 && ppd.size === 0 && model.size === 0, 'zero leaked handlers');
model.destroy(); // idempotent
let late = 'unset';
model.set('quiet', e => { late = e; });
ok(late instanceof Error, 'set after destroy rejected');

const none = new P.ProfileModel([{name: 'asusd', source: new FakeSource()}]);
eq([none.backend, none.profile, none.choices], [null, null, ['quiet', 'balanced', 'performance']], 'nothing available');
none.destroy();

// --- profile OSD: only changes after startup, bursts coalesced (fake clock)
class Clock {
    constructor() {
        this.now = 0;
        this.timers = new Map();
        this.next = 1;
    }

    schedule(ms, fn) {
        const id = this.next++;
        this.timers.set(id, [this.now + ms, fn]);
        return id;
    }

    cancel(id) {
        if (!this.timers.delete(id))
            throw new Error(`cancel of unknown timer ${id}`);
    }

    advance(ms) {
        this.now += ms;
        for (const [id, [at, fn]] of [...this.timers]) {
            if (at <= this.now && this.timers.delete(id))
                fn();
        }
    }
}

{
    const clock = new Clock();
    const shown = [];
    const a2 = new FakeSource(null, []);
    a2.available = false;
    const p2 = new FakeSource(null, []);
    p2.available = false;
    const m2 = new P.ProfileModel([{name: 'asusd', source: a2}, {name: 'ppd', source: p2}]);
    const osd = new P.ProfileAnnouncer(m2, {
        schedule: (ms, fn) => clock.schedule(ms, fn), cancel: id => clock.cancel(id),
        show: p => shown.push(p),
    });
    const setA = profile => {
        a2.available = true;
        a2.profile = profile;
        a2.emit();
    };
    setA('balanced'); // the proxy comes up: startup value, no OSD
    clock.advance(1000);
    eq(shown, [], 'startup profile not announced');
    setA('performance'); // Fn+F5
    clock.advance(299);
    eq(shown, [], 'not before 300 ms');
    clock.advance(1);
    eq(shown, ['performance'], 'change announced');
    setA('quiet');
    clock.advance(100);
    setA('balanced');
    clock.advance(100);
    setA('performance');
    clock.advance(299);
    eq(shown, ['performance'], 'burst still waiting');
    clock.advance(1);
    eq(shown, ['performance', 'performance'], 'burst of three presses: only the last shown');
    a2.emit(); // e.g. charge limit changed, same profile
    clock.advance(1000);
    eq(shown.length, 2, 'unrelated property change not announced');
    setA('quiet');
    a2.available = false; // asusd stops mid-burst; ppd takes over
    p2.available = true;
    p2.profile = 'balanced';
    a2.emit();
    clock.advance(1000);
    eq(shown.length, 2, 'backend switch: baseline only, pending OSD dropped');
    p2.profile = 'quiet';
    p2.emit();
    clock.advance(300);
    eq(shown.slice(-1), ['quiet'], 'ppd fallback change announced');
    p2.profile = 'performance';
    p2.emit();
    ok(osd.pending && clock.timers.size === 1, 'one timer pending');
    osd.destroy();
    ok(!osd.pending && clock.timers.size === 0, 'destroy cancels the pending OSD');
    clock.advance(1000);
    eq([shown.length, osd.shown], [3, 3], 'nothing after destroy');
    osd.destroy(); // idempotent
    ok(m2.size === 0, 'announcer handler removed from model');
    m2.destroy();
}

// --- extension lifecycle: enable/disable cycles with stand-in sources (no shell needed)
{
    const clock = new Clock();
    const src = new FakeSource('balanced', ['quiet', 'balanced', 'performance']);
    const shown = [];
    let model = null, osd = null;
    const enable = () => {
        model = new P.ProfileModel([{name: 'asusd', source: src}]);
        osd = new P.ProfileAnnouncer(model, {
            schedule: (ms, fn) => clock.schedule(ms, fn), cancel: id => clock.cancel(id),
            show: p => shown.push(p),
        });
    };
    const disable = () => {
        // same order as TufControlQuickExtension.disable()
        osd.destroy();
        model.destroy();
        osd = model = null;
    };
    for (let i = 0; i < 3; i++) {
        src.destroyed = false;
        enable();
        src.profile = i % 2 ? 'quiet' : 'performance';
        src.emit();
        disable(); // OSD still pending when the extension goes away
    }
    clock.advance(1000);
    eq([shown, clock.timers.size, src.size], [[], 0, 0], 'enable/disable x3: no stray OSD, timer or handler');
}

// --- real Gio sources: read-only smoke (construct, let proxies settle, destroy)
const D = await import('../extension/tuf-control-quick@hamza-gouda/dbus.js');
const sources = [new D.AsusdProfileSource(), new D.PpdProfileSource(), new D.GpuModeSource()];
await new Promise(res => GLib.timeout_add(GLib.PRIORITY_DEFAULT, 800, () => { res(); return GLib.SOURCE_REMOVE; }));
print(`  live: asusd=${sources[0].available}/${sources[0].profile} ppd=${sources[1].available}/${sources[1].profile} gpu=${sources[2].mode}`);
const generation = sources[0]._generation;
sources.forEach(s => s.destroy());
const lateProxy = {connect() { throw new Error('late proxy connected after destroy'); }};
ok(!sources[0]._acceptProxy(lateProxy, generation), 'late async proxy cannot resurrect destroyed source');
ok(sources.every(s => s.size === 0 && s._watchId === 0 && s._proxy === null), 'dbus sources fully cleaned up');

print(`${passed} passed, ${failed} failed`);
System.exit(failed ? 1 : 0);

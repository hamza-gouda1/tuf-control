// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda
// Run: gjs -m tests/test_desktop_drag.js
import {DesktopDrag} from '../extension/tuf-control-quick@hamza-gouda/drag_desktop.js';

let passed = 0, failed = 0;
function ok(value, msg) {
    if (value) passed++;
    else { failed++; print(`FAIL ${msg}`); }
}
function eq(a, b, msg) { ok(JSON.stringify(a) === JSON.stringify(b), `${msg}: ${a} !== ${b}`); }
class Win {
    constructor(name, minimized = false) {
        this.name = name; this.minimized = minimized; this.handlers = new Map(); this.next = 1;
        this.actor = {opacity: 210, translation_y: 7, remove_transition() {}};
    }
    connect(_signal, cb) { const id = this.next++; this.handlers.set(id, cb); return id; }
    disconnect(id) { this.handlers.delete(id); }
    minimize() { this.minimized = true; }
    unminimize() { this.minimized = false; }
    unmanaged() { for (const cb of this.handlers.values()) cb(); this.handlers.clear(); }
}
function rig(wins = [new Win('a'), new Win('pre', true)]) {
    const q = [], activated = [], skipped = [];
    const d = new DesktopDrag({
        windows: () => wins, eligible: w => w.name !== 'ineligible', focus: () => wins[0],
        activate: w => activated.push(w), actor: w => w.actor,
        skipEffect: a => skipped.push(a), animationsEnabled: () => true,
        height: () => 1000,
        settle: (actor, props, done) => q.push({actor, props, done}),
    });
    return {d, wins, q, activated, skipped};
}
function complete(q, index = 0) {
    const entry = q[index];
    entry.actor.opacity = entry.props.opacity;
    entry.actor.translation_y = entry.props.translation_y;
    entry.done();
}

// Progress follows the finger, clamps at either edge, and preserves originals.
{
    const {d, wins, q} = rig(); const w = wins[0], a = w.actor;
    ok(d.begin(), 'begin hide'); d.update(.5);
    eq([a.opacity, a.translation_y], [105, 97], 'halfway hide follows down and fades');
    d.update(2); eq([a.opacity, a.translation_y], [0, 187], 'progress clamps at one');
    d.update(-2); eq([a.opacity, a.translation_y], [210, 7], 'progress clamps at zero');
    d.update(.5); d.end(true);
    eq([w.minimized, q[0].props.opacity, q[0].props.translation_y],
        [false, 0, 187], 'settle targets endpoint before minimizing');
    complete(q); eq([w.minimized, a.opacity, a.translation_y], [true, 210, 7], 'hide minimizes and restores actor props');
    ok(d.canReturn, 'owned desktop is returnable');
    ok(wins[1].minimized, 'pre-minimized window remains minimized');
}

// Cancel hide returns actors and drops ownership; cancel restore returns to hidden.
{
    const {d, wins, q} = rig(); const w = wins[0], a = w.actor;
    d.begin(); d.update(.7); d.end(false);
    complete(q); eq([w.minimized, a.opacity, a.translation_y], [false, 210, 7], 'cancel hide settles back');
    ok(!d.canReturn && w.handlers.size === 0, 'cancel hide drops unmanaged signal');
    d.begin(); d.update(1); d.end(true); complete(q);
    d.begin(); ok(!w.minimized, 'restore gesture unminimizes owned window');
    d.update(.5); eq([a.opacity, a.translation_y], [105, -83], 'restore also tracks down');
    d.end(false); complete(q, 2);
    eq([w.minimized, a.opacity, a.translation_y], [true, 210, 7], 'cancel restore re-minimizes');
    ok(d.canReturn, 'cancel restore keeps desktop returnable');
}

// A new gesture resolves an old settle first; stale completion cannot mutate it.
{
    const {d, wins, q} = rig(); const w = wins[0], a = w.actor;
    d.begin(); d.update(.4); d.end(true);
    const stale = q[0].done;
    ok(d.begin(), 'rapid begin starts return after settling hide');
    ok(w.minimized === false, 'rapid begin sees hidden state and starts restore');
    stale(); eq([w.minimized, a.opacity, a.translation_y], [false, 0, -173], 'stale callback ignored');
    d.update(.5); d.end(true); complete(q, 1);
    eq([w.minimized, a.opacity, a.translation_y], [false, 210, 7], 'new settle completes restore');
}

// Immediate disable restores owned windows, preserves pre-minimized windows, and cleans signals.
{
    const {d, wins, q, activated} = rig(); const w = wins[0];
    d.begin(); d.update(1); d.end(true); complete(q);
    d.begin(); d.update(.6); d.restore();
    eq([w.minimized, w.actor.opacity, w.actor.translation_y], [false, 210, 7], 'disable restores immediately');
    ok(!d.canReturn && w.handlers.size === 0, 'disable clears ownership and signals');
    ok(wins[1].minimized, 'disable does not unminimize unrelated window');
    ok(activated.length > 0, 'restoring desktop returns focus');
}

// Unmanaged windows and reduced motion do not leave work behind.
{
    const {d, wins, q} = rig(); const w = wins[0];
    d.begin(); d.update(.3); w.unmanaged(); d.end(true);
    ok(q.length === 0 && !d.canReturn, 'destroyed active window is ignored safely');
    d.destroy(); ok(!d.begin(), 'destroy prevents later gestures');
}
{
    const wins = [new Win('a')]; let skipped = 0;
    const d = new DesktopDrag({windows: () => wins, eligible: () => true, focus: () => wins[0],
        activate() {}, actor: w => w.actor, skipEffect: () => skipped++, animationsEnabled: () => false,
        height: () => 500, settle() { throw new Error('reduced motion must not settle'); }});
    d.begin(); d.update(.8);
    eq([wins[0].actor.opacity, wins[0].actor.translation_y], [210, 7], 'reduced motion keeps actors still');
    d.end(true); ok(wins[0].minimized && skipped > 0, 'reduced motion commits action');
}

print(`desktop drag: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} desktop drag assertions failed`);

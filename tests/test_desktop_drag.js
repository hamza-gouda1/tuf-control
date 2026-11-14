// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda
// Run: gjs -m tests/test_desktop_drag.js
import {DesktopDrag} from '../extension/asusius-quick@hamza-gouda/drag_desktop.js';

let passed = 0, failed = 0;
function ok(value, msg) {
    if (value) passed++;
    else { failed++; print(`FAIL ${msg}`); }
}
function eq(a, b, msg) { ok(JSON.stringify(a) === JSON.stringify(b), `${msg}: ${a} !== ${b}`); }
class Win {
    constructor(name, minimized = false) {
        this.name = name; this.minimized = minimized; this.handlers = new Map(); this.next = 1;
        this.actor = {opacity: 210, translation_x: 4, translation_y: 7,
            scale_x: 1.25, scale_y: 0.75, remove_transition() {}};
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
        tuckOffset: () => ({x: 120, y: 240}),
        settle: (actor, props, done) => q.push({actor, props, done}),
    });
    return {d, wins, q, activated, skipped};
}
function complete(q, index = 0) {
    const {actor, props: {duration: _duration, ...pose}, done} = q[index];
    Object.assign(actor, pose);
    done();
}
const pose = a => [a.opacity, a.translation_x, a.translation_y, a.scale_x, a.scale_y]
    .map(value => Math.round(value * 100) / 100);
const open = [210, 4, 7, 1.25, 0.75];
const tucked = [0, 100, 199, 0.25, 0.15];
const halfway = [158, 52, 103, 0.75, 0.45];

// Progress follows the finger, clamps at either edge, and preserves originals.
{
    const {d, wins, q} = rig(); const w = wins[0], a = w.actor;
    ok(d.begin(), 'begin hide'); d.update(.5);
    eq(pose(a), halfway, 'halfway hide shrinks and moves toward the anchor');
    d.update(2); eq(pose(a), tucked, 'progress clamps at one');
    d.update(-2); eq(pose(a), open, 'progress clamps at zero');
    d.update(.5); d.end(true);
    ok(!w.minimized, 'hide does not minimize before the settle ends');
    eq(pose(q[0].props), tucked, 'settle finishes the same tuck');
    complete(q); eq(pose(a), open, 'hide restores every actor property');
    ok(w.minimized, 'hide minimizes on settle completion');
    ok(d.canReturn, 'owned desktop is returnable');
    ok(wins[1].minimized, 'pre-minimized window remains minimized');
}

// Cancel hide returns actors and drops ownership; cancel restore returns to hidden.
{
    const {d, wins, q} = rig(); const w = wins[0], a = w.actor;
    d.begin(); d.update(.7); d.end(false);
    complete(q); eq(pose(a), open, 'cancel hide settles back');
    ok(!w.minimized, 'cancel hide leaves the window visible');
    ok(!d.canReturn && w.handlers.size === 0, 'cancel hide drops unmanaged signal');
    d.begin(); d.update(1); d.end(true);
    ok(w.minimized && q.length === 1, 'release at the endpoint needs no extra animation');
    d.begin(); ok(!w.minimized, 'restore gesture unminimizes owned window');
    eq(pose(a), tucked, 'restore begins where hide ended');
    d.update(.5); eq(pose(a), halfway, 'restore follows the same path in reverse');
    d.update(.25); ok(a.scale_x < halfway[3], 'reversing the return shrinks the window again');
    d.end(false); complete(q, 1);
    eq(pose(a), open, 'cancel return restores actor properties');
    ok(w.minimized, 'cancel return re-minimizes');
    ok(d.canReturn, 'cancel restore keeps desktop returnable');
}

// A new gesture resolves an old settle first; stale completion cannot mutate it.
{
    const {d, wins, q, activated} = rig(); const w = wins[0], a = w.actor;
    ok(!d.begin(true), 'up with no tucked windows does not start a hide');
    d.begin(false); d.update(.4); d.end(true);
    const stale = q[0].done;
    ok(!d.begin(false) && w.minimized, 'second down finishes the hide without reopening');
    ok(d.begin(true), 'rapid up starts return after settling hide');
    ok(w.minimized === false, 'rapid begin sees hidden state and starts restore');
    stale(); eq(pose(a), tucked, 'stale callback leaves the return pose alone');
    d.update(.5); d.end(true); complete(q, 1);
    eq(pose(a), open, 'new settle completes restore');
    ok(!w.minimized, 'return leaves the window visible');
    eq(activated.map(window => window.name), ['a'], 'return refocuses the previous window');
}

// Immediate disable restores owned windows, preserves pre-minimized windows, and cleans signals.
{
    const {d, wins, activated} = rig(); const w = wins[0];
    d.begin(); d.update(1); d.end(true);
    d.begin(); d.update(.6); d.restore();
    eq(pose(w.actor), open, 'disable restores immediately');
    ok(!w.minimized, 'disable leaves the window visible');
    ok(!d.canReturn && w.handlers.size === 0, 'disable clears ownership and signals');
    ok(wins[1].minimized, 'disable does not unminimize unrelated window');
    ok(activated.length > 0, 'restoring desktop returns focus');
}

// destroy() restores hidden windows without activating/focusing them (lock screen safety).
{
    const {d, wins, activated} = rig(); const w = wins[0];
    d.begin(); d.update(1); d.end(true);
    ok(w.minimized, 'tucked desktop window minimized');
    d.destroy();
    ok(!w.minimized && w.handlers.size === 0 && activated.length === 0,
        'destroy unminimizes tucked window without calling activate()');
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
        tuckOffset() { throw new Error('reduced motion needs no tuck geometry'); },
        settle() { throw new Error('reduced motion must not settle'); }});
    d.begin(); d.update(.8);
    eq(pose(wins[0].actor), open, 'reduced motion keeps actors still');
    d.end(true); ok(wins[0].minimized && skipped > 0, 'reduced motion commits action');
    d.begin(); d.update(.6);
    ok(wins[0].minimized, 'reduced motion return waits for release');
    d.end(true); ok(!wins[0].minimized, 'reduced motion return restores the window');
}

print(`desktop drag: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} desktop drag assertions failed`);

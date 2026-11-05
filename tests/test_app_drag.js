// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda
// Run: gjs -m tests/test_app_drag.js

import * as D from '../extension/tuf-control-quick@hamza-gouda/drag_switcher.js';

let failed = 0, passed = 0;
function ok(cond, msg) {
    if (cond) passed++;
    else { failed++; print(`FAIL ${msg}`); }
}
const eq = (a, b, msg) => ok(JSON.stringify(a) === JSON.stringify(b),
    `${msg}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);

class Window {
    constructor(name, actor, minimized = false) {
        this.name = name;
        this.actor = actor;
        this.minimized = minimized;
        this._ids = 0;
        this._signals = new Map();
    }
    connect(name, fn) {
        const id = ++this._ids;
        this._signals.set(id, [name, fn]);
        return id;
    }
    disconnect(id) { this._signals.delete(id); }
    emit(name) {
        for (const [signal, fn] of [...this._signals.values()])
            if (signal === name) fn(this);
    }
    unminimize() { this.minimized = false; }
    minimize() { this.minimized = true; }
}

function actor(x, y, opacity) {
    return {
        translation_x: x, translation_y: y, opacity,
        remove_all_transitions() { this.removedTransitions = true; },
    };
}

function fixture({reduced = false, minimized = false, queued = false} = {}) {
    const a = actor(10, 4, 255), b = actor(-7, 8, 180), c = actor(31, 2, 220);
    const w1 = new Window('one', a), w2 = new Window('two', b, minimized), w3 = new Window('three', c);
    const wins = [w1, w2, w3];
    const stack = [a, b, c];
    const raised = [], restored = [], activated = [], skipped = [], settlements = [];
    const drag = new D.AppDrag({
        windows: () => wins,
        focus: () => w1,
        actor: w => w.actor,
        width: () => 100,
        stack: () => stack,
        raise: ac => raised.push(ac),
        restoreStack: order => restored.push([...order]),
        skipEffect: ac => skipped.push(ac),
        activate: w => activated.push(w),
        animationsEnabled: () => !reduced,
        settle: (ac, props, done) => {
            settlements.push({ac, props, done});
            if (!queued) {
                Object.assign(ac, {translation_x: props.translation_x, opacity: props.opacity});
                done();
            }
        },
    });
    return {drag, a, b, c, w1, w2, w3, wins, stack, raised, restored, activated, skipped, settlements};
}

// Next is a leftward pair; progress is clamped and preserves original y.
{
    const f = fixture();
    ok(f.drag.begin(1), 'begin next app');
    f.drag.update(0.5);
    eq([f.a.translation_x, f.b.translation_x, f.a.translation_y, f.b.translation_y],
        [-40, 43, 4, 8], 'next midpoint positions');
    f.drag.update(2);
    eq([f.a.translation_x, f.b.translation_x], [-90, -7], 'progress clamps at end');
    f.drag.update(-1);
    eq([f.a.translation_x, f.b.translation_x], [10, 93], 'progress clamps at start');
    f.drag.cancel();
    eq([f.a.translation_x, f.a.translation_y, f.a.opacity, f.b.translation_x, f.b.translation_y, f.b.opacity],
        [10, 4, 255, -7, 8, 180], 'cancel restores exact actor state');
}

// Previous app moves right and the incoming app moves from the left.
{
    const f = fixture();
    ok(f.drag.begin(-1), 'begin previous app');
    f.drag.update(0.25);
    eq([f.a.translation_x, f.c.translation_x], [35, -44], 'previous midpoint direction');
    f.drag.cancel();
}

// A minimized destination is temporarily unminimized, then put back on cancel.
{
    const f = fixture({minimized: true});
    ok(f.drag.begin(1), 'begin with minimized destination');
    eq([f.w2.minimized, f.skipped], [false, [f.b]], 'destination unminimized without effect');
    f.drag.update(0.4);
    f.drag.cancel();
    eq([f.w2.minimized, f.skipped.length, f.restored.length], [true, 2, 1],
        'cancel re-minimizes and restores stacking');
}

// End commits activation after both actors settle and restores actor transforms.
{
    const f = fixture();
    f.drag.begin(1);
    f.drag.update(0.75);
    f.drag.end(true);
    eq([f.drag.active, f.activated, f.a.translation_x, f.b.translation_x],
        [false, [f.w2], 10, -7], 'commit activates target and restores actor transforms');
    eq(f.restored, [f.stack], 'commit restores original stack order');
}

// Reduced motion keeps actors still and activation occurs only on commit.
{
    const f = fixture({reduced: true});
    f.drag.begin(1);
    f.drag.update(0.8);
    eq([f.a.translation_x, f.b.translation_x, f.activated.length], [10, -7, 0],
        'reduced motion has no preview transform');
    f.drag.end(true);
    eq(f.activated, [f.w2], 'reduced motion commits target');
}

// Unmanaged windows cancel safely and disconnect all remaining handlers.
{
    const f = fixture({minimized: true});
    f.drag.begin(1);
    f.drag.update(0.4);
    f.w2.emit('unmanaged');
    eq([f.drag.active, f.w2.minimized, f.a.translation_x, f.w1._signals.size, f.w2._signals.size],
        [false, false, 10, 0, 0], 'target unmanaged restores survivor and disconnects signals');
}

// Destroy invalidates queued settle completions; a later session stays owned.
{
    const f = fixture({queued: true});
    f.drag.begin(1);
    f.drag.update(0.7);
    f.drag.end(true);
    const stale = f.settlements.map(s => s.done);
    f.drag.destroy();
    eq([f.drag.active, f.a.translation_x, f.b.translation_x], [false, 10, -7],
        'destroy restores active preview');
    f.drag.begin(-1);
    for (const done of stale) done();
    eq([f.drag.active, f.activated.length], [true, 0], 'stale completion cannot finish new session');
    f.drag.cancel();
}

print(`${passed} passed, ${failed} failed`);
if (failed)
    imports.system.exit(1);

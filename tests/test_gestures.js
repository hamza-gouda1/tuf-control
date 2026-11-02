// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda
// Decision-maths and watcher tests for the touchpad gestures.
// Run: gjs -m tests/test_gestures.js

import System from 'system';
import GLib from 'gi://GLib';
import * as L from '../extension/tuf-control-quick@hamza-gouda/gesture_logic.js';
import * as G from '../extension/tuf-control-quick@hamza-gouda/gestures.js';

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

const {PHASE_BEGIN, PHASE_UPDATE, PHASE_END, PHASE_CANCEL} = L;

// --- tables -------------------------------------------------------------------
eq(L.GESTURES,
    ['swipe3-down', 'swipe4-up', 'swipe4-down', 'swipe4-left', 'swipe4-right',
        'pinch-in', 'pinch-out'], 'gestures');
eq(L.ACTIONS,
    ['none', 'overview', 'app-grid', 'show-desktop', 'switch-windows',
        'prev-workspace', 'next-workspace', 'launch-app'], 'actions');
eq([L.SWIPE_DISTANCE, L.PINCH_SCALE, L.DOMINANCE], [50, 0.25, 1.4], 'thresholds');

// --- Clutter constants ------------------------------------------------------------
// Clutter's typelib ships with mutter, outside gjs's default search path. Load
// it when it is there (GI_TYPELIB_PATH) to check the values this file and
// gesture_logic.js rely on against the real thing; otherwise the same verified
// mutter 46 values stand in for the watcher tests.
const FALLBACK = {
    EventType: {TOUCHPAD_SWIPE: 14, TOUCHPAD_PINCH: 13, BUTTON_PRESS: 6},
    TouchpadGesturePhase: {BEGIN: 0, UPDATE: 1, END: 2, CANCEL: 3},
    EVENT_PROPAGATE: false,
};
let Clutter = null;
try {
    Clutter = (await import('gi://Clutter')).default;
} catch {
}
if (Clutter) {
    eq([FALLBACK.EventType.TOUCHPAD_SWIPE, FALLBACK.EventType.TOUCHPAD_PINCH,
        FALLBACK.EventType.BUTTON_PRESS],
    [Clutter.EventType.TOUCHPAD_SWIPE, Clutter.EventType.TOUCHPAD_PINCH,
        Clutter.EventType.BUTTON_PRESS], 'event types match Clutter 46');
    eq([FALLBACK.TouchpadGesturePhase.BEGIN, FALLBACK.TouchpadGesturePhase.UPDATE,
        FALLBACK.TouchpadGesturePhase.END, FALLBACK.TouchpadGesturePhase.CANCEL],
    [Clutter.TouchpadGesturePhase.BEGIN, Clutter.TouchpadGesturePhase.UPDATE,
        Clutter.TouchpadGesturePhase.END, Clutter.TouchpadGesturePhase.CANCEL],
    'phases match Clutter 46');
    ok(FALLBACK.EVENT_PROPAGATE === Clutter.EVENT_PROPAGATE, 'EVENT_PROPAGATE matches');
    eq([PHASE_BEGIN, PHASE_UPDATE, PHASE_END, PHASE_CANCEL],
        [Clutter.TouchpadGesturePhase.BEGIN, Clutter.TouchpadGesturePhase.UPDATE,
            Clutter.TouchpadGesturePhase.END, Clutter.TouchpadGesturePhase.CANCEL],
        'tracker phases match Clutter 46');
}
const CT = Clutter ?? FALLBACK;
print(`  clutter: ${Clutter ? 'typelib loaded, constants checked' : 'not on the default path, mutter 46 values stand in'}`);

// --- the gschema matches the logic --------------------------------------------------
const SCHEMA = 'extension/tuf-control-quick@hamza-gouda/schemas/'
    + 'org.gnome.shell.extensions.tuf-control-quick.gschema.xml';
const [xmlOk, raw] = GLib.file_get_contents(SCHEMA);
ok(xmlOk, 'gschema found (run from the repo root)');
const xml = typeof raw === 'string' ? raw : new TextDecoder().decode(raw);
const keyBlock = name => xml.match(new RegExp(`<key name="${name}"[\\s\\S]*?</key>`))?.[0] ?? '';
for (const g of L.GESTURES) {
    const block = keyBlock(`gesture-${g}`);
    ok(block.includes(`<default>'none'</default>`), `gesture-${g} defaults to none`);
    for (const a of L.ACTIONS)
        ok(block.includes(`<choice value='${a}'/>`), `gesture-${g} allows ${a}`);
}
ok(keyBlock('gesture-enabled').includes('<default>false</default>'),
    'gesture-enabled defaults to off');
ok(keyBlock('gesture-launch-app').includes(`<default>''</default>`),
    'gesture-launch-app defaults to empty');

// --- swipe distance threshold --------------------------------------------------------
const runSwipe = (fingers, steps) => {
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, fingers, 0, 0);
    for (const [dx, dy] of steps)
        t.swipe(PHASE_UPDATE, fingers, dx, dy);
    return t.swipe(PHASE_END, fingers, 0, 0);
};
eq(runSwipe(3, [[0, 30], [0, 19]]), null, '49 px is below the 50 px threshold');
eq(runSwipe(3, [[0, 30], [0, 20]]), 'swipe3-down', '50 px completes the gesture');
eq(runSwipe(3, [[0, 300]]), 'swipe3-down', 'one long swipe counts as one gesture');

// --- 3-finger: down only, the shell owns the rest --------------------------------------
eq(runSwipe(3, [[0, -300]]), null, '3-finger up is the shell\'s');
eq(runSwipe(3, [[300, 0]]), null, '3-finger horizontal is the shell\'s');
eq(runSwipe(3, [[300, 0], [300, 400]]), null, 'horizontal wander kills a 3-finger down');

// --- 4-finger mapping -------------------------------------------------------------------
eq(runSwipe(4, [[0, -200]]), 'swipe4-up', '4-finger up');
eq(runSwipe(4, [[0, 200]]), 'swipe4-down', '4-finger down');
eq(runSwipe(4, [[-200, 0]]), 'swipe4-left', '4-finger left');
eq(runSwipe(4, [[200, 0]]), 'swipe4-right', '4-finger right');

// --- axis dominance -----------------------------------------------------------------------
eq(runSwipe(4, [[140, 190]]), null, '190 down over 140 right is too diagonal (190 < 196)');
eq(runSwipe(4, [[140, 200]]), 'swipe4-down', '200 down over 140 right just dominates (200 >= 196)');
eq(runSwipe(4, [[100, 145]]), 'swipe4-down', 'dominance boundary counts (145 >= 140)');
eq(runSwipe(4, [[200, 100]]), 'swipe4-right', '200 right over 100 down dominates');
eq(runSwipe(4, [[100, 300]]), 'swipe4-down', 'the dominant axis is the one that decides');
eq(runSwipe(4, [[0, 40]]), null, 'dominant but below the distance: nothing');
eq(runSwipe(4, [[-140, -220]]), 'swipe4-up',
    'signs set the direction, the maths stays absolute');
eq(runSwipe(4, [[-200, -100]]), 'swipe4-left', 'negative horizontal maps left');
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 4, 0, 0);
    t.swipe(PHASE_UPDATE, 4, 0, 60);
    eq(t.swipe(PHASE_END, 4, 500, 0), 'swipe4-down', 'deltas at END are ignored');
}

// --- finger counts --------------------------------------------------------------------------
eq(runSwipe(2, [[0, 300]]), null, '2-finger swipes are not ours');
eq(runSwipe(5, [[0, 300]]), null, '5-finger swipes are not ours');

// a mid-gesture finger change starts the gesture over
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 4, 0, 0);
    t.swipe(PHASE_UPDATE, 3, 0, 500); // mutter switched fingers: drop everything
    eq(t.swipe(PHASE_END, 3, 0, 0), null, 'finger count changed mid-gesture: silent');
    t.swipe(PHASE_BEGIN, 3, 0, 0);
    t.swipe(PHASE_UPDATE, 3, 0, 60);
    eq(t.swipe(PHASE_END, 3, 0, 0), 'swipe3-down', 'the next gesture is unaffected');
}

// --- pinch -------------------------------------------------------------------------------------
const runPinch = steps => {
    const t = new L.GestureTracker();
    t.pinch(PHASE_BEGIN, 2, 0, 0);
    for (const [dx, dy] of steps)
        t.pinch(PHASE_UPDATE, 2, dx, dy);
    return t.pinch(PHASE_END, 2, 0, 0);
};
eq(runPinch([[0, 1.1], [0, 1.2], [0, 1.26]]), 'pinch-out', 'total scale +0.26 grows');
eq(runPinch([[0, 0.8], [0, 0.7]]), 'pinch-in', 'total scale -0.3 shrinks');
eq(runPinch([[0, 1.2], [0, 1.24]]), null, 'total scale +0.24 is too small');
eq(runPinch([[0, 1.125], [0, 1.25]]), 'pinch-out', 'exactly +0.25 counts');
eq(runPinch([[0, 0.875], [0, 0.75]]), 'pinch-in', 'exactly -0.25 counts');
eq(runPinch([[1000, 1], [-1000, 1]]), null, 'angle deltas (dx) never count');
eq(runPinch([[0, 1.2], [0, 1.0]]), null, 'a pinch that undoes itself is nothing');

// --- reset between gestures ----------------------------------------------------------------------
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 3, 0, 0);
    t.swipe(PHASE_UPDATE, 3, 0, 119);
    t.swipe(PHASE_BEGIN, 3, 0, 0); // fingers land again: from zero, not 119
    t.swipe(PHASE_UPDATE, 3, 0, 60);
    t.swipe(PHASE_UPDATE, 3, 0, 60);
    eq(t.swipe(PHASE_END, 3, 0, 0), 'swipe3-down', 'BEGIN resets the previous accumulation');
}
{
    const t = new L.GestureTracker();
    eq(t.swipe(PHASE_UPDATE, 4, 0, 500), null, 'UPDATE before BEGIN is dropped');
    eq(t.swipe(PHASE_END, 4, 0, 0), null, 'END without BEGIN is silent');
}
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 4, 0, 0);
    t.swipe(PHASE_UPDATE, 4, 0, -150);
    eq(t.swipe(PHASE_END, 4, 0, 0), 'swipe4-up', 'first gesture fires');
    eq(t.swipe(PHASE_END, 4, 0, 0), null, 'END after END stays silent');
    t.swipe(PHASE_UPDATE, 4, 0, 500); // a stray event between gestures
    eq(t.swipe(PHASE_END, 4, 0, 0), null, 'stray UPDATE after a gesture is dropped');
}
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 4, 0, 0);
    t.swipe(PHASE_UPDATE, 4, 0, -500);
    eq(t.swipe(PHASE_CANCEL, 4, 0, 0), null, 'cancelled gestures never fire');
    eq(t.swipe(PHASE_END, 4, 0, 0), null, 'nothing is left after a cancel');
}
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 3, 0, 0);
    t.swipe(PHASE_UPDATE, 3, 0, 300);
    eq(t.pinch(PHASE_END, 3, 0, 0), null, 'a swipe does not end as a pinch');
}
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 4, 0, 0);
    t.swipe(PHASE_UPDATE, 4, 100, 0);
    t.swipe(PHASE_UPDATE, 4, 40, 0);
    eq(t.swipe(PHASE_END, 4, 0, 0), 'swipe4-right', 'consecutive gesture 1');
    t.swipe(PHASE_BEGIN, 4, 0, 0);
    t.swipe(PHASE_UPDATE, 4, 100, 0);
    t.swipe(PHASE_UPDATE, 4, 40, 0);
    eq(t.swipe(PHASE_END, 4, 0, 0), 'swipe4-right', 'consecutive gesture 2, from zero');
}
{
    const t = new L.GestureTracker();
    t.swipe(PHASE_BEGIN, 4, 0, 0);
    t.swipe(PHASE_UPDATE, 4, 0, 500);
    t.reset();
    eq(t.swipe(PHASE_END, 4, 0, 0), null, 'reset() forgets a half-finished gesture');
}

// --- resolveAction: the live bindings ---------------------------------------------------------------
function fakeSettings(bindings = {}, enabled = true) {
    const asked = [];
    return {
        asked,
        get_boolean(key) {
            asked.push(key);
            return key === 'gesture-enabled' ? enabled : false;
        },
        get_string(key) {
            asked.push(key);
            return bindings[key] ?? 'none';
        },
    };
}

{
    const off = fakeSettings({'gesture-swipe4-left': 'overview'}, false);
    eq(L.resolveAction(off, 'swipe4-left'), null, 'disabled: nothing fires');
    eq(off.asked, ['gesture-enabled'], 'disabled: only the switch is read');
}
{
    const on = fakeSettings({'gesture-swipe4-left': 'overview'});
    eq(L.resolveAction(on, 'swipe4-left'), 'overview', 'bound action returned');
    eq(on.asked, ['gesture-enabled', 'gesture-swipe4-left'],
        'the binding is read live under its key');
}
eq(L.resolveAction(fakeSettings(), 'swipe4-right'), null, "'none' binds nothing");
eq(L.resolveAction(fakeSettings({'gesture-pinch-out': 'bogus'}), 'pinch-out'), null,
    'unknown action values are ignored');
eq(L.resolveAction(fakeSettings({'gesture-pinch-in': 'launch-app'}), 'pinch-in'),
    'launch-app', 'launch-app is an action like the rest');

// --- the watcher: fake stage, fake events, real tracker ---------------------------------------------
const SWIPE = CT.EventType.TOUCHPAD_SWIPE;
const PINCH = CT.EventType.TOUCHPAD_PINCH;
const BUTTON = CT.EventType.BUTTON_PRESS;
const ev = (type, phase, fingers, dx, dy) => ({
    type: () => type,
    get_gesture_phase: () => phase,
    // Mutter 14's name (clutter_event_get_touchpad_gesture_finger_count)
    get_touchpad_gesture_finger_count: () => fingers,
    get_gesture_motion_delta: () => [dx, dy],
    get_gesture_pinch_angle_delta: () => dx,
    get_gesture_pinch_scale: () => dy,
});

function makeWatcher({bindings = {}, enabled = true, overview = false,
                      canReturn = false} = {}) {
    const fired = [];
    const actions = {
        overviewVisible: () => overview,
        showDesktopCanReturn: () => canReturn,
        overview: () => fired.push('overview'),
        'app-grid': () => fired.push('app-grid'),
        'show-desktop': () => fired.push('show-desktop'),
        'switch-windows': () => fired.push('switch-windows'),
        'prev-workspace': () => fired.push('prev-workspace'),
        'next-workspace': () => fired.push('next-workspace'),
        'launch-app': () => fired.push('launch-app'),
    };
    const stage = {
        handlers: [],
        disconnects: 0,
        connect(_sig, fn) {
            this.handlers.push(fn);
            return this.handlers.length;
        },
        disconnect() {
            this.disconnects++;
            this.handlers[0] = null;
        },
    };
    const settings = fakeSettings(bindings, enabled);
    const watcher = new G.GestureWatcher({settings, stage, actions, clutter: CT,
                                          log: () => {}});
    watcher.enable();
    const handle = stage.handlers[0];
    // one full swipe through the handler; returns every answer it gave
    const swipe = (fingers, steps, type = SWIPE) => {
        const answers = [handle(null, ev(type, PHASE_BEGIN, fingers, 0, 0))];
        for (const [dx, dy] of steps)
            answers.push(handle(null, ev(type, PHASE_UPDATE, fingers, dx, dy)));
        answers.push(handle(null, ev(type, PHASE_END, fingers, 0, 0)));
        return answers;
    };
    return {watcher, stage, settings, fired, swipe};
}

{
    const wt = makeWatcher();
    ok(wt.stage.handlers.length === 1 && wt.stage.disconnects === 0,
        'enable() connects exactly one handler');
    wt.watcher.disable();
    ok(wt.stage.disconnects === 1 && wt.stage.handlers[0] === null,
        'disable() removes the handler');
    wt.watcher.disable();
    ok(wt.stage.disconnects === 1, 'disable() twice changes nothing');
}
{
    // switched off: not even the bindings are looked at
    const wt = makeWatcher({
        enabled: false,
        bindings: {'gesture-swipe4-up': 'overview'},
    });
    const answers = wt.swipe(4, [[0, -80], [0, -80]]);
    eq(wt.fired, [], 'disabled: a full gesture does nothing');
    ok(answers.every(a => a === CT.EVENT_PROPAGATE),
        'disabled: every event still propagates');
    eq(wt.settings.asked, Array(4).fill('gesture-enabled'),
        'disabled: only the switch is read, per event');
}
{
    const wt = makeWatcher({bindings: {'gesture-swipe4-up': 'overview'}});
    const answers = wt.swipe(4, [[0, -80], [0, -80]]);
    eq(wt.fired, ['overview'], 'enabled: the bound action fires once, at END');
    ok(answers.every(a => a === CT.EVENT_PROPAGATE),
        'enabled: firing still propagates every event');
}
{
    const wt = makeWatcher({
        bindings: {'gesture-swipe3-down': 'app-grid'},
        overview: true,
    });
    wt.swipe(3, [[0, 60], [0, 60]]);
    eq(wt.fired, [], '3-finger down while the overview is up: other bindings skip');
    eq(wt.settings.asked,
       [...Array(5).fill('gesture-enabled'), 'gesture-swipe3-down'],
       'other bindings: read once to decide, then hands off to the shell');
}
{
    // show-desktop + overview + we minimised the app: the swipe brings it back
    const wt = makeWatcher({
        bindings: {'gesture-swipe3-down': 'show-desktop'},
        overview: true,
        canReturn: true,
    });
    wt.swipe(3, [[0, 60], [0, 60]]);
    eq(wt.fired, ['show-desktop'], 'overview: show-desktop returns the last app');
    const wt2 = makeWatcher({
        bindings: {'gesture-swipe3-down': 'show-desktop'},
        overview: true,
        canReturn: false,   // the overview just covers visible windows: hands off
    });
    wt2.swipe(3, [[0, 60], [0, 60]]);
    eq(wt2.fired, [], 'overview without our desktop: the shell keeps the gesture');
    const wt3 = makeWatcher({
        bindings: {'gesture-swipe3-down': 'show-desktop'},
        overview: false,
    });
    wt3.swipe(3, [[0, 60], [0, 60]]);
    eq(wt3.fired, ['show-desktop'], '3-finger down on the desktop fires');
}
{
    const wt = makeWatcher({bindings: {'gesture-swipe4-left': 'none'}});
    wt.swipe(4, [[-80, 0], [-80, 0]]);
    eq(wt.fired, [], "bound to 'none': nothing fires");
}
{
    // a key press between BEGIN and END is none of our business
    const wt = makeWatcher({bindings: {'gesture-swipe4-right': 'prev-workspace'}});
    const handle = wt.stage.handlers[0];
    handle(null, ev(SWIPE, PHASE_BEGIN, 4, 0, 0));
    handle(null, ev(SWIPE, PHASE_UPDATE, 4, 80, 0));
    eq(handle(null, ev(BUTTON, 0, 0, 0, 0)), CT.EVENT_PROPAGATE,
        'other events propagate untouched');
    handle(null, ev(SWIPE, PHASE_UPDATE, 4, 40, 0));
    handle(null, ev(SWIPE, PHASE_END, 4, 0, 0));
    eq(wt.fired, ['prev-workspace'], 'the gesture completes across other events');
}
{
    const wt = makeWatcher({bindings: {'gesture-pinch-out': 'next-workspace'}});
    const handle = wt.stage.handlers[0];
    handle(null, ev(PINCH, PHASE_BEGIN, 2, 0, 0));
    handle(null, ev(PINCH, PHASE_UPDATE, 2, 0, 1.15));
    handle(null, ev(PINCH, PHASE_UPDATE, 2, 0, 1.30));
    eq(handle(null, ev(PINCH, PHASE_END, 2, 0, 0)), CT.EVENT_PROPAGATE,
        'pinch END propagates');
    eq(wt.fired, ['next-workspace'], 'pinch out runs its binding through the watcher');
}
{
    // disable() mid-gesture forgets it: the next enable starts clean
    const wt = makeWatcher({bindings: {'gesture-swipe3-down': 'overview'}});
    const handle = wt.stage.handlers[0];
    handle(null, ev(SWIPE, PHASE_BEGIN, 3, 0, 0));
    handle(null, ev(SWIPE, PHASE_UPDATE, 3, 0, 100));
    wt.watcher.disable();
    wt.watcher.enable();
    ok(wt.stage.handlers.length === 2, 'enable() after disable() connects again');
    const fresh = wt.stage.handlers[1];
    fresh(null, ev(SWIPE, PHASE_BEGIN, 3, 0, 0));
    fresh(null, ev(SWIPE, PHASE_UPDATE, 3, 0, 30)); // 30 px, would count with a 50 threshold
    eq(fresh(null, ev(SWIPE, PHASE_END, 3, 0, 0)), CT.EVENT_PROPAGATE, 'END propagates');
    eq(wt.fired, [], 'a half-finished gesture does not survive disable()');
    wt.watcher.disable();
}

// Shell actions run against an isolated window model, including multi-window
// restoration, destroyed windows and disable while the desktop is shown.
{
    const originalGlobal = globalThis.global;
    const makeWindow = (minimized = false) => ({
        minimized, skip_taskbar: false, activated: 0, handlers: new Map(),
        can_minimize: () => true, is_on_all_workspaces: () => false,
        connect(_name, fn) { this.handlers.set(1, fn); return 1; },
        disconnect(id) { this.handlers.delete(id); },
        minimize() { this.minimized = true; },
        unminimize() { this.minimized = false; },
        activate() { this.activated++; },
    });
    const one = makeWindow(), two = makeWindow(), pre = makeWindow(true);
    const workspace = {list_windows: () => [one, two, pre]};
    globalThis.global = {
        workspace_manager: {get_active_workspace: () => workspace},
        display: {focus_window: one}, get_current_time: () => 1,
    };
    try {
        const actions = G.shellActions({overview: {}}, {}, {});
        actions['show-desktop']();
        eq([one.minimized, two.minimized, pre.minimized], [true, true, true],
            'show desktop minimizes visible active-workspace windows');
        actions['show-desktop']();
        eq([one.minimized, two.minimized, pre.minimized, one.activated],
            [false, false, true, 1], 'toggle restores every changed window and focus');
        actions['show-desktop']();
        two.handlers.get(1)();
        actions.destroy();
        ok(!one.minimized && two.minimized && !actions.showDesktopCanReturn(),
            'disable restores surviving windows and forgets destroyed windows');
        ok(one.handlers.size === 0, 'disable disconnects window handlers');
    } finally {
        globalThis.global = originalGlobal;
    }
}

print(`${passed} passed, ${failed} failed`);
System.exit(failed ? 1 : 0);

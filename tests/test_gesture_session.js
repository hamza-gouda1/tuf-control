// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda

import {GestureSession} from '../extension/tuf-control-quick@hamza-gouda/gesture_session.js';
import {PHASE_BEGIN as BEGIN, PHASE_UPDATE as UPDATE, PHASE_END as END,
    PHASE_CANCEL as CANCEL} from '../extension/tuf-control-quick@hamza-gouda/gesture_logic.js';

let passed = 0;
function equal(got, want, message) {
    if (JSON.stringify(got) !== JSON.stringify(want))
        throw new Error(`${message}: ${JSON.stringify(got)} vs ${JSON.stringify(want)}`);
    passed++;
}

function fixture(desktopHidden = false) {
    const calls = [];
    const bindings = {'swipe3-down': 'show-desktop', 'swipe3-left': 'next-window',
        'swipe3-right': 'prev-window', 'swipe4-up': 'volume-up', 'swipe4-down': 'volume-down',
        hold4: 'play-pause'};
    const drag = name => ({
        begin: step => calls.push([name, 'begin', step ?? null]),
        update: value => calls.push([name, 'progress', value]),
        end: commit => calls.push([name, 'end', commit]),
        cancel: () => {},
        restore: () => {},
    });
    const input = {
        prepareOverview: () => calls.push('prepare-overview'),
        dragAllowed: () => true,
        mediaAllowed: () => true,
        desktop: {
            ...drag('desktop'),
            begin: returning => {
                if (returning !== desktopHidden)
                    return false;
                calls.push(['desktop', 'begin', returning]);
                return true;
            },
        },
        apps: drag('apps'),
    };
    const settings = {get_boolean: () => true, get_string: key => bindings[key.slice(8)] ?? 'none'};
    const actions = Object.fromEntries(['volume-up', 'volume-down', 'play-pause'].map(
        action => [action, () => calls.push(action)]));
    return {calls, input, session: new GestureSession(settings, actions, input)};
}

{
    const {session, calls} = fixture();
    session.swipe(BEGIN, 3, 0, 0, 0);
    session.swipe(UPDATE, 3, 0, 80, 100);
    session.swipe(UPDATE, 3, 0, -40, 170);
    session.swipe(END, 3, 0, 0, 300);
    equal(calls, [['desktop', 'begin', false], ['desktop', 'progress', 0.4],
        ['desktop', 'progress', 0.2], ['desktop', 'end', false]],
    'desktop tracks reversal before release and cancels below threshold');
}
{
    const {session, calls} = fixture();
    session.swipe(BEGIN, 3, 0, 0, 0);
    session.swipe(UPDATE, 3, -56, 0, 60);
    session.swipe(END, 3, 0, 0, 65);
    equal(calls, [['apps', 'begin', 1], ['apps', 'progress', 0.2],
        ['apps', 'end', true]], 'a deliberate left flick selects the next app');
    calls.length = 0;
    session.swipe(BEGIN, 3, 0, 0, 100);
    session.swipe(UPDATE, 3, 140, 0, 150);
    session.swipe(CANCEL, 3, 0, 0, 160);
    equal(calls, [['apps', 'begin', -1], ['apps', 'progress', 0.5],
        ['apps', 'end', false]], 'right previews the previous app and cancellation keeps focus');
}
{
    const {session, calls} = fixture();
    session.swipe(BEGIN, 3, 0, 0, 0);
    equal(session.swipe(UPDATE, 3, 0, -40, 40), false, 'up movement propagates to GNOME');
    equal(calls, ['prepare-overview'], 'up forwards its first movement to the native overview');
    equal(session.swipe(END, 3, 0, 0, 80), false, 'up release belongs to GNOME');
}
{
    const {session, calls} = fixture(true);
    session.swipe(BEGIN, 3, 0, 0, 0);
    equal(session.swipe(UPDATE, 3, 0, 120, 100), true, 'down is consumed on the tucked desktop');
    session.swipe(END, 3, 0, 0, 110);
    equal(calls, [], 'a second down does not reopen windows');

    session.swipe(BEGIN, 3, 0, 0, 200);
    equal(session.swipe(UPDATE, 3, 0, -120, 300), true, 'up restores instead of opening Overview');
    equal(session.swipe(END, 3, 0, 0, 310), true, 'the return gesture owns its release');
    equal(calls, [['desktop', 'begin', true], ['desktop', 'progress', 0.6],
        ['desktop', 'end', true]], 'up commits a return along the tuck path');
}
{
    const {session, calls, input} = fixture();
    input.dragAllowed = () => false;
    equal(session.swipe(BEGIN, 3, 0, 0, 0), false, 'overview gestures bypass the desktop owner');
    equal(calls, [], 'overview never holds native input');
}
{
    const {session, calls} = fixture();
    session.hold(BEGIN, 4, 0);
    session.swipe(BEGIN, 4, 0, 0, 70);
    session.swipe(UPDATE, 4, 0, -57, 120);
    session.swipe(UPDATE, 4, 0, 29, 160);
    session.swipe(END, 4, 0, 0, 180);
    session.hold(END, 4, 200);
    equal(calls, ['volume-up', 'volume-up', 'volume-down'],
        'volume steps during travel, reverses, and never fires a pending hold');
    session.hold(BEGIN, 4, 300);
    session.hold(CANCEL, 4, 500);
    session.hold(BEGIN, 4, 600);
    session.hold(END, 4, 840);
    equal(calls.at(-1), 'play-pause', 'stationary four-finger release triggers playback once');
    equal(calls.filter(action => action === 'play-pause').length, 1, 'cancelled hold never triggers');
}
{
    const {session, calls} = fixture();
    session.swipe(BEGIN, 3, 0, 0, 0);
    session.swipe(UPDATE, 3, 0, 70, 40);
    session.cancel();
    equal(session.swipe(END, 3, 0, 0, 70), false, 'interruption releases ownership');
    equal(calls.at(-1), ['desktop', 'end', false], 'interruption cancels the preview');
}

print(`gesture session: ${passed} passed, 0 failed`);

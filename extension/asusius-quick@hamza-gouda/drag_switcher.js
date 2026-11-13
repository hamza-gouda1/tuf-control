// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) Hamza Gouda

// A two-window preview for touchpad drag switching. It animates the existing
// compositor actors; it never creates window clones or frame callbacks.
export class AppDrag {
    constructor(deps) {
        this._d = deps;
        this._session = null;
    }

    get active() { return this._session !== null; }

    begin(step) {
        if (this._session || (step !== -1 && step !== 1))
            return false;

        const windows = [...this._d.windows()];
        const source = this._d.focus();
        const sourceIndex = windows.indexOf(source);
        if (sourceIndex < 0 || windows.length < 2)
            return false;

        const target = windows[(sourceIndex + step + windows.length) % windows.length];
        if (!target || target === source)
            return false;

        const sourceActor = this._d.actor(source);
        const targetActor = this._d.actor(target);
        if (!sourceActor || !targetActor)
            return false;

        const sourceState = this._snapshot(sourceActor);
        const targetState = this._snapshot(targetActor);
        const session = {
            step,
            source,
            target,
            sourceActor,
            targetActor,
            sourceState,
            targetState,
            width: Math.max(0, Number(this._d.width()) || 0),
            stack: [...this._d.stack()],
            targetWasMinimized: !!target.minimized,
            reducedMotion: !this._d.animationsEnabled(),
            endSerial: 0,
            ending: false,
            sourceUnmanaged: 0,
            targetUnmanaged: 0,
        };
        this._session = session;

        session.sourceUnmanaged = source.connect('unmanaged', () => this._onUnmanaged(session, source));
        session.targetUnmanaged = target.connect('unmanaged', () => this._onUnmanaged(session, target));

        if (!session.reducedMotion && session.targetWasMinimized) {
            this._d.skipEffect(targetActor);
            target.unminimize();
        }

        if (!session.reducedMotion) {
            this._d.raise(sourceActor);
            this._d.raise(targetActor);
        }
        return true;
    }

    update(progress) {
        const s = this._session;
        if (!s || s.reducedMotion || s.ending)
            return false;

        const p = Math.max(0, Math.min(1, Number(progress) || 0));
        const direction = s.step === 1 ? -1 : 1;
        s.sourceActor.translation_x = s.sourceState.x + direction * s.width * p;
        s.sourceActor.translation_y = s.sourceState.y;
        s.targetActor.translation_x = s.targetState.x - direction * s.width * (1 - p);
        s.targetActor.translation_y = s.targetState.y;
        return true;
    }

    end(commit) {
        const s = this._session;
        if (!s)
            return false;

        const finish = () => this._finish(s, !!commit);
        if (s.reducedMotion || !this._d.animationsEnabled()) {
            finish();
            return true;
        }
        s.ending = true;
        const endSerial = ++s.endSerial;

        const p = commit ? 1 : 0;
        const direction = s.step === 1 ? -1 : 1;
        const fraction = Math.max(0, Math.min(1,
            Math.abs((s.sourceActor.translation_x - s.sourceState.x) / (s.width || 1))));
        const duration = 90 + 70 * fraction;
        const remaining = {count: 2};
        const done = () => {
            if (this._session !== s || s.endSerial !== endSerial)
                return;
            remaining.count--;
            if (remaining.count === 0)
                finish();
        };

        this._d.settle(s.sourceActor, {
            translation_x: s.sourceState.x + direction * s.width * p,
            opacity: s.sourceState.opacity,
            duration,
        }, done);
        this._d.settle(s.targetActor, {
            translation_x: s.targetState.x - direction * s.width * (1 - p),
            opacity: s.targetState.opacity,
            duration,
        }, done);
        return true;
    }

    cancel() {
        const s = this._session;
        if (!s)
            return false;
        this._session = null;
        this._restore(s, false);
        return true;
    }

    destroy() {
        this.cancel();
    }

    _snapshot(actor) {
        return {
            x: Number(actor.translation_x) || 0,
            y: Number(actor.translation_y) || 0,
            opacity: actor.opacity,
        };
    }

    _onUnmanaged(s, gone) {
        if (this._session !== s)
            return;
        this._session = null;
        // The unmanaged actor is already leaving the stage; restore only the
        // surviving actor and put back the original stack where possible.
        this._restore(s, false, gone);
    }

    _finish(s, commit) {
        if (this._session !== s)
            return;
        this._session = null;
        this._restore(s, commit);
        if (commit)
            this._d.activate(s.target);
    }

    _restore(s, commit, gone = null) {
        for (const [actor, state] of [[s.sourceActor, s.sourceState], [s.targetActor, s.targetState]]) {
            if (gone && ((gone === s.source && actor === s.sourceActor) ||
                    (gone === s.target && actor === s.targetActor)))
                continue;
            actor.remove_transition?.('translation-x');
            actor.remove_transition?.('opacity');
            actor.translation_x = state.x;
            actor.translation_y = state.y;
            actor.opacity = state.opacity;
        }

        if (s.targetWasMinimized && !commit && gone !== s.target && !s.target.minimized) {
            this._d.skipEffect(s.targetActor);
            s.target.minimize();
        }
        this._d.restoreStack(s.stack);
        this._disconnect(s);
    }

    _disconnect(s) {
        if (s.sourceUnmanaged) {
            s.source.disconnect(s.sourceUnmanaged);
            s.sourceUnmanaged = 0;
        }
        if (s.targetUnmanaged) {
            s.target.disconnect(s.targetUnmanaged);
            s.targetUnmanaged = 0;
        }
    }
}

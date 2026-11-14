// Copyright (C) Hamza Gouda
// SPDX-License-Identifier: GPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Hamza Gouda
//
// Asusius Quick Settings: a "Performance" menu toggle with a Quiet /
// Balanced / Performance submenu, an OSD naming the new profile whenever
// it changes outside the menu too (Fn+F5 cycles it in asusd), and optional
// touchpad gestures bound in the settings. The look is the shell theme's own;
// nothing here sets colours.

import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

import {AsusdProfileSource, GpuModeSource, PpdProfileSource} from './dbus.js';
import {GestureWatcher, shellActions} from './gestures.js';
import {createGestureInput} from './gesture_input.js';
import {
    ICONS, LABELS, PROFILES, ProfileAnnouncer, ProfileModel, chargeLabel, gpuModeName,
    isToggleActive, pickToggleTarget, subtitleFor,
} from './profiles.js';

const APP_DESKTOP_ID = 'io.github.hamzagouda.Asusius.desktop';
const APP_SYMBOLIC_ICON = 'io.github.hamzagouda.Asusius-symbolic';


/**
 * Hide one stock Quick Settings toggle while active and restore it cleanly on
 * disable or when the setting/precondition flips off. Null-guarded against Shell
 * versions or panels that omit or replace the target item.
 */
class QuickTileHider {
    constructor({settings, key, findToggle, shouldHide = () => true, restoreVisibility}) {
        this._settings = settings;
        this._key = key;
        this._findToggle = findToggle;
        this._shouldHide = shouldHide;
        this._restoreVisibility = restoreVisibility;
        this._quickSettings = Main.panel?.statusArea?.quickSettings ?? null;
        this._toggle = null;
        this._hideIdleId = 0;
        this._visibilityId = 0;
        this._destroyId = 0;
        this._settingsId = 0;
        this._indicatorAddedId = 0;
        this._destroyed = false;
    }

    enable() {
        this._settingsId = this._settings.connect(`changed::${this._key}`, () => this.sync());
        this._watchIndicators();
        this.sync();
    }

    _watchIndicators() {
        if (this._destroyed || this._toggle || this._indicatorAddedId)
            return;
        const indicators = this._quickSettings?._indicators;
        if (indicators)
            this._indicatorAddedId = indicators.connect('child-added', () => this.sync());
    }

    _unwatchIndicators() {
        if (this._indicatorAddedId) {
            this._quickSettings?._indicators?.disconnect(this._indicatorAddedId);
            this._indicatorAddedId = 0;
        }
    }

    sync() {
        if (this._destroyed)
            return;
        if (!this._settings.get_boolean(this._key) || !this._shouldHide()) {
            this._restore();
            return;
        }

        if (this._toggle) {
            if (this._toggle.visible)
                this._toggle.hide();
            return;
        }

        const toggle = this._quickSettings ? this._findToggle(this._quickSettings) : null;
        if (!toggle) {
            this._watchIndicators();
            return;
        }

        this._unwatchIndicators();
        this._toggle = toggle;
        this._visibilityId = toggle.connect('notify::visible', () => {
            if (!toggle.visible || this._hideIdleId || this._toggle !== toggle)
                return;

            // Let GNOME finish syncing before hiding the tile again.
            this._hideIdleId = GLib.idle_add(GLib.PRIORITY_HIGH_IDLE, () => {
                this._hideIdleId = 0;
                if (!this._destroyed && this._toggle === toggle)
                    toggle.hide();
                return GLib.SOURCE_REMOVE;
            });
        });
        this._destroyId = toggle.connect('destroy', () => this._onToggleDestroyed());
        toggle.hide();
    }

    _onToggleDestroyed() {
        if (this._hideIdleId) {
            GLib.source_remove(this._hideIdleId);
            this._hideIdleId = 0;
        }
        this._visibilityId = 0;
        this._destroyId = 0;
        this._toggle = null;
        if (!this._destroyed)
            this._watchIndicators();
    }

    _restore() {
        if (!this._toggle)
            return;

        if (this._hideIdleId) {
            GLib.source_remove(this._hideIdleId);
            this._hideIdleId = 0;
        }
        const toggle = this._toggle;
        this._toggle = null;
        if (this._visibilityId)
            toggle.disconnect(this._visibilityId);
        if (this._destroyId)
            toggle.disconnect(this._destroyId);
        this._visibilityId = 0;
        this._destroyId = 0;
        this._restoreVisibility(toggle);
        if (!this._destroyed)
            this._watchIndicators();
    }

    destroy() {
        this._destroyed = true;
        this._unwatchIndicators();
        if (this._settingsId) {
            this._settings.disconnect(this._settingsId);
            this._settingsId = 0;
        }
        this._restore();
    }
}

const ProfileToggle = GObject.registerClass(
class AsusiusProfileToggle extends QuickMenuToggle {
    _init(model, asusd, gpu, announcer) {
        super._init({
            title: 'Performance',
            iconName: ICONS.balanced,
            toggleMode: false,
        });
        this._model = model;
        this._asusd = asusd;
        this._gpu = gpu;
        this._announcer = announcer;
        this._items = new Map();

        this.menu.setHeader(
            Gio.ThemedIcon.new_from_names([APP_SYMBOLIC_ICON, ICONS.balanced]),
            'Performance', 'ASUS platform profile');

        this._profileSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._profileSection);
        for (const profile of PROFILES) {
            const item = new PopupMenu.PopupImageMenuItem(LABELS[profile], ICONS[profile]);
            item.connect('activate', () => this._choose(profile));
            this._items.set(profile, item);
            this._profileSection.addMenuItem(item);
        }

        this._infoSeparator = new PopupMenu.PopupSeparatorMenuItem();
        this.menu.addMenuItem(this._infoSeparator);
        this._gpuItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._chargeItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this.menu.addMenuItem(this._gpuItem);
        this.menu.addMenuItem(this._chargeItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const open = new PopupMenu.PopupImageMenuItem('Open Asusius',
            Gio.ThemedIcon.new_from_names([APP_SYMBOLIC_ICON, 'preferences-system-symbolic']));
        open.connect('activate', () => this._openApp());
        this.menu.addMenuItem(open);

        this.connect('clicked', () => {
            const want = pickToggleTarget(this._model.profile, this._model.choices);
            if (want)
                this._choose(want);
            else
                this.sync();
        });

        this._ids = [
            [this._model, this._model.connect(() => this.sync())],
            [this._asusd, this._asusd.connect(() => this.sync())],
            [this._gpu, this._gpu.connect(() => this.sync())],
        ];
        this.sync();
    }

    _choose(profile) {
        if (!this._model.choices.includes(profile)) {
            this.sync();
            return;
        }
        this._announcer?.suppress(profile);
        this._model.set(profile, err => {
            if (err) {
                this._announcer?.clearSuppression();
                this.sync();
                Main.notifyError('Asusius', `Could not switch profile: ${err.message}`);
            }
        });
    }

    _openApp() {
        this.menu.close();
        try {
            const info = Gio.DesktopAppInfo.new(APP_DESKTOP_ID);
            if (info)
                info.launch([], global.create_app_launch_context(0, -1));
            else
                Main.notifyError('Asusius', 'The Asusius app is not installed.');
        } catch (e) {
            Main.notifyError('Asusius', `Could not start the app: ${e.message}`);
        }
    }

    sync() {
        const profile = this._model.profile;
        const choices = this._model.choices;
        const known = !!profile;
        const hasPerf = choices.includes('performance');
        this.title = hasPerf ? 'Performance' : 'Power Mode';
        this.visible = this._model.backend !== null;
        this.checked = isToggleActive(profile, choices);
        this.subtitle = subtitleFor(profile, this._model.backend);
        this.iconName = ICONS[profile] ?? ICONS.balanced;
        this.menu.setHeader(
            Gio.ThemedIcon.new_from_names([APP_SYMBOLIC_ICON, this.iconName]),
            this.title,
            this._model.backend === 'ppd' ? 'Power Profiles Daemon' : 'ASUS platform profile');
        for (const [name, item] of this._items) {
            item.visible = choices.includes(name);
            item.setOrnament(known && name === profile
                ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        }
        const gpuName = gpuModeName(this._gpu.mode);
        this._gpuItem.visible = !!gpuName;
        this._gpuItem.label.text = gpuName ? `GPU mode: ${gpuName}` : '';
        const charge = chargeLabel(this._asusd.chargeLimit);
        this._chargeItem.visible = !!charge;
        this._chargeItem.label.text = charge ?? '';
        this._infoSeparator.visible = !!(gpuName || charge);
    }

    destroy() {
        for (const [obj, id] of this._ids ?? [])
            obj.disconnect(id);
        this._ids = [];
        this._announcer = null;
        this.menu?.close(false);
        this.menu?.destroy();
        super.destroy();
    }
});

const ProfileIndicator = GObject.registerClass(
class AsusiusProfileIndicator extends SystemIndicator {
    _init(model, asusd, gpu, announcer) {
        super._init();
        this._toggle = new ProfileToggle(model, asusd, gpu, announcer);
        this.quickSettingsItems.push(this._toggle);
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        this.quickSettingsItems = [];
        super.destroy();
    }
});

export default class AsusiusQuickExtension extends Extension {
    enable() {
        try {
            // The schema id is passed explicitly: a session that cached the extension's
            // pre-gestures metadata (without "settings-schema") would otherwise hand
            // getSettings() an undefined id here.
            const settings = this.getSettings('org.gnome.shell.extensions.asusius-quick');

            this._asusd = new AsusdProfileSource();
            this._ppd = new PpdProfileSource();
            this._gpu = new GpuModeSource();
            this._model = new ProfileModel([
                {name: 'asusd', source: this._asusd},
                {name: 'ppd', source: this._ppd},
            ]);
            // GNOME shows nothing when Fn+F5 (asusd) or another app switches the
            // profile: show the new one in an OSD, like the volume keys do.
            this._announcer = new ProfileAnnouncer(this._model, {
                schedule: (ms, fn) => {
                    const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
                        fn();
                        return GLib.SOURCE_REMOVE;
                    });
                    GLib.Source.set_name_by_id(id, '[asusius-quick] profile osd');
                    return id;
                },
                cancel: id => GLib.source_remove(id),
                show: profile => Main.osdWindowManager.show(-1,
                    Gio.ThemedIcon.new_from_names([ICONS[profile] ?? ICONS.balanced, APP_SYMBOLIC_ICON]),
                    LABELS[profile] ?? profile, null),
            });
            this._indicator = new ProfileIndicator(this._model, this._asusd, this._gpu, this._announcer);
            Main.panel?.statusArea?.quickSettings?.addExternalIndicator(this._indicator);

            // Touchpad gestures, off until 'gesture-enabled' says otherwise. The
            // settings are read per gesture, so changed bindings need no restart.
            this._gestureInput = createGestureInput(Main);
            this._gestures = new GestureWatcher({
                settings,
                stage: global.stage,
                actions: shellActions(Main, Meta, settings, this._gestureInput),
                input: this._gestureInput,
                clutter: Clutter,
            });
            const resetGestureMotion = () => {
                this._gestures.cancel();
                this._gestureInput.prepareOverview();
            };
            this._overviewGestureId = Main.overview.connect('showing', resetGestureMotion);
            this._workspaceGestureId = global.workspace_manager.connect(
                'active-workspace-changed', resetGestureMotion);
            this._gestures.enable();

            // One Power Mode selector is enough: hide GNOME's stock tile only while
            // Asusius has an active backend (asusd or ppd) to replace it.
            this._hider = new QuickTileHider({
                settings,
                key: 'hide-power-mode',
                findToggle: qs => qs._powerProfiles?.quickSettingsItems?.[0] ?? null,
                shouldHide: () => this._model?.backend !== null,
                restoreVisibility: toggle => {
                    if (typeof toggle._sync === 'function')
                        toggle._sync();
                    else
                        toggle.visible = true;
                },
            });
            this._hiderModelId = this._model.connect(() => this._hider?.sync());
            this._hider.enable();

            this._nightLightHider = new QuickTileHider({
                settings,
                key: 'hide-night-light',
                findToggle: qs => qs._nightLight?.quickSettingsItems?.[0] ?? null,
                restoreVisibility: toggle => {
                    const supported = global.backend?.get_monitor_manager?.()?.night_light_supported;
                    if (typeof supported === 'boolean')
                        toggle.visible = supported;
                    else
                        toggle.show();
                },
            });
            this._nightLightHider.enable();
        } catch (e) {
            this.disable();
            throw e;
        }
    }

    disable() {
        if (this._hiderModelId && this._model)
            this._model.disconnect(this._hiderModelId);
        this._hiderModelId = 0;
        this._nightLightHider?.destroy();
        this._nightLightHider = null;
        this._hider?.destroy();
        this._hider = null;
        if (this._overviewGestureId)
            Main.overview.disconnect(this._overviewGestureId);
        this._overviewGestureId = 0;
        if (this._workspaceGestureId)
            global.workspace_manager.disconnect(this._workspaceGestureId);
        this._workspaceGestureId = 0;
        this._gestures?.disable();
        this._gestureInput?.destroy();
        this._gestureInput = null;
        this._gestures = null;
        this._announcer?.destroy();
        this._announcer = null;
        // Destroying the indicator removes its quick settings items from the grid.
        this._indicator?.destroy();
        this._indicator = null;
        this._model?.destroy();
        this._model = null;
        this._gpu?.destroy();
        this._gpu = null;
        this._asusd = null;
        this._ppd = null;
    }
}

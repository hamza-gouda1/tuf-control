// SPDX-License-Identifier: GPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Hamza Gouda
//
// TUF Control Quick Settings: a "Performance" menu toggle with a Quiet /
// Balanced / Performance submenu, and an OSD naming the new profile whenever
// it changes outside the menu too (Fn+F5 cycles it in asusd). The look is the
// shell theme's own; nothing here sets colours.

import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

import {AsusdProfileSource, GpuModeSource, PpdProfileSource} from './dbus.js';
import {
    ICONS, LABELS, PROFILES, ProfileAnnouncer, ProfileModel, chargeLabel, gpuModeName, subtitleFor,
} from './profiles.js';

const APP_DESKTOP_ID = 'io.github.hamzagouda.TufControl.desktop';

const ProfileToggle = GObject.registerClass(
class TufProfileToggle extends QuickMenuToggle {
    _init(model, asusd, gpu) {
        super._init({
            title: 'Performance',
            iconName: ICONS.balanced,
            toggleMode: true,
        });
        this._model = model;
        this._asusd = asusd;
        this._gpu = gpu;
        this._items = new Map();

        this.menu.setHeader(ICONS.balanced, 'Performance', 'ASUS platform profile');

        this._profileSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._profileSection);
        for (const profile of PROFILES) {
            const item = new PopupMenu.PopupMenuItem(LABELS[profile]);
            item.connect('activate', () => this._choose(profile));
            this._items.set(profile, item);
            this._profileSection.addMenuItem(item);
        }

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._gpuItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._chargeItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this.menu.addMenuItem(this._gpuItem);
        this.menu.addMenuItem(this._chargeItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const open = new PopupMenu.PopupMenuItem('Open TUF Control');
        open.connect('activate', () => this._openApp());
        this.menu.addMenuItem(open);

        // A click on the button itself flips between Performance and Balanced.
        this.connect('clicked', () => {
            const want = this.checked ? 'performance' : 'balanced';
            this._choose(want);
        });

        this._ids = [
            [this._model, this._model.connect(() => this.sync())],
            [this._asusd, this._asusd.connect(() => this.sync())],
            [this._gpu, this._gpu.connect(() => this.sync())],
        ];
        this.sync();
    }

    _choose(profile) {
        this._model.set(profile, err => {
            if (err)
                Main.notifyError('TUF Control', `Could not switch profile: ${err.message}`);
        });
    }

    _openApp() {
        this.menu.close();
        try {
            const info = Gio.DesktopAppInfo.new(APP_DESKTOP_ID);
            if (info)
                info.launch([], global.create_app_launch_context(0, -1));
            else
                Main.notifyError('TUF Control', 'The TUF Control app is not installed.');
        } catch (e) {
            Main.notifyError('TUF Control', `Could not start the app: ${e.message}`);
        }
    }

    sync() {
        const profile = this._model.profile;
        const known = !!profile;
        this.visible = this._model.backend !== null;
        this.checked = profile === 'performance';
        this.subtitle = subtitleFor(profile, this._model.backend);
        this.iconName = ICONS[profile] ?? ICONS.balanced;
        this.menu.setHeader(this.iconName, 'Performance',
            this._model.backend === 'ppd' ? 'power-profiles-daemon' : 'ASUS platform profile');
        const choices = this._model.choices;
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
    }

    destroy() {
        for (const [obj, id] of this._ids ?? [])
            obj.disconnect(id);
        this._ids = [];
        super.destroy();
    }
});

const ProfileIndicator = GObject.registerClass(
class TufProfileIndicator extends SystemIndicator {
    _init(model, asusd, gpu) {
        super._init();
        this._toggle = new ProfileToggle(model, asusd, gpu);
        this.quickSettingsItems.push(this._toggle);
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        this.quickSettingsItems = [];
        super.destroy();
    }
});

export default class TufControlQuickExtension extends Extension {
    enable() {
        this._asusd = new AsusdProfileSource();
        this._ppd = new PpdProfileSource();
        this._gpu = new GpuModeSource();
        this._model = new ProfileModel([
            {name: 'asusd', source: this._asusd},
            {name: 'ppd', source: this._ppd},
        ]);
        this._indicator = new ProfileIndicator(this._model, this._asusd, this._gpu);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        // GNOME shows nothing when Fn+F5 (asusd) or another app switches the
        // profile: show the new one in an OSD, like the volume keys do.
        this._announcer = new ProfileAnnouncer(this._model, {
            schedule: (ms, fn) => {
                const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
                    fn();
                    return GLib.SOURCE_REMOVE;
                });
                GLib.Source.set_name_by_id(id, '[tuf-control-quick] profile osd');
                return id;
            },
            cancel: id => GLib.source_remove(id),
            show: profile => Main.osdWindowManager.show(-1,
                Gio.ThemedIcon.new(ICONS[profile] ?? ICONS.balanced), LABELS[profile] ?? profile, null),
        });
    }

    disable() {
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

// SPDX-License-Identifier: GPL-2.0-or-later
import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences, gettext as _} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {
    SLIDER_MIN, SLIDER_MAX, sliderToFactor, factorToSlider, formatFactor,
    helperConfig, isNeutral, summarize
} from './model.js';
import {RULE_PATH, installArgv, removeArgv} from './permission.js';

const PROBE_INTERVAL_S = 3;
const SLIDER_MARKS = [0.25, 0.5, 1, 2, 4];
const TEST_LINES = 120;

// A row with a logarithmic speed slider bound to a "factor" key.
function speedRow(settings, key, title, subtitle) {
    const row = new Adw.ActionRow({title, subtitle});
    const value = new Gtk.Label({width_chars: 7, xalign: 1, css_classes: ['numeric']});
    const scale = new Gtk.Scale({
        adjustment: new Gtk.Adjustment({
            lower: SLIDER_MIN, upper: SLIDER_MAX, step_increment: 0.1, page_increment: 0.5,
        }),
        draw_value: false,
        hexpand: true,
        valign: Gtk.Align.CENTER,
        width_request: 260,
    });
    for (const mark of SLIDER_MARKS)
        scale.add_mark(Math.log2(mark), Gtk.PositionType.BOTTOM, formatFactor(mark));

    // Moving the slider writes the setting; a setting changed elsewhere moves
    // the slider without being rounded to a slider step.
    let syncing = false;
    const show = () => {
        const factor = settings.get_double(key);
        value.label = formatFactor(factor);
        if (sliderToFactor(scale.get_value()) !== factor) {
            syncing = true;
            scale.set_value(factorToSlider(factor));
            syncing = false;
        }
    };
    scale.connect('value-changed', () => {
        if (syncing)
            return;
        const factor = sliderToFactor(scale.get_value());
        if (factor !== settings.get_double(key))
            settings.set_double(key, factor);
    });
    settings.connect(`changed::${key}`, show);
    show();

    row.add_suffix(scale);
    row.add_suffix(value);
    return row;
}

function accelerationRow(settings) {
    const row = new Adw.ActionRow({
        title: _('Acceleration'),
        subtitle: _('Scroll further when the wheel spins fast'),
    });
    const value = new Gtk.Label({width_chars: 7, xalign: 1, css_classes: ['numeric']});
    const scale = new Gtk.Scale({
        adjustment: new Gtk.Adjustment({lower: 0, upper: 1, step_increment: 0.05, page_increment: 0.25}),
        draw_value: false,
        hexpand: true,
        valign: Gtk.Align.CENTER,
        width_request: 260,
    });
    const show = () => {
        const amount = settings.get_double('acceleration');
        value.label = amount === 0 ? _('Off') : `${Math.round(amount * 100)} %`;
    };
    settings.bind('acceleration', scale.adjustment, 'value', Gio.SettingsBindFlags.DEFAULT);
    settings.connect('changed::acceleration', show);
    show();

    row.add_suffix(scale);
    row.add_suffix(value);
    return row;
}

// A place to feel the speed without leaving the window.
function testArea() {
    const lines = [];
    for (let i = 1; i <= TEST_LINES; i++)
        lines.push(_('Line %d').replace('%d', i));
    const label = new Gtk.Label({
        label: lines.join('\n'),
        xalign: 0,
        margin_top: 12, margin_bottom: 12, margin_start: 12, margin_end: 12,
    });
    return new Gtk.ScrolledWindow({
        child: label,
        min_content_height: 180,
        hscrollbar_policy: Gtk.PolicyType.NEVER,
        css_classes: ['card'],
    });
}

export default class ScrollSpeedPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage();
        window.add(page);

        // -- speed
        const speed = new Adw.PreferencesGroup({title: _('Mouse wheel')});
        page.add(speed);

        const enabled = new Adw.SwitchRow({
            title: _('Change the wheel speed'),
            subtitle: _('Touchpads and pointing sticks are not affected'),
        });
        settings.bind('enabled', enabled, 'active', Gio.SettingsBindFlags.DEFAULT);
        speed.add(enabled);

        const vertical = speedRow(settings, 'vertical-factor', _('Scroll speed'),
            _('1× is the normal speed'));
        speed.add(vertical);

        const linked = new Adw.SwitchRow({title: _('Same speed for horizontal scrolling')});
        settings.bind('horizontal-linked', linked, 'active', Gio.SettingsBindFlags.DEFAULT);
        speed.add(linked);

        const horizontal = speedRow(settings, 'horizontal-factor', _('Horizontal scroll speed'), '');
        settings.bind('horizontal-linked', horizontal, 'visible', Gio.SettingsBindFlags.INVERT_BOOLEAN);
        speed.add(horizontal);

        const acceleration = accelerationRow(settings);
        speed.add(acceleration);

        for (const row of [vertical, linked, horizontal, acceleration])
            settings.bind('enabled', row, 'sensitive', Gio.SettingsBindFlags.GET);

        // -- status
        const status = new Adw.PreferencesGroup({title: _('Mice')});
        page.add(status);
        let statusRows = [];

        const authorize = (argv, done) => {
            let process;
            try {
                process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE);
            } catch (error) {
                window.add_toast(new Adw.Toast({title: error.message}));
                return;
            }
            process.wait_check_async(null, (_process, result) => {
                try {
                    process.wait_check_finish(result);
                    window.add_toast(new Adw.Toast({title: done}));
                } catch {
                    window.add_toast(new Adw.Toast({title: _('Nothing was changed')}));
                }
                refresh();
            });
        };

        const buttonRow = (title, subtitle, label, onClick, suggested) => {
            const row = new Adw.ActionRow({title, subtitle});
            const button = new Gtk.Button({
                label,
                valign: Gtk.Align.CENTER,
                css_classes: suggested ? ['suggested-action'] : [],
            });
            button.connect('clicked', onClick);
            row.add_suffix(button);
            row.activatable_widget = button;
            return row;
        };

        const deviceRow = (device, neutral) => {
            let state;
            if (!device.access)
                state = _('No access yet');
            else if (device.active)
                state = _('Active');
            else if (neutral)
                state = _('Normal speed');
            else
                state = _('Starting…');
            const row = new Adw.ActionRow({title: device.name, subtitle: device.path});
            row.add_suffix(new Gtk.Label({label: state, css_classes: ['dim-label']}));
            return row;
        };

        const render = (probe, ruleInstalled) => {
            for (const row of statusRows)
                status.remove(row);
            statusRows = [];

            const summary = summarize(probe, ruleInstalled);
            const neutral = isNeutral(helperConfig({
                enabled: settings.get_boolean('enabled'),
                vertical: settings.get_double('vertical-factor'),
                horizontalLinked: settings.get_boolean('horizontal-linked'),
                horizontal: settings.get_double('horizontal-factor'),
                acceleration: settings.get_double('acceleration'),
            }));
            const installRule = () =>
                authorize(installArgv(), _('Authorization granted'));

            switch (summary.kind) {
            case 'helper-failed':
                statusRows.push(new Adw.ActionRow({
                    title: _('The helper could not be started'),
                    subtitle: _('Scroll Speed needs Python 3, which was not found.'),
                }));
                break;
            case 'needs-authorization':
                statusRows.push(buttonRow(
                    _('Authorization needed'),
                    _('Let your session read the mouse and replay it with the new wheel speed. Asked once, with your password.'),
                    _('Authorize…'), installRule, true));
                break;
            case 'authorization-pending':
                statusRows.push(buttonRow(
                    _('Authorization installed, but not effective yet'),
                    _('Unplug the mouse and plug it back in, or authorize again.'),
                    _('Authorize again…'), installRule, false));
                break;
            case 'uinput-missing':
                statusRows.push(new Adw.ActionRow({
                    title: _('The virtual input device is not available'),
                    subtitle: _('Restart the computer so that the uinput kernel module is loaded.'),
                }));
                break;
            case 'no-mouse':
                statusRows.push(new Adw.ActionRow({
                    title: _('No wheel mouse detected'),
                    subtitle: _('Plug in a mouse: it will be picked up within a few seconds.'),
                }));
                break;
            }
            for (const device of summary.devices)
                statusRows.push(deviceRow(device, neutral));

            if (ruleInstalled) {
                statusRows.push(buttonRow(
                    _('Remove the authorization'),
                    _('Deletes the udev rule. Access already granted lasts until the next restart.'),
                    _('Remove…'),
                    () => authorize(removeArgv(), _('Authorization removed')), false));
            }
            for (const row of statusRows)
                status.add(row);
        };

        // The helper's --probe mode reports the mice and permissions as JSON.
        const helper = this.dir.get_child('helper').get_child('scroll_speed_helper.py');
        const rule = Gio.File.new_for_path(RULE_PATH);
        let closed = false;
        let probing = false;

        const ruleInstalled = () => new Promise(resolve => {
            rule.query_info_async('standard::type', Gio.FileQueryInfoFlags.NONE,
                GLib.PRIORITY_DEFAULT, null, (_file, result) => {
                    try {
                        rule.query_info_finish(result);
                        resolve(true);
                    } catch {
                        resolve(false);
                    }
                });
        });

        const runProbe = () => new Promise(resolve => {
            let process;
            try {
                process = Gio.Subprocess.new(['python3', helper.get_path(), '--probe'],
                    Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            } catch {
                resolve(null);
                return;
            }
            process.communicate_utf8_async(null, null, (_process, result) => {
                try {
                    const [, stdout] = process.communicate_utf8_finish(result);
                    resolve(process.get_successful() ? JSON.parse(stdout) : null);
                } catch {
                    resolve(null);
                }
            });
        });

        async function refresh() {
            if (probing || closed)
                return;
            probing = true;
            const [probe, installed] = await Promise.all([runProbe(), ruleInstalled()]);
            probing = false;
            if (!closed)
                render(probe, installed);
        }

        refresh();
        const timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, PROBE_INTERVAL_S, () => {
            refresh();
            return GLib.SOURCE_CONTINUE;
        });
        window.connect('close-request', () => {
            closed = true;
            GLib.source_remove(timer);
            return false;
        });

        // -- try it
        const tryIt = new Adw.PreferencesGroup({
            title: _('Try it'),
            description: _('Scroll here to feel the speed; changes apply immediately.'),
        });
        tryIt.add(testArea());
        page.add(tryIt);
    }
}

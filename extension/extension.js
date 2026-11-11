// SPDX-License-Identifier: GPL-2.0-or-later
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {settingsConfig} from './model.js';

const SIGTERM = 15;
// A helper that dies is restarted after a growing delay, a few times at most.
const RESTART_DELAYS_S = [2, 10, 60];
// A helper that lived this long is considered healthy again.
const HEALTHY_AFTER_S = 120;

export default class ScrollSpeedExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._settings.connectObject('changed', () => this._sendConfig(), this);
        this._failures = 0;
        this._startHelper();
    }

    disable() {
        this._settings.disconnectObject(this);
        this._settings = null;
        if (this._restartId) {
            GLib.source_remove(this._restartId);
            this._restartId = 0;
        }
        this._stopHelper();
    }

    // The helper rescales scrolling below the compositor; it takes its
    // configuration as JSON lines on stdin and exits when stdin closes, so it
    // never outlives the shell.
    _startHelper() {
        const script = this.dir.get_child('helper').get_child('scroll_speed_helper.py');
        const launcher = new Gio.SubprocessLauncher({
            flags: Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_SILENCE,
        });
        let helper;
        try {
            helper = launcher.spawnv(['python3', script.get_path(), '--stdin-control']);
        } catch (error) {
            console.error(`Scroll Speed: cannot start the helper: ${error.message}`);
            return;
        }

        const startedAt = GLib.get_monotonic_time();
        this._helper = helper;
        this._stdin = helper.get_stdin_pipe();
        this._writing = false;
        this._stale = false;
        this._cancellable = new Gio.Cancellable();
        helper.wait_async(this._cancellable, (_helper, result) => {
            try {
                helper.wait_finish(result);
            } catch {
                return; // cancelled by disable()
            }
            this._onHelperExit(startedAt);
        });
        this._sendConfig();
    }

    _stopHelper() {
        this._cancellable?.cancel();
        this._cancellable = null;
        this._helper?.send_signal(SIGTERM);
        this._helper = null;
        this._stdin = null;
    }

    _onHelperExit(startedAt) {
        this._helper = null;
        this._stdin = null;
        this._cancellable = null;

        const livedFor = (GLib.get_monotonic_time() - startedAt) / GLib.USEC_PER_SEC;
        if (livedFor > HEALTHY_AFTER_S)
            this._failures = 0;
        if (this._failures >= RESTART_DELAYS_S.length) {
            console.error('Scroll Speed: the helper keeps stopping, giving up');
            return;
        }
        const delay = RESTART_DELAYS_S[this._failures++];
        this._restartId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, delay, () => {
            this._restartId = 0;
            this._startHelper();
            return GLib.SOURCE_REMOVE;
        });
    }

    // One write at a time: a slider being dragged changes the settings faster
    // than the pipe write completes, so the latest state is sent afterwards.
    _sendConfig() {
        const stdin = this._stdin;
        if (!stdin)
            return;
        if (this._writing) {
            this._stale = true;
            return;
        }
        const line = new GLib.Bytes(new TextEncoder().encode(`${JSON.stringify(settingsConfig(this._settings))}\n`));
        this._writing = true;
        stdin.write_bytes_async(line, GLib.PRIORITY_DEFAULT, null,
            (_stream, result) => {
                try {
                    stdin.write_bytes_finish(result);
                } catch {
                    return; // the helper is gone; its exit is handled elsewhere
                }
                if (this._stdin !== stdin)
                    return;
                this._writing = false;
                if (this._stale) {
                    this._stale = false;
                    this._sendConfig();
                }
            });
    }
}

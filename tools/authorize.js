// SPDX-License-Identifier: GPL-2.0-or-later
// Installs (or with --remove, deletes) the udev rule from a terminal, with the
// same commands as the "Authorize…" button of the preferences. With --check it
// changes nothing and only tells, by its exit status, whether the installed
// rule is the current one.
//
//   gjs -m tools/authorize.js [--remove | --check]
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import System from 'system';

import {RULE_PATH, ruleState, installArgv, removeArgv} from '../extension/permission.js';

if (ARGV.includes('--check')) {
    let contents = null;
    try {
        contents = new TextDecoder().decode(GLib.file_get_contents(RULE_PATH)[1]);
    } catch {
        // no rule file
    }
    const state = ruleState(contents);
    print(state);
    System.exit(state === 'current' ? 0 : 1);
}

const argv = ARGV.includes('--remove') ? removeArgv() : installArgv();
try {
    Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE).wait_check(null);
} catch (error) {
    printerr(`Nothing was changed: ${error.message}`);
    System.exit(1);
}

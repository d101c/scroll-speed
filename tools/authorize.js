// SPDX-License-Identifier: GPL-2.0-or-later
// Installs (or with --remove, deletes) the udev rule from a terminal, with the
// same commands as the "Authorize…" button of the preferences.
//
//   gjs -m tools/authorize.js [--remove]
import Gio from 'gi://Gio';
import System from 'system';

import {installArgv, removeArgv} from '../extension/permission.js';

const argv = ARGV.includes('--remove') ? removeArgv() : installArgv();
try {
    Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE).wait_check(null);
} catch (error) {
    printerr(`Nothing was changed: ${error.message}`);
    System.exit(1);
}

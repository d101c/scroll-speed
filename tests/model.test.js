// SPDX-License-Identifier: GPL-2.0-or-later
// gjs -m tests/model.test.js
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import System from 'system';

import * as model from '../extension/model.js';
import * as permission from '../extension/permission.js';

let failures = 0;
function check(name, actual, expected) {
    const a = JSON.stringify(actual), e = JSON.stringify(expected);
    if (a === e) {
        print(`ok   ${name}`);
    } else {
        failures++;
        print(`FAIL ${name}\n     got      ${a}\n     expected ${e}`);
    }
}

// -- slider mapping
check('slider centre is the normal speed', model.sliderToFactor(0), 1);
check('slider snaps to normal near the centre', model.sliderToFactor(0.04), 1);
check('half speed', model.sliderToFactor(-1), 0.5);
check('double speed', model.sliderToFactor(1), 2);
check('slider ends', [model.sliderToFactor(model.SLIDER_MIN), model.sliderToFactor(model.SLIDER_MAX)],
    [model.FACTOR_MIN, model.FACTOR_MAX]);
check('beyond the ends is clamped', [model.sliderToFactor(-9), model.sliderToFactor(9)],
    [model.FACTOR_MIN, model.FACTOR_MAX]);
check('just outside the snap zone', model.sliderToFactor(Math.log2(1.1)), 1.1);
check('factors land on 0.05 steps', model.sliderToFactor(Math.log2(0.33)), 0.35);
for (const factor of [0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3.35, 5])
    check(`round trip ${factor}`, model.sliderToFactor(model.factorToSlider(factor)), factor);
check('out-of-range factor maps to the slider end', model.factorToSlider(20), model.SLIDER_MAX);

// -- labels
check('label en', [0.5, 1, 1.25, 4].map(f => model.formatFactor(f, 'en-US')),
    ['0.5×', '1×', '1.25×', '4×']);
check('label fr uses a comma', model.formatFactor(0.75, 'fr-FR'), '0,75×');

// -- helper configuration
const base = {enabled: true, vertical: 0.5, horizontalLinked: true, horizontal: 3, acceleration: 0};
check('linked horizontal follows vertical', model.helperConfig(base),
    {enabled: true, vertical: 0.5, horizontal: 0.5, acceleration: 0});
check('unlinked horizontal keeps its own factor',
    model.helperConfig({...base, horizontalLinked: false}).horizontal, 3);
check('neutral when disabled', model.isNeutral(model.helperConfig({...base, enabled: false})), true);
check('neutral at 1x', model.isNeutral(model.helperConfig({...base, vertical: 1})), true);
check('not neutral at 0.5x', model.isNeutral(model.helperConfig(base)), false);
check('acceleration alone is not neutral',
    model.isNeutral(model.helperConfig({...base, vertical: 1, acceleration: 0.3})), false);

// -- status summary
const mouse = {name: 'M', path: '/dev/input/event9', access: true, active: true};
check('no helper', model.summarize(null, false).kind, 'helper-failed');
check('uinput denied', model.summarize({uinput: 'denied', devices: [mouse]}, false).kind,
    'needs-authorization');
check('uinput missing', model.summarize({uinput: 'missing', devices: []}, false).kind,
    'needs-authorization');
check('mouse denied', model.summarize({uinput: 'ok', devices: [{...mouse, access: false}]}, false).kind,
    'needs-authorization');
check('rule installed but still denied',
    model.summarize({uinput: 'ok', devices: [{...mouse, access: false}]}, true).kind,
    'authorization-pending');
check('no mouse', model.summarize({uinput: 'ok', devices: []}, true).kind, 'no-mouse');
check('ready', model.summarize({uinput: 'ok', devices: [mouse]}, true),
    {kind: 'ready', devices: [mouse]});

// -- privileged commands
check('install runs through pkexec sh', permission.installArgv().slice(0, 3), ['pkexec', '/bin/sh', '-c']);
check('remove deletes the rule', permission.removeScript().startsWith(`rm -f ${permission.RULE_PATH}`), true);

// The install script, pointed at a scratch file, must write exactly the rule
// lines; udevadm then checks that the result is a valid rules file.
const scratch = GLib.build_filenamev([GLib.get_tmp_dir(), `scroll-speed-rule-${GLib.get_monotonic_time()}.rules`]);
const writeOnly = permission.installScript().split(' && udevadm')[0].replace(permission.RULE_PATH, scratch);
const [, , , writeStatus] = GLib.spawn_sync(null, ['/bin/sh', '-c', writeOnly], null, GLib.SpawnFlags.SEARCH_PATH, null);
const [, written] = GLib.file_get_contents(scratch);
check('install script exit status', writeStatus, 0);
check('install script writes the rule', new TextDecoder().decode(written),
    `${permission.RULE_LINES.join('\n')}\n`);
if (GLib.find_program_in_path('udevadm')) {
    const [, out, err, verifyStatus] = GLib.spawn_sync(null, ['udevadm', 'verify', scratch], null,
        GLib.SpawnFlags.SEARCH_PATH, null);
    check(`udevadm verify accepts the rule ${new TextDecoder().decode(out)}${new TextDecoder().decode(err)}`.trim().split('\n')[0],
        verifyStatus, 0);
}
Gio.File.new_for_path(scratch).delete(null);

print(failures ? `\n${failures} FAILED` : '\nall passed');
System.exit(failures ? 1 : 0);

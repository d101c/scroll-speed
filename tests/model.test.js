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
    {enabled: true, vertical: 0.5, horizontal: 0.5, acceleration: 0, touchpad: 1, pointingstick: 1});
check('unlinked horizontal keeps its own factor',
    model.helperConfig({...base, horizontalLinked: false}).horizontal, 3);
check('neutral when disabled', model.isNeutral(model.helperConfig({...base, enabled: false})), true);
check('neutral at 1x', model.isNeutral(model.helperConfig({...base, vertical: 1})), true);
check('not neutral at 0.5x', model.isNeutral(model.helperConfig(base)), false);
check('acceleration alone is not neutral',
    model.isNeutral(model.helperConfig({...base, vertical: 1, acceleration: 0.3})), false);
const padOnly = model.helperConfig({...base, vertical: 1, touchpad: 0.5});
check('touchpad alone is not neutral', model.isNeutral(padOnly), false);
check('neutral per kind', model.KINDS.map(kind => model.kindIsNeutral(padOnly, kind)), [true, false, true]);
check('nothing is touched when disabled',
    model.KINDS.map(kind => model.kindIsNeutral({...padOnly, enabled: false}, kind)), [true, true, true]);
const fakeSettings = {
    get_boolean: key => ({enabled: true, 'horizontal-linked': false})[key],
    get_double: key => ({'vertical-factor': 2, 'horizontal-factor': 3, acceleration: 0.5,
        'touchpad-factor': 0.4, 'pointingstick-factor': 0.6})[key],
};
check('configuration read from the settings', model.settingsConfig(fakeSettings),
    {enabled: true, vertical: 2, horizontal: 3, acceleration: 0.5, touchpad: 0.4, pointingstick: 0.6});

// -- status summary
const mouse = {name: 'M', path: '/dev/input/event9', kind: 'mouse', access: true, active: true};
const pad = {name: 'T', path: '/dev/input/event5', kind: 'touchpad', access: false, active: false};
check('no helper', model.summarize(null, 'missing').kind, 'helper-failed');
check('uinput denied', model.summarize({uinput: 'denied', devices: [mouse]}, 'missing').kind,
    'needs-authorization');
check('uinput missing', model.summarize({uinput: 'missing', devices: []}, 'missing').kind,
    'needs-authorization');
check('uinput missing with the rule installed', model.summarize({uinput: 'missing', devices: []}, 'current').kind,
    'uinput-missing');
check('mouse denied', model.summarize({uinput: 'ok', devices: [{...mouse, access: false}]}, 'missing').kind,
    'needs-authorization');
check('rule installed but still denied',
    model.summarize({uinput: 'ok', devices: [{...mouse, access: false}]}, 'current').kind,
    'authorization-pending');
check('rule from a version that only knew mice',
    model.summarize({uinput: 'ok', devices: [mouse, pad]}, 'outdated').kind, 'authorization-outdated');
check('an old rule is enough when only mice are there',
    model.summarize({uinput: 'ok', devices: [mouse]}, 'outdated').kind, 'ready');
check('no device', model.summarize({uinput: 'ok', devices: []}, 'current').kind, 'no-device');
check('ready', model.summarize({uinput: 'ok', devices: [mouse]}, 'current'),
    {kind: 'ready', devices: [mouse]});

// -- privileged commands
check('install runs through pkexec sh', permission.installArgv().slice(0, 3), ['pkexec', '/bin/sh', '-c']);
check('remove deletes the rule', permission.removeScript().startsWith(`rm -f ${permission.RULE_PATH}`), true);
check('rule state', [null, permission.ruleText(), '# an older rule\n'].map(permission.ruleState),
    ['missing', 'current', 'outdated']);
check('keyboards are excluded from every access rule',
    permission.RULE_LINES.filter(line => line.includes('uaccess') && line.includes('SUBSYSTEM=="input"'))
        .every(line => line.includes('ENV{ID_INPUT_KEYBOARD}!="1"')), true);

// The install script, pointed at a scratch file, must write exactly the rule
// lines; udevadm then checks that the result is a valid rules file.
const scratch = GLib.build_filenamev([GLib.get_tmp_dir(), `scroll-speed-rule-${GLib.get_monotonic_time()}.rules`]);
const writeOnly = permission.installScript().split(' && udevadm')[0].replace(permission.RULE_PATH, scratch);
const [, , , writeStatus] = GLib.spawn_sync(null, ['/bin/sh', '-c', writeOnly], null, GLib.SpawnFlags.SEARCH_PATH, null);
const [, written] = GLib.file_get_contents(scratch);
check('install script exit status', writeStatus, 0);
check('install script writes the rule', new TextDecoder().decode(written), permission.ruleText());
if (GLib.find_program_in_path('udevadm')) {
    const [, out, err, verifyStatus] = GLib.spawn_sync(null, ['udevadm', 'verify', scratch], null,
        GLib.SpawnFlags.SEARCH_PATH, null);
    check(`udevadm verify accepts the rule ${new TextDecoder().decode(out)}${new TextDecoder().decode(err)}`.trim().split('\n')[0],
        verifyStatus, 0);
}
Gio.File.new_for_path(scratch).delete(null);

print(failures ? `\n${failures} FAILED` : '\nall passed');
System.exit(failures ? 1 : 0);

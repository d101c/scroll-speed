// SPDX-License-Identifier: GPL-2.0-or-later
// The one privileged step: a udev rule that lets the user of the active
// session read wheel mice and create the virtual mouse. The commands run as
// root are fixed strings built here, never a file a user process could edit.

export const RULE_PATH = '/etc/udev/rules.d/70-scroll-speed.rules';

// "uaccess" grants access to whoever is logged in on the seat, the way udev
// already does for joysticks and sound cards. Keyboards and pointing sticks
// are left out, so the rule never exposes keystrokes.
//
// The last two rules give a clone the hardware database entry of the mouse it
// mirrors (resolution, trackball flag): udev only looks those up for devices
// on a real bus, and without them libinput would move the pointer at a
// different speed through the clone.
const CLONE = 'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{phys}=="scroll-speed/clone"';
const HWDB = bus => `IMPORT{builtin}="hwdb 'mouse:${bus}:v$attr{id/vendor}p$attr{id/product}:name:$attr{name}:'"`;

export const RULE_LINES = [
    '# Installed by the Scroll Speed GNOME extension.',
    'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"',
    'SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_MOUSE}=="1", ENV{ID_INPUT_KEYBOARD}!="1", ENV{ID_INPUT_POINTINGSTICK}!="1", TAG+="uaccess"',
    `${CLONE}, ATTRS{id/bustype}=="0003", ${HWDB('usb')}`,
    `${CLONE}, ATTRS{id/bustype}=="0005", ${HWDB('bluetooth')}`,
];

// Re-runs the rules on the devices already plugged in, so nothing has to be
// unplugged or restarted.
const RELOAD = [
    'udevadm control --reload',
    'udevadm trigger --action=change --subsystem-match=input --property-match=ID_INPUT_MOUSE=1',
    'udevadm trigger --action=change --subsystem-match=misc --sysname-match=uinput',
].join(' && ');

export function installScript() {
    // Each line is a single-quoted shell word, so a quote inside it has to close
    // the word, add an escaped quote and reopen it.
    const lines = RULE_LINES.map(line => `'${line.replaceAll("'", "'\\''")}'`).join(' ');
    return `umask 022 && printf '%s\\n' ${lines} > ${RULE_PATH} && ${RELOAD}`;
}

export function removeScript() {
    return `rm -f ${RULE_PATH} && ${RELOAD}`;
}

export function installArgv() {
    return ['pkexec', '/bin/sh', '-c', installScript()];
}

export function removeArgv() {
    return ['pkexec', '/bin/sh', '-c', removeScript()];
}

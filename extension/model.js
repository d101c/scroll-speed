// SPDX-License-Identifier: GPL-2.0-or-later
// Pure helpers shared by the extension and its preferences.

export const FACTOR_MIN = 0.1;
export const FACTOR_MAX = 5;
export const SLIDER_MIN = Math.log2(FACTOR_MIN);
export const SLIDER_MAX = Math.log2(FACTOR_MAX);

const FACTOR_STEP = 0.05;
// The slider snaps to the normal speed when it lands this close to it, which
// makes 1× an easy target (a 0.05 step alone would be a few pixels wide).
const SNAP_TO_NORMAL = 0.07;

function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
}

// The slider is logarithmic so that ×0.5 and ×2 sit at the same distance from
// the normal speed.
export function sliderToFactor(position) {
    const raw = 2 ** position;
    if (Math.abs(raw - 1) < SNAP_TO_NORMAL)
        return 1;
    const factor = Math.round(raw / FACTOR_STEP) * FACTOR_STEP;
    return Number(clamp(factor, FACTOR_MIN, FACTOR_MAX).toFixed(2));
}

export function factorToSlider(factor) {
    return clamp(Math.log2(factor), SLIDER_MIN, SLIDER_MAX);
}

export function formatFactor(factor, locale = undefined) {
    return `${factor.toLocaleString(locale, {maximumFractionDigits: 2})}×`;
}

// The configuration line understood by the helper.
export function helperConfig({enabled, vertical, horizontalLinked, horizontal, acceleration}) {
    return {
        enabled,
        vertical,
        horizontal: horizontalLinked ? vertical : horizontal,
        acceleration,
    };
}

export function isNeutral(config) {
    return !config.enabled ||
        (config.vertical === 1 && config.horizontal === 1 && config.acceleration === 0);
}

// Turns a helper `--probe` report into what the preferences should show.
// `probe` is null when the helper could not be run at all.
export function summarize(probe, ruleInstalled) {
    if (!probe)
        return {kind: 'helper-failed', devices: []};

    const devices = probe.devices ?? [];
    // No /dev/uinput at all: the kernel module is not loaded, and replugging
    // the mouse would not change that.
    if (probe.uinput === 'missing' && ruleInstalled)
        return {kind: 'uinput-missing', devices};
    const blocked = probe.uinput !== 'ok' || devices.some(device => !device.access);
    if (blocked)
        return {kind: ruleInstalled ? 'authorization-pending' : 'needs-authorization', devices};
    if (devices.length === 0)
        return {kind: 'no-mouse', devices};
    return {kind: 'ready', devices};
}

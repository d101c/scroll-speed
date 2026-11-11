#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Scroll Speed helper: rescales scrolling below the compositor.

A Wayland compositor delivers scroll events to applications before any shell
extension can see them, so the speed has to be changed where the events come
from. This helper grabs each device (EVIOCGRAB), mirrors it through a uinput
clone and forwards every event untouched except what makes the view scroll:

- mouse: the wheel axes are multiplied by the factor. The clone always speaks
  the high-resolution wheel protocol (120 units per detent), so fractional
  factors reach applications as fractional scroll steps;
- touchpad: scrolling does not exist in the kernel events, libinput derives it
  from the motion two fingers have in common. That shared motion is what gets
  multiplied, by shifting both fingers together;
- pointing stick: the motion made while the middle button is held, which is
  what libinput turns into scrolling.

Standard library only. Needs read access to the event nodes and write access
to /dev/uinput (see the udev rule installed from the preferences).
"""

import argparse
import errno
import fcntl
import json
import math
import os
import selectors
import signal
import stat
import struct
import sys
import time

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
EV_MSC = 0x04

SYN_REPORT = 0
SYN_DROPPED = 3

REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
REL_WHEEL_HI_RES = 0x0B
REL_HWHEEL_HI_RES = 0x0C

ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
ABS_MT_FIRST = 0x2F
ABS_MT_LAST = 0x3E

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_TOOL_FINGER = 0x145
INPUT_PROP_DIRECT = 0x01
INPUT_PROP_SEMI_MT = 0x03
INPUT_PROP_POINTING_STICK = 0x05

KINDS = ('mouse', 'touchpad', 'pointingstick')

KEY_MAX = 0x2FF
REL_MAX = 0x0F
ABS_MAX = 0x3F
MSC_MAX = 0x07
INPUT_PROP_MAX = 0x1F

DETENT = 120

# Marks our clones so that a scan never grabs them.
CLONE_PHYS = 'scroll-speed/clone'

EVENT = struct.Struct('llHHi')
LONG_BITS = struct.calcsize('l') * 8

# Wheel speed (detents per second) where acceleration starts and saturates,
# and the extra gain reached at saturation when acceleration is 1.0.
ACCEL_START = 8.0
ACCEL_FULL = 40.0
ACCEL_GAIN = 4.0
# Two wheel events further apart than this belong to different gestures.
GESTURE_GAP = 0.25

FACTOR_MIN = 0.05
FACTOR_MAX = 20.0

# Touchpad. Two fingers have to travel this far together before their motion
# is rescaled: libinput spends about that distance deciding between a tap, a
# scroll and a pinch, and that decision must not depend on the factor.
SCROLL_START_MM = 2.0
# Two fingers scrolling slowly report their motion in different frames; moves
# closer together than this still count as one common motion.
MATCH_WINDOW = 0.1
# libinput discards a touch that moves more than 20 mm, or whose step grows by
# more than 7 mm, between two frames 12 ms apart (it takes that for a firmware
# glitch). An amplified scroll is kept below both limits.
JUMP_FRAME = 0.012
JUMP_MAX_MM = 15.0
JUMP_RISE_MM = 5.0
JUMP_IRREGULAR = 0.030
CLICK_BUTTONS = (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE)

# Set SCROLL_SPEED_DEBUG=1 to copy every status change to stderr (the journal,
# when the helper is started by GNOME Shell).
DEBUG = bool(os.environ.get('SCROLL_SPEED_DEBUG'))


def clamp(value, low, high):
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Pure logic


class AxisScaler:
    """Scales one wheel axis, carrying the fractional remainder forward."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.sign = 0
        self.remainder = 0.0
        self.detent_acc = 0
        self.speed = 0.0
        self.last_time = None

    def _boost(self, magnitude, now, accel):
        previous = self.last_time
        self.last_time = now
        if previous is None or now - previous > GESTURE_GAP or now < previous:
            self.speed = 0.0
            return 1.0
        dt = max(now - previous, 0.001)
        instant = (magnitude / DETENT) / dt
        self.speed = instant if self.speed == 0.0 else (self.speed + instant) / 2
        if accel <= 0:
            return 1.0
        ramp = clamp((self.speed - ACCEL_START) / (ACCEL_FULL - ACCEL_START), 0.0, 1.0)
        return 1.0 + accel * ACCEL_GAIN * ramp

    def feed(self, value, now, factor, accel=0.0):
        """Takes a high-resolution delta, returns (hi_res, detents) to emit."""
        if value == 0:
            return 0, 0
        sign = 1 if value > 0 else -1
        if sign != self.sign:
            self.reset()
            self.sign = sign
        exact = value * factor * self._boost(abs(value), now, accel) + self.remainder
        hi_res = math.trunc(exact)
        self.remainder = exact - hi_res
        self.detent_acc += hi_res
        detents = math.trunc(self.detent_acc / DETENT)
        self.detent_acc -= detents * DETENT
        return hi_res, detents


class FrameTransformer:
    """Rewrites the wheel events of one device, frame by frame."""

    def __init__(self):
        self.vertical = AxisScaler()
        self.horizontal = AxisScaler()
        self._passthrough = []
        self._hi = {REL_WHEEL_HI_RES: 0, REL_HWHEEL_HI_RES: 0}
        self._lo = {REL_WHEEL: 0, REL_HWHEEL: 0}
        self._saw_hi = set()

    def reset(self):
        self.vertical.reset()
        self.horizontal.reset()
        self.drop_frame()

    def drop_frame(self):
        self._passthrough = []
        for code in self._hi:
            self._hi[code] = 0
        for code in self._lo:
            self._lo[code] = 0
        self._saw_hi = set()

    def _axis_delta(self, hi_code, lo_code):
        # A frame that carries the high-resolution value is authoritative; a
        # low-resolution-only frame (old kernels, PS/2 mice) counts whole detents.
        if hi_code in self._saw_hi:
            return self._hi[hi_code]
        return self._lo[lo_code] * DETENT

    def feed(self, etype, code, value, now, config):
        """Feeds one event; returns the finished frame as a list of
        (type, code, value) when the event closes one, else None."""
        if etype == EV_REL and code in self._hi:
            self._hi[code] += value
            self._saw_hi.add(code)
            return None
        if etype == EV_REL and code in self._lo:
            self._lo[code] += value
            return None
        if not (etype == EV_SYN and code == SYN_REPORT):
            self._passthrough.append((etype, code, value))
            return None

        out = self._passthrough
        v_hi, v_lo = self.vertical.feed(
            self._axis_delta(REL_WHEEL_HI_RES, REL_WHEEL), now,
            config['vertical'], config['acceleration'])
        h_hi, h_lo = self.horizontal.feed(
            self._axis_delta(REL_HWHEEL_HI_RES, REL_HWHEEL), now,
            config['horizontal'], config['acceleration'])
        if v_hi:
            out.append((EV_REL, REL_WHEEL_HI_RES, v_hi))
        if v_lo:
            out.append((EV_REL, REL_WHEEL, v_lo))
        if h_hi:
            out.append((EV_REL, REL_HWHEEL_HI_RES, h_hi))
        if h_lo:
            out.append((EV_REL, REL_HWHEEL, h_lo))
        self.drop_frame()
        if not out:
            return []
        out.append((EV_SYN, SYN_REPORT, 0))
        return out

    def resync(self, keys, slots=None):
        return []


class StickTransformer:
    """Rewrites a pointing stick: the motion made while the middle button is
    held, which libinput turns into scrolling, is multiplied by the factor."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.held = False
        self.remainder = [0.0, 0.0]
        self._frame = []

    def drop_frame(self):
        self._frame = []

    def resync(self, keys, slots=None):
        self.held = BTN_MIDDLE in keys
        self.remainder = [0.0, 0.0]
        return []

    def feed(self, etype, code, value, now, config):
        if not (etype == EV_SYN and code == SYN_REPORT):
            self._frame.append((etype, code, value))
            return None
        frame, self._frame = self._frame, []
        was_held = self.held
        for ftype, fcode, fvalue in frame:
            if ftype == EV_KEY and fcode == BTN_MIDDLE:
                self.held = bool(fvalue)
        if not (was_held and self.held):
            self.remainder = [0.0, 0.0]
            out = frame
        else:
            motion = [0, 0]
            out = []
            for event in frame:
                if event[0] == EV_REL and event[1] in (REL_X, REL_Y):
                    motion[event[1]] += event[2]
                else:
                    out.append(event)
            for axis in (REL_X, REL_Y):
                exact = motion[axis] * config['pointingstick'] + self.remainder[axis]
                # The nudge keeps ten steps of 0.1 from adding up to 0.9999.
                whole = math.trunc(exact + math.copysign(1e-9, exact))
                self.remainder[axis] = exact - whole
                if whole:
                    out.append((EV_REL, axis, whole))
        if not out:
            return []
        out.append((EV_SYN, SYN_REPORT, 0))
        return out


class Touch:
    """One multi-touch slot: where the finger really is, and how far the
    position shown to the compositor has been shifted from it."""

    __slots__ = ('tid', 'pos', 'off', 'out', 'order', 'fresh', 'delta', 'pend',
                 'pend_time', 'step_mm', 'step_time')

    def __init__(self):
        self.tid = -1
        self.pos = [0, 0]
        self.off = [0.0, 0.0]
        # A new uinput device starts with every slot at 0, 0.
        self.out = [0, 0]
        self.order = 0
        self.fresh = False
        self.delta = [0, 0]
        self.pend = [0, 0]
        self.pend_time = [0.0, 0.0]
        self.step_mm = 0.0
        self.step_time = None


class TouchpadTransformer:
    """Rewrites a multi-touch touchpad so that two-finger scrolling is faster
    or slower.

    libinput scrolls by the average motion of the two fingers. While exactly
    two fingers are down and move the same way, both reported positions are
    shifted by (factor - 1) times that average: the average becomes `factor`
    times larger, while the distance and angle between the fingers (what a
    pinch is made of) stay exactly as they are. One finger, three fingers and
    a finger moving next to a resting thumb are forwarded unchanged.
    """

    def __init__(self):
        self.slots = []
        self.axes = ((0, 0, 0), (0, 0, 0))
        self.pointer = None
        self.reset()

    def configure(self, description):
        """Takes the axis ranges of the device; raises OSError when it is not
        a slot-based multi-touch device."""
        info = {axis: struct.unpack('iiiiii', raw)
                for axis, raw in description.get('abs', {}).items()}
        needed = (ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID)
        if any(axis not in info for axis in needed):
            raise OSError(errno.EINVAL, 'not a multi-touch touchpad')
        self.slots = [Touch() for _ in range(max(1, info[ABS_MT_SLOT][2] + 1))]
        # (minimum, maximum, units per millimetre)
        self.axes = tuple((info[axis][1], info[axis][2], info[axis][5])
                          for axis in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y))
        # The single-touch position the kernel derives from the oldest finger.
        self.pointer = ([info[ABS_X][0], info[ABS_Y][0]]
                        if ABS_X in info and ABS_Y in info else None)
        self.reset()

    def reset(self):
        self._frame = []
        self.in_slot = 0
        self.out_slot = 0
        self.sequence = 0
        self.pressed = set()
        self.pair_key = None
        self.travel = [0.0, 0.0]
        self.scaling = False

    def drop_frame(self):
        self._frame = []

    # -- slot bookkeeping

    def _select(self, out, index):
        if self.out_slot != index:
            out.append((EV_ABS, ABS_MT_SLOT, index))
            self.out_slot = index

    def _begin(self, touch, tid):
        self.sequence += 1
        touch.tid = tid
        touch.order = self.sequence
        touch.fresh = True
        touch.off = [0.0, 0.0]
        touch.pend = [0, 0]
        touch.step_mm = 0.0
        touch.step_time = None

    def _end(self, touch):
        touch.tid = -1
        touch.off = [0.0, 0.0]
        touch.pend = [0, 0]

    def _target(self, touch, axis, extra=0.0):
        low, high, _res = self.axes[axis]
        value = round(touch.pos[axis] + touch.off[axis] + extra)
        return clamp(value, low, high) if high > low else value

    def _step_mm(self, touch, extra, now):
        """How far the reported position would move this frame, the way
        libinput measures it: in millimetres per 12 ms. None when libinput
        would not judge this frame (irregular timing)."""
        if touch.step_time is None:
            return None
        elapsed = now - touch.step_time
        if elapsed <= 0 or elapsed > JUMP_IRREGULAR:
            return None
        mm = [(self._target(touch, axis, extra[axis]) - touch.out[axis]) / self.axes[axis][2]
              for axis in (0, 1)]
        return math.hypot(*mm) * JUMP_FRAME / elapsed

    # -- scaling

    def _common_motion(self, pair, now):
        """The motion the two fingers have in common on each axis: their
        average when both moved the same way, nothing otherwise."""
        common = [0.0, 0.0]
        for axis in (0, 1):
            low, high, res = self.axes[axis]
            leftover = max(2, res * 0.25 if res > 0 else (high - low) / 400)
            for touch in pair:
                if touch.delta[axis]:
                    touch.pend[axis] += touch.delta[axis]
                    touch.pend_time[axis] = now
                elif touch.pend[axis] and not 0 <= now - touch.pend_time[axis] <= MATCH_WINDOW:
                    touch.pend[axis] = 0
            first, second = pair[0].pend[axis], pair[1].pend[axis]
            if first * second > 0:
                common[axis] = (first + second) / 2
                pair[0].pend[axis] = pair[1].pend[axis] = 0
            else:
                # A finger moving on its own (the other one rests) leaves
                # little behind to be matched with a later twitch.
                for touch in pair:
                    touch.pend[axis] = clamp(touch.pend[axis], -leftover, leftover)
        return common

    def _scale(self, pair, now, factor):
        common = self._common_motion(pair, now)
        has_size = self.axes[0][2] > 0 and self.axes[1][2] > 0
        if not self.scaling:
            self.travel[0] += common[0]
            self.travel[1] += common[1]
            if has_size and math.hypot(self.travel[0] / self.axes[0][2],
                                       self.travel[1] / self.axes[1][2]) < SCROLL_START_MM:
                return
            self.scaling = True
            if has_size:
                return
        extra = [(factor - 1.0) * common[0], (factor - 1.0) * common[1]]
        if not (extra[0] or extra[1]):
            return
        share = 1.0
        if factor > 1.0 and has_size:
            share = self._jump_safe_share(pair, extra, now)
        for touch in pair:
            touch.off[0] += share * extra[0]
            touch.off[1] += share * extra[1]

    def _jump_safe_share(self, pair, extra, now):
        """The largest part of `extra` that libinput will not mistake for a
        pointer jump (see JUMP_MAX_MM); what is left out is simply lost."""
        def acceptable(share):
            part = (share * extra[0], share * extra[1])
            for touch in pair:
                step = self._step_mm(touch, part, now)
                # libinput forgets the previous step whenever it restarts a
                # touch's history (a finger landing, a gesture starting), so
                # the rise is counted from zero.
                if step is not None and step > JUMP_RISE_MM:
                    return False
            return True

        if acceptable(1.0):
            return 1.0
        low, high = 0.0, 1.0
        for _ in range(10):
            middle = (low + high) / 2
            if acceptable(middle):
                low = middle
            else:
                high = middle
        return low

    # -- output

    def _emit_positions(self, out, now):
        """Appends the position of every finger whose reported position
        changed, then the single-touch position."""
        oldest = None
        for index, touch in enumerate(self.slots):
            if touch.tid == -1:
                continue
            if oldest is None or touch.order < oldest.order:
                oldest = touch
            target = [self._target(touch, 0), self._target(touch, 1)]
            if target != touch.out:
                if self.axes[0][2] > 0 and self.axes[1][2] > 0 and not touch.fresh:
                    step = self._step_mm(touch, (0.0, 0.0), now)
                    if step is not None:
                        touch.step_mm = step
                self._select(out, index)
                for axis, code in ((0, ABS_MT_POSITION_X), (1, ABS_MT_POSITION_Y)):
                    if target[axis] != touch.out[axis]:
                        out.append((EV_ABS, code, target[axis]))
                touch.out = target
                touch.step_time = now
            elif touch.fresh:
                touch.step_time = now
            touch.fresh = False
        if oldest is not None and self.pointer is not None:
            for axis, code in ((0, ABS_X), (1, ABS_Y)):
                if oldest.out[axis] != self.pointer[axis]:
                    self.pointer[axis] = oldest.out[axis]
                    out.append((EV_ABS, code, oldest.out[axis]))

    def feed(self, etype, code, value, now, config):
        """Feeds one event; returns the finished frame as a list of
        (type, code, value) when the event closes one, else None."""
        if not (etype == EV_SYN and code == SYN_REPORT):
            self._frame.append((etype, code, value))
            return None
        frame, self._frame = self._frame, []
        out = []
        for touch in self.slots:
            touch.delta = [0, 0]
        for ftype, fcode, fvalue in frame:
            if ftype == EV_KEY:
                if fcode in CLICK_BUTTONS:
                    if fvalue:
                        self.pressed.add(fcode)
                    else:
                        self.pressed.discard(fcode)
                out.append((ftype, fcode, fvalue))
            elif ftype != EV_ABS:
                out.append((ftype, fcode, fvalue))
            elif fcode == ABS_MT_SLOT:
                if 0 <= fvalue < len(self.slots):
                    self.in_slot = fvalue
            elif fcode in (ABS_X, ABS_Y) and self.pointer is not None:
                pass  # rebuilt from the oldest finger
            elif ABS_MT_FIRST <= fcode <= ABS_MT_LAST:
                touch = self.slots[self.in_slot]
                if fcode in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                    axis = fcode - ABS_MT_POSITION_X
                    if touch.tid != -1 and not touch.fresh:
                        touch.delta[axis] += fvalue - touch.pos[axis]
                    touch.pos[axis] = fvalue
                    continue
                if fcode == ABS_MT_TRACKING_ID:
                    if fvalue == -1:
                        self._end(touch)
                    else:
                        self._begin(touch, fvalue)
                self._select(out, self.in_slot)
                out.append((ftype, fcode, fvalue))
            else:
                out.append((ftype, fcode, fvalue))

        active = [touch for touch in self.slots if touch.tid != -1]
        # A clickpad held down is a click or a drag, not a scroll.
        steady = (len(active) == 2 and not self.pressed
                  and not any(touch.fresh for touch in active))
        key = tuple(touch.order for touch in active) if steady else None
        if key != self.pair_key:
            self.pair_key = key
            self.travel = [0.0, 0.0]
            self.scaling = False
            for touch in active:
                touch.pend = [0, 0]
        if steady:
            self._scale(active, now, config['touchpad'])

        self._emit_positions(out, now)
        if not out:
            return []
        out.append((EV_SYN, SYN_REPORT, 0))
        return out

    def resync(self, keys, slots=None):
        """Realigns with the device (when it is taken, or after the kernel
        dropped events). `slots` is (tracking ids, x positions, y positions,
        current slot). Returns the events that bring the clone up to date."""
        self.pressed = set(keys) & set(CLICK_BUTTONS)
        self.pair_key = None
        if slots is None:
            return []
        tids, xs, ys, current = slots
        ended, started = [], []
        for index, touch in enumerate(self.slots):
            if touch.tid != -1 and touch.tid != tids[index]:
                self._select(ended, index)
                ended.append((EV_ABS, ABS_MT_TRACKING_ID, -1))
                self._end(touch)
        for index, touch in enumerate(self.slots):
            touch.pos = [xs[index], ys[index]]
            touch.delta = [0, 0]
            if tids[index] != -1 and touch.tid == -1:
                self._begin(touch, tids[index])
                self._select(started, index)
                started.append((EV_ABS, ABS_MT_TRACKING_ID, tids[index]))
        self._emit_positions(started, time.time())
        if 0 <= current < len(self.slots):
            self.in_slot = current
        out = []
        for events in (ended, started):
            if events:
                out += events + [(EV_SYN, SYN_REPORT, 0)]
        return out


TRANSFORMERS = {
    'mouse': FrameTransformer,
    'touchpad': TouchpadTransformer,
    'pointingstick': StickTransformer,
}


def parse_bitmask(text):
    """Parses a sysfs capability bitmask (hex words, most significant first)."""
    value = 0
    for word in text.split():
        value = (value << LONG_BITS) | int(word, 16)
    return value


# udev calls a device a keyboard when it has all of the keys 1 to 31
# (Esc, the digit row, Q to P…); the udev rule gives no access to those.
KEYBOARD_KEYS = 0xFFFFFFFE


def is_wheel_mouse(rel_bits, key_bits, prop_bits):
    """A relative pointer with a left button and a wheel. Pointing sticks and
    combined keyboard/mouse nodes are excluded, like in the udev rule."""
    has_motion = rel_bits >> REL_X & 1 and rel_bits >> REL_Y & 1
    has_wheel = rel_bits >> REL_WHEEL & 1 or rel_bits >> REL_HWHEEL & 1
    has_button = key_bits >> BTN_LEFT & 1
    is_stick = prop_bits >> INPUT_PROP_POINTING_STICK & 1
    is_keyboard = key_bits & KEYBOARD_KEYS == KEYBOARD_KEYS
    return bool(has_motion and has_wheel and has_button
                and not is_stick and not is_keyboard)


def device_kind(rel_bits, key_bits, abs_bits, prop_bits):
    """Which of KINDS a device is, from its capability bitmasks, or None when
    it is not something whose scrolling this helper rescales."""
    if key_bits & KEYBOARD_KEYS == KEYBOARD_KEYS:
        return None
    has_motion = rel_bits >> REL_X & 1 and rel_bits >> REL_Y & 1
    if prop_bits >> INPUT_PROP_POINTING_STICK & 1:
        return 'pointingstick' if has_motion and key_bits >> BTN_MIDDLE & 1 else None
    multi_touch = all(abs_bits >> axis & 1 for axis in (
        ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID))
    if multi_touch:
        # A touchscreen is "direct"; a semi-mt pad only reports a bounding box.
        indirect = not (prop_bits >> INPUT_PROP_DIRECT & 1
                        or prop_bits >> INPUT_PROP_SEMI_MT & 1)
        return 'touchpad' if indirect and key_bits >> BTN_TOOL_FINGER & 1 else None
    return 'mouse' if is_wheel_mouse(rel_bits, key_bits, prop_bits) else None


def normalize_config(raw, base=None):
    config = dict(base or {'enabled': True, 'vertical': 1.0, 'horizontal': 1.0,
                           'acceleration': 0.0, 'touchpad': 1.0, 'pointingstick': 1.0})
    if 'enabled' in raw:
        config['enabled'] = bool(raw['enabled'])
    for key in ('vertical', 'horizontal', 'touchpad', 'pointingstick'):
        if key in raw:
            config[key] = clamp(float(raw[key]), FACTOR_MIN, FACTOR_MAX)
    if 'acceleration' in raw:
        config['acceleration'] = clamp(float(raw['acceleration']), 0.0, 1.0)
    return config


NEUTRAL_CONFIG = normalize_config({})


def kind_is_neutral(config, kind):
    """True when the configuration leaves devices of that kind unchanged."""
    if not config['enabled']:
        return True
    if kind == 'mouse':
        return (config['vertical'] == 1.0 and config['horizontal'] == 1.0 and
                config['acceleration'] == 0.0)
    return config[kind] == 1.0


def is_neutral(config):
    """True when the configuration would leave every device unchanged."""
    return all(kind_is_neutral(config, kind) for kind in KINDS)


# --------------------------------------------------------------------------
# Kernel interface

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, kind, number, size):
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | number


EVIOCGID = _ioc(_IOC_READ, 'E', 0x02, 8)
EVIOCGRAB = _ioc(_IOC_WRITE, 'E', 0x90, 4)
UI_DEV_CREATE = _ioc(0, 'U', 1, 0)
UI_DEV_DESTROY = _ioc(0, 'U', 2, 0)
UI_DEV_SETUP = _ioc(_IOC_WRITE, 'U', 3, 92)
UI_ABS_SETUP = _ioc(_IOC_WRITE, 'U', 4, 28)
UI_SET_EVBIT = _ioc(_IOC_WRITE, 'U', 100, 4)
UI_SET_KEYBIT = _ioc(_IOC_WRITE, 'U', 101, 4)
UI_SET_RELBIT = _ioc(_IOC_WRITE, 'U', 102, 4)
UI_SET_ABSBIT = _ioc(_IOC_WRITE, 'U', 103, 4)
UI_SET_MSCBIT = _ioc(_IOC_WRITE, 'U', 104, 4)
UI_SET_PHYS = _ioc(_IOC_WRITE, 'U', 108, struct.calcsize('P'))
UI_SET_PROPBIT = _ioc(_IOC_WRITE, 'U', 110, 4)


def _eviocgname(length):
    return _ioc(_IOC_READ, 'E', 0x06, length)


def _eviocgprop(length):
    return _ioc(_IOC_READ, 'E', 0x09, length)


def _eviocgkey(length):
    return _ioc(_IOC_READ, 'E', 0x18, length)


def _eviocgbit(etype, length):
    return _ioc(_IOC_READ, 'E', 0x20 + etype, length)


def _eviocgabs(axis):
    return _ioc(_IOC_READ, 'E', 0x40 + axis, 24)


def _eviocgmtslots(length):
    return _ioc(_IOC_READ, 'E', 0x0A, length)


def _ui_get_sysname(length):
    return _ioc(_IOC_READ, 'U', 44, length)


def _read_bits(fd, request_for_length, max_code):
    length = max_code // 8 + 1
    buffer = bytearray(length)
    fcntl.ioctl(fd, request_for_length(length), buffer)
    return [code for code in range(max_code + 1) if buffer[code // 8] >> (code % 8) & 1]


def keys_down(fd):
    return _read_bits(fd, _eviocgkey, KEY_MAX)


def read_mt_state(fd, count):
    """The multi-touch state of a device: (tracking ids, x positions,
    y positions, current slot)."""
    values = []
    for code in (ABS_MT_TRACKING_ID, ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
        buffer = bytearray(struct.pack('I', code) + bytes(4 * count))
        fcntl.ioctl(fd, _eviocgmtslots(len(buffer)), buffer)
        values.append(list(struct.unpack('%di' % count, buffer[4:])))
    info = bytearray(24)
    fcntl.ioctl(fd, _eviocgabs(ABS_MT_SLOT), info)
    return values[0], values[1], values[2], struct.unpack('iiiiii', info)[0]


def udev_properties(fd):
    """The udev properties of an open device node ({} when unknown)."""
    try:
        number = os.fstat(fd).st_rdev
        path = '/run/udev/data/c%d:%d' % (os.major(number), os.minor(number))
        with open(path, encoding='utf-8', errors='replace') as stream:
            lines = [line[2:].rstrip('\n') for line in stream if line.startswith('E:')]
    except OSError:
        return {}
    return dict(line.split('=', 1) for line in lines if '=' in line)


def restore_fuzz(description, properties):
    """udev moves the kernel's axis fuzz into LIBINPUT_FUZZ_xx properties, for
    libinput to apply itself. Putting it back in the description lets the same
    happen to the clone, so that it is smoothed exactly like the original."""
    for axis, raw in list(description['abs'].items()):
        try:
            fuzz = int(properties.get('LIBINPUT_FUZZ_%02x' % axis, ''))
        except ValueError:
            continue
        info = list(struct.unpack('iiiiii', raw))
        info[3] = fuzz
        description['abs'][axis] = struct.pack('iiiiii', *info)


def pack_events(events):
    return b''.join(EVENT.pack(0, 0, etype, code, value) for etype, code, value in events)


def describe_source(fd):
    """Reads everything needed to build a clone of an evdev device."""
    name = bytearray(80)
    fcntl.ioctl(fd, _eviocgname(len(name)), name)
    ident = bytearray(8)
    fcntl.ioctl(fd, EVIOCGID, ident)
    abs_codes = _read_bits(fd, lambda n: _eviocgbit(EV_ABS, n), ABS_MAX)
    abs_info = {}
    for axis in abs_codes:
        info = bytearray(24)
        fcntl.ioctl(fd, _eviocgabs(axis), info)
        abs_info[axis] = bytes(info)
    return {
        'name': name.split(b'\0', 1)[0].decode('utf-8', 'replace'),
        'id': struct.unpack('HHHH', ident),
        'keys': _read_bits(fd, lambda n: _eviocgbit(EV_KEY, n), KEY_MAX),
        'rel': _read_bits(fd, lambda n: _eviocgbit(EV_REL, n), REL_MAX),
        'abs': abs_info,
        'msc': _read_bits(fd, lambda n: _eviocgbit(EV_MSC, n), MSC_MAX),
        'props': _read_bits(fd, _eviocgprop, INPUT_PROP_MAX),
    }


def create_uinput_device(description, phys=None):
    """Creates a uinput device from a description; returns its fd."""
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    try:
        groups = (
            (EV_KEY, UI_SET_KEYBIT, description.get('keys', [])),
            (EV_REL, UI_SET_RELBIT, description.get('rel', [])),
            (EV_ABS, UI_SET_ABSBIT, list(description.get('abs', {}))),
            (EV_MSC, UI_SET_MSCBIT, description.get('msc', [])),
        )
        for etype, request, codes in groups:
            if not codes:
                continue
            fcntl.ioctl(fd, UI_SET_EVBIT, etype)
            for code in codes:
                fcntl.ioctl(fd, request, code)
        for prop in description.get('props', []):
            fcntl.ioctl(fd, UI_SET_PROPBIT, prop)
        for axis, info in description.get('abs', {}).items():
            fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack('HH', axis, 0) + info)
        if phys:
            fcntl.ioctl(fd, UI_SET_PHYS, phys.encode() + b'\0')
        bustype, vendor, product, version = description['id']
        name = description['name'].encode()[:79]
        fcntl.ioctl(fd, UI_DEV_SETUP,
                    struct.pack('HHHH80sI', bustype, vendor, product, version, name, 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def uinput_event_node(fd):
    """Returns the /dev/input/eventN path of a uinput device, or None."""
    buffer = bytearray(64)
    fcntl.ioctl(fd, _ui_get_sysname(len(buffer)), buffer)
    sysname = buffer.split(b'\0', 1)[0].decode()
    base = '/sys/devices/virtual/input/' + sysname
    try:
        for entry in os.listdir(base):
            if entry.startswith('event'):
                return '/dev/input/' + entry
    except OSError:
        pass
    return None


def clone_description(source):
    """The clone mirrors the source and always offers high-resolution wheels."""
    rel = set(source['rel'])
    if REL_WHEEL in rel or REL_WHEEL_HI_RES in rel:
        rel |= {REL_WHEEL, REL_WHEEL_HI_RES}
    if REL_HWHEEL in rel or REL_HWHEEL_HI_RES in rel:
        rel |= {REL_HWHEEL, REL_HWHEEL_HI_RES}
    clone = dict(source)
    clone['rel'] = sorted(rel)
    return clone


def _read_sysfs(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            return stream.read().strip()
    except OSError:
        return ''


def list_input_devices():
    """Yields (event node path, name, phys, kind or None) from sysfs."""
    try:
        entries = sorted(os.listdir('/sys/class/input'))
    except OSError:
        return
    for entry in entries:
        if not entry.startswith('event'):
            continue
        base = '/sys/class/input/%s/device/' % entry
        rel = parse_bitmask(_read_sysfs(base + 'capabilities/rel') or '0')
        key = parse_bitmask(_read_sysfs(base + 'capabilities/key') or '0')
        absolute = parse_bitmask(_read_sysfs(base + 'capabilities/abs') or '0')
        props = parse_bitmask(_read_sysfs(base + 'properties') or '0')
        yield ('/dev/input/' + entry, _read_sysfs(base + 'name'),
               _read_sysfs(base + 'phys'), device_kind(rel, key, absolute, props))


def uinput_state():
    if not os.path.exists('/dev/uinput'):
        return 'missing'
    return 'ok' if os.access('/dev/uinput', os.W_OK) else 'denied'


# --------------------------------------------------------------------------
# Runtime

# How long a fresh clone is left alone so the compositor can open it.
CLONE_SETTLE = 0.3
RESCAN_INTERVAL = 2.0
# A device that cannot be taken (grabbed by an input remapper, clone creation
# failing…) is left alone for this long before the next attempt.
RETRY_DELAY = 30.0


class Device:
    """One grabbed (or about to be grabbed) source device and its clone."""

    def __init__(self, path, name, kind='mouse'):
        self.path = path
        self.name = name
        self.kind = kind
        self.fd = -1
        self.clone_fd = -1
        self.state = 'waiting'
        self.grabbed = False
        self.ready_at = 0.0
        self.down = set()
        self.transformer = TRANSFORMERS[kind]()
        self.skip_frame = False

    def open(self):
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        source = describe_source(self.fd)
        restore_fuzz(source, udev_properties(self.fd))
        description = clone_description(source)
        self.name = description['name'] or self.name
        if self.kind == 'touchpad':
            self.transformer.configure(description)
        self.clone_fd = create_uinput_device(description, CLONE_PHYS)
        self.ready_at = time.monotonic() + CLONE_SETTLE

    def drain(self):
        try:
            while os.read(self.fd, EVENT.size * 64):
                pass
        except BlockingIOError:
            pass

    def busy(self):
        """A button is held on the device or, for a touchpad, a finger is on it."""
        if keys_down(self.fd):
            return True
        if self.kind == 'touchpad':
            return any(tid != -1 for tid in
                       read_mt_state(self.fd, len(self.transformer.slots))[0])
        return False

    def engaged(self):
        """Same question, answered from what was forwarded to the clone."""
        if self.down:
            return True
        return self.kind == 'touchpad' and any(
            touch.tid != -1 for touch in self.transformer.slots)

    def try_grab(self):
        """Takes the device once the clone has settled and no button is held
        (for a touchpad: no finger is down), so that the compositor never sees
        a press without its release."""
        if time.monotonic() < self.ready_at or self.busy():
            return False
        fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        # What is queued predates the grab and already reached the compositor.
        # A press landing after this drain stays queued and is forwarded.
        self.drain()
        if self.busy():
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
            return False
        self.grabbed = True
        self.state = 'active'
        self.transformer.reset()
        events = self._resync()
        if events:
            os.write(self.clone_fd, pack_events(events))
        return True

    def forward(self, config):
        """Reads pending events and writes the rewritten frames to the clone."""
        data = os.read(self.fd, EVENT.size * 64)
        if not data:
            raise OSError(errno.ENODEV, 'device closed')
        if not self.grabbed:
            return
        out = []
        for sec, usec, etype, code, value in EVENT.iter_unpack(
                data[:len(data) - len(data) % EVENT.size]):
            if etype == EV_SYN and code == SYN_DROPPED:
                self.skip_frame = True
                continue
            if self.skip_frame:
                if etype == EV_SYN and code == SYN_REPORT:
                    self.skip_frame = False
                    self.transformer.drop_frame()
                    out.extend(self._resync())
                continue
            frame = self.transformer.feed(etype, code, value, sec + usec / 1e6, config)
            if not frame:
                continue
            for ftype, fcode, fvalue in frame:
                if ftype == EV_KEY:
                    if fvalue:
                        self.down.add(fcode)
                    else:
                        self.down.discard(fcode)
            out.extend(frame)
        if out:
            os.write(self.clone_fd, pack_events(out))

    def _resync(self):
        """Realigns the clone with the device: when it is taken, and after the
        kernel dropped events."""
        actual = set(keys_down(self.fd))
        events = [(EV_KEY, code, 0) for code in self.down - actual]
        events += [(EV_KEY, code, 1) for code in actual - self.down]
        self.down = actual
        if events:
            events.append((EV_SYN, SYN_REPORT, 0))
        slots = None
        if self.kind == 'touchpad':
            slots = read_mt_state(self.fd, len(self.transformer.slots))
        return events + self.transformer.resync(actual, slots)

    def close(self):
        if self.clone_fd >= 0:
            try:
                # libinput releases any button still held when a device goes away.
                fcntl.ioctl(self.clone_fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.clone_fd)
            self.clone_fd = -1
        if self.fd >= 0:
            # Closing the descriptor also releases the grab.
            os.close(self.fd)
            self.fd = -1
        self.grabbed = False
        self.down = set()


class Helper:
    def __init__(self, config, forced_devices=None, stdin_control=False):
        self.set_config(config)
        self.forced = list(forced_devices or [])
        self.stdin_control = stdin_control
        self.forced_kind = 'mouse'
        self.selector = selectors.DefaultSelector()
        self.mice = {}
        self.denied = {}
        self.backoff = {}
        self.running = True
        self.stdin_buffer = b''
        self.last_status = None
        self.next_scan = 0.0
        self.changed = True

    def set_config(self, config):
        self.config = config
        self.neutral = is_neutral(config)
        self.idle_kinds = {kind for kind in KINDS if kind_is_neutral(config, kind)}
        # What scrolling is multiplied by right now.
        self.applied = config if config['enabled'] else NEUTRAL_CONFIG

    # -- device bookkeeping

    def _candidates(self):
        """{event node: (name, kind)} of the devices to take care of."""
        if self.forced:
            return {path: (os.path.basename(path), self.forced_kind) for path in self.forced}
        return {path: (name, kind) for path, name, phys, kind in list_input_devices()
                if kind and phys != CLONE_PHYS}

    def scan(self):
        self.next_scan = time.monotonic() + RESCAN_INTERVAL
        self.changed = True
        candidates = self._candidates()
        for path in list(self.mice):
            if path not in candidates:
                self.release(path)
        self.denied = {}
        self.release_idle()
        now = time.monotonic()
        self.backoff = {path: entry for path, entry in self.backoff.items()
                        if path in candidates and entry[1] > now
                        and candidates[path][1] not in self.idle_kinds}
        for path, (name, kind) in candidates.items():
            if kind in self.idle_kinds:
                # Left untouched; only its permission is reported.
                if path not in self.mice and not os.access(path, os.R_OK):
                    self.denied[path] = name
                continue
            if path in self.mice or path in self.backoff:
                continue
            mouse = Device(path, name, kind)
            try:
                mouse.open()
            except PermissionError:
                mouse.close()
                self.denied[path] = name
                continue
            except OSError as error:
                mouse.close()
                self.postpone(mouse, error)
                continue
            self.mice[path] = mouse
            self.selector.register(mouse.fd, selectors.EVENT_READ, mouse)

    def postpone(self, mouse, error):
        """Leaves a mouse that could not be taken alone for a while, instead of
        retrying (and logging, and flashing a clone) at every scan."""
        log('cannot use %s: %s' % (mouse.path, error))
        self.backoff[mouse.path] = (mouse.name, time.monotonic() + RETRY_DELAY,
                                    error.errno == errno.EBUSY)
        self.changed = True

    def release_idle(self):
        """Devices whose kind is back to the normal speed are given back, but
        not in the middle of a click, a drag or a touch: one with a button or
        a finger down keeps being forwarded, unscaled, until it is up."""
        for path, mouse in list(self.mice.items()):
            if mouse.kind in self.idle_kinds and not mouse.engaged():
                self.release(path)

    def release(self, path):
        mouse = self.mice.pop(path, None)
        if not mouse:
            return
        self.changed = True
        try:
            self.selector.unregister(mouse.fd)
        except (KeyError, ValueError):
            pass
        mouse.close()

    # -- control channel

    def read_stdin(self):
        try:
            data = os.read(0, 4096)
        except BlockingIOError:
            return
        if not data:
            self.running = False
            return
        self.stdin_buffer += data
        while b'\n' in self.stdin_buffer:
            line, self.stdin_buffer = self.stdin_buffer.split(b'\n', 1)
            if not line.strip():
                continue
            try:
                self.set_config(normalize_config(json.loads(line), self.config))
            except (ValueError, TypeError) as error:
                log('ignored control line: %s' % error)
                continue
            self.scan()

    def report(self):
        """Prints the status as a JSON line whenever it differs from the last one."""
        if not self.changed:
            return
        self.changed = False
        devices = [{'name': mouse.name, 'path': path, 'state': mouse.state, 'kind': mouse.kind}
                   for path, mouse in sorted(self.mice.items())]
        devices += [{'name': name, 'path': path, 'state': 'denied'}
                    for path, name in sorted(self.denied.items())]
        devices += [{'name': name, 'path': path, 'state': 'busy'}
                    for path, (name, _retry, busy) in sorted(self.backoff.items()) if busy]
        status = {
            'uinput': uinput_state(),
            'neutral': self.neutral,
            'config': self.config,
            'devices': devices,
        }
        if status != self.last_status:
            self.last_status = status
            line = json.dumps(status)
            try:
                sys.stdout.write(line + '\n')
                sys.stdout.flush()
            except OSError:
                pass  # nobody is reading; never let that stall the mouse
            if DEBUG:
                log(line)

    # -- main loop

    def run(self):
        # A status reader that stops reading must not block the event loop.
        if stat.S_ISFIFO(os.fstat(1).st_mode):
            os.set_blocking(1, False)
        if self.stdin_control:
            os.set_blocking(0, False)
            self.selector.register(0, selectors.EVENT_READ, 'stdin')
        wake_read, wake_write = os.pipe()
        os.set_blocking(wake_read, False)
        os.set_blocking(wake_write, False)
        signal.set_wakeup_fd(wake_write)
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, lambda *_: setattr(self, 'running', False))
        self.selector.register(wake_read, selectors.EVENT_READ, 'wake')
        try:
            while self.running:
                now = time.monotonic()
                if now >= self.next_scan:
                    self.scan()
                pending = [m for m in self.mice.values() if not m.grabbed]
                for mouse in pending:
                    try:
                        self.changed |= mouse.try_grab()
                    except OSError as error:
                        self.release(mouse.path)
                        self.postpone(mouse, error)
                self.release_idle()
                self.report()
                timeout = 0.05 if any(not m.grabbed for m in self.mice.values()) \
                    else max(0.0, self.next_scan - time.monotonic())
                for key, _ in self.selector.select(timeout):
                    if key.data == 'stdin':
                        self.read_stdin()
                    elif key.data == 'wake':
                        try:
                            os.read(wake_read, 64)
                        except BlockingIOError:
                            pass
                    elif self.mice.get(key.data.path) is key.data:
                        self.pump(key.data)
        finally:
            for path in list(self.mice):
                self.release(path)

    def pump(self, mouse):
        try:
            mouse.forward(self.applied)
        except BlockingIOError:
            pass
        except OSError as error:
            if error.errno != errno.ENODEV:
                log('lost %s: %s' % (mouse.path, error))
            self.release(mouse.path)


def log(message):
    sys.stderr.write('scroll-speed: %s\n' % message)
    sys.stderr.flush()


def take_instance_lock():
    """Two helpers would fight over the same devices; returns the lock's file
    descriptor (to keep open), or None when another helper already runs."""
    directory = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    fd = os.open(os.path.join(directory, 'scroll-speed-%d.lock' % os.getuid()),
                 os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def probe():
    """One-shot report used by the preferences window."""
    devices = []
    clones = set()
    for path, name, phys, kind in list_input_devices():
        if phys == CLONE_PHYS:
            clones.add(name)
        elif kind:
            devices.append({'name': name, 'path': path, 'kind': kind,
                            'access': os.access(path, os.R_OK)})
    for device in devices:
        device['active'] = device['name'] in clones
    return {'uinput': uinput_state(), 'devices': devices}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--vertical', type=float, default=1.0,
                        help='vertical wheel factor (1.0 leaves it unchanged)')
    parser.add_argument('--horizontal', type=float, default=None,
                        help='horizontal wheel factor (defaults to --vertical)')
    parser.add_argument('--acceleration', type=float, default=0.0,
                        help='0..1, extra speed when the wheel spins fast')
    parser.add_argument('--touchpad', type=float, default=1.0,
                        help='two-finger scrolling factor of touchpads')
    parser.add_argument('--pointingstick', type=float, default=1.0,
                        help='middle-button scrolling factor of pointing sticks')
    parser.add_argument('--device', action='append', default=[],
                        help='use this event node instead of scanning for devices')
    parser.add_argument('--kind', choices=KINDS, default='mouse',
                        help='what the --device nodes are (default: mouse)')
    parser.add_argument('--stdin-control', action='store_true',
                        help='read JSON configuration lines on stdin, exit on EOF')
    parser.add_argument('--probe', action='store_true',
                        help='print the detected devices and permissions, then exit')
    args = parser.parse_args(argv)

    if args.probe:
        print(json.dumps(probe()))
        return 0

    lock = take_instance_lock()
    if lock is None:
        log('another helper is already running (is the Scroll Speed extension enabled?)')
        return 3

    config = normalize_config({
        'vertical': args.vertical,
        'horizontal': args.vertical if args.horizontal is None else args.horizontal,
        'acceleration': args.acceleration,
        'touchpad': args.touchpad,
        'pointingstick': args.pointingstick,
    })
    helper = Helper(config, args.device, args.stdin_control)
    helper.forced_kind = args.kind
    helper.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())

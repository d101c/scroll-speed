#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Scroll Speed helper: rescales mouse wheel events below the compositor.

A Wayland compositor delivers wheel events to applications before any shell
extension can see them, so the speed has to be changed where the events come
from. This helper grabs each wheel mouse (EVIOCGRAB), mirrors it through a
uinput clone and forwards every event untouched except the wheel axes, which
are multiplied by the configured factor. The clone always speaks the
high-resolution wheel protocol (120 units per detent), so fractional factors
reach applications as fractional scroll steps.

Standard library only. Needs read access to the mouse event nodes and write
access to /dev/uinput (see the udev rule installed from the preferences).
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

BTN_LEFT = 0x110
INPUT_PROP_POINTING_STICK = 0x05

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


def normalize_config(raw, base=None):
    config = dict(base or {'enabled': True, 'vertical': 1.0, 'horizontal': 1.0,
                           'acceleration': 0.0})
    if 'enabled' in raw:
        config['enabled'] = bool(raw['enabled'])
    for key in ('vertical', 'horizontal'):
        if key in raw:
            config[key] = clamp(float(raw[key]), FACTOR_MIN, FACTOR_MAX)
    if 'acceleration' in raw:
        config['acceleration'] = clamp(float(raw['acceleration']), 0.0, 1.0)
    return config


NEUTRAL_CONFIG = normalize_config({})


def is_neutral(config):
    """True when the configuration would leave wheel events unchanged."""
    return (not config['enabled'] or
            (config['vertical'] == 1.0 and config['horizontal'] == 1.0 and
             config['acceleration'] == 0.0))


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


def _ui_get_sysname(length):
    return _ioc(_IOC_READ, 'U', 44, length)


def _read_bits(fd, request_for_length, max_code):
    length = max_code // 8 + 1
    buffer = bytearray(length)
    fcntl.ioctl(fd, request_for_length(length), buffer)
    return [code for code in range(max_code + 1) if buffer[code // 8] >> (code % 8) & 1]


def keys_down(fd):
    return _read_bits(fd, _eviocgkey, KEY_MAX)


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
    """Yields (event node path, name, phys, is wheel mouse) from sysfs."""
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
        props = parse_bitmask(_read_sysfs(base + 'properties') or '0')
        yield ('/dev/input/' + entry, _read_sysfs(base + 'name'),
               _read_sysfs(base + 'phys'), is_wheel_mouse(rel, key, props))


def uinput_state():
    if not os.path.exists('/dev/uinput'):
        return 'missing'
    return 'ok' if os.access('/dev/uinput', os.W_OK) else 'denied'


# --------------------------------------------------------------------------
# Runtime

# How long a fresh clone is left alone so the compositor can open it.
CLONE_SETTLE = 0.3
RESCAN_INTERVAL = 2.0
# A mouse that cannot be taken (grabbed by an input remapper, clone creation
# failing…) is left alone for this long before the next attempt.
RETRY_DELAY = 30.0


class Mouse:
    """One grabbed (or about to be grabbed) source device and its clone."""

    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.fd = -1
        self.clone_fd = -1
        self.state = 'waiting'
        self.grabbed = False
        self.ready_at = 0.0
        self.down = set()
        self.transformer = FrameTransformer()
        self.skip_frame = False

    def open(self):
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        description = clone_description(describe_source(self.fd))
        self.name = description['name'] or self.name
        self.clone_fd = create_uinput_device(description, CLONE_PHYS)
        self.ready_at = time.monotonic() + CLONE_SETTLE

    def drain(self):
        try:
            while os.read(self.fd, EVENT.size * 64):
                pass
        except BlockingIOError:
            pass

    def try_grab(self):
        """Takes the device once the clone has settled and no button is held,
        so that the compositor never sees a press without its release."""
        if time.monotonic() < self.ready_at or keys_down(self.fd):
            return False
        fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        # What is queued predates the grab and already reached the compositor.
        # A press landing after this drain stays queued and is forwarded.
        self.drain()
        if keys_down(self.fd):
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
            return False
        self.grabbed = True
        self.state = 'active'
        self.transformer.reset()
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
                    out.extend(self._resync_keys())
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

    def _resync_keys(self):
        """After the kernel dropped events, realigns the clone's buttons."""
        actual = set(keys_down(self.fd))
        events = [(EV_KEY, code, 0) for code in self.down - actual]
        events += [(EV_KEY, code, 1) for code in actual - self.down]
        self.down = actual
        if events:
            events.append((EV_SYN, SYN_REPORT, 0))
        return events

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
        # What the wheel is multiplied by right now.
        self.applied = NEUTRAL_CONFIG if self.neutral else config

    # -- device bookkeeping

    def _candidates(self):
        if self.forced:
            return [(path, os.path.basename(path)) for path in self.forced]
        return [(path, name) for path, name, phys, wheel in list_input_devices()
                if wheel and phys != CLONE_PHYS]

    def scan(self):
        self.next_scan = time.monotonic() + RESCAN_INTERVAL
        self.changed = True
        candidates = dict(self._candidates())
        for path in list(self.mice):
            if path not in candidates:
                self.release(path)
        self.denied = {}
        if self.neutral:
            self.backoff = {}
            self.release_idle()
            for path, name in candidates.items():
                if not os.access(path, os.R_OK):
                    self.denied[path] = name
            return
        now = time.monotonic()
        self.backoff = {path: entry for path, entry in self.backoff.items()
                        if path in candidates and entry[1] > now}
        for path, name in candidates.items():
            if path in self.mice or path in self.backoff:
                continue
            mouse = Mouse(path, name)
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
        """With a neutral configuration the mice are given back, but not in the
        middle of a click or drag: a mouse with a button held keeps being
        forwarded, unscaled, until the button is up."""
        for path, mouse in list(self.mice.items()):
            if not mouse.down:
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
        devices = [{'name': mouse.name, 'path': path, 'state': mouse.state}
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
                if self.neutral:
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
    """Two helpers would fight over the same mice; returns the lock's file
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
    for path, name, phys, wheel in list_input_devices():
        if phys == CLONE_PHYS:
            clones.add(name)
        elif wheel:
            devices.append({'name': name, 'path': path,
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
    parser.add_argument('--device', action='append', default=[],
                        help='use this event node instead of scanning for mice')
    parser.add_argument('--stdin-control', action='store_true',
                        help='read JSON configuration lines on stdin, exit on EOF')
    parser.add_argument('--probe', action='store_true',
                        help='print the detected mice and permissions, then exit')
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
    })
    Helper(config, args.device, args.stdin_control).run()
    return 0


if __name__ == '__main__':
    sys.exit(main())

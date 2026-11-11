# SPDX-License-Identifier: GPL-2.0-or-later
"""End-to-end test of the helper against real kernel devices.

The fake source is a uinput device shaped like a joystick with a wheel: udev
grants the session user access to joysticks out of the box, and libinput
ignores them, so nothing here reaches the desktop. Skipped when /dev/uinput is
not writable.

    python3 -m unittest -v tests/test_integration.py
"""

import errno
import fcntl
import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest

HELPER_DIR = os.path.join(os.path.dirname(__file__), '..', 'extension', 'helper')
sys.path.insert(0, HELPER_DIR)

import scroll_speed_helper as h  # noqa: E402

HELPER = os.path.join(HELPER_DIR, 'scroll_speed_helper.py')
SOURCE_NAME = 'Scroll Speed Test Source'
BTN_TRIGGER = 0x120
ABS_X = 0x00
ABS_RANGE = struct.pack('iiiiii', 0, -32768, 32767, 0, 0, 0)
SYN = (h.EV_SYN, h.SYN_REPORT, 0)


def wait_for(predicate, timeout=5.0, what='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('timed out waiting for ' + what)


def find_clone():
    for path, name, phys, _wheel in h.list_input_devices():
        if phys == h.CLONE_PHYS and name == SOURCE_NAME:
            return path
    return None


def detent(direction=1):
    return [(h.EV_REL, h.REL_WHEEL, direction),
            (h.EV_REL, h.REL_WHEEL_HI_RES, direction * 120), SYN]


@unittest.skipUnless(h.uinput_state() == 'ok', '/dev/uinput is not writable')
class HelperIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.source = h.create_uinput_device({
            'name': SOURCE_NAME, 'id': (0x06, 0x1234, 0x5678, 1),
            'keys': [BTN_TRIGGER, BTN_TRIGGER + 1],
            'rel': [h.REL_HWHEEL, h.REL_WHEEL, h.REL_WHEEL_HI_RES, h.REL_HWHEEL_HI_RES],
            'abs': {ABS_X: ABS_RANGE, 1: ABS_RANGE}, 'msc': [4], 'props': [],
        }, 'scroll-speed/test-source')
        self.addCleanup(self._close_source)
        self.node = wait_for(lambda: h.uinput_event_node(self.source), what='source node')
        wait_for(lambda: os.access(self.node, os.R_OK), what='source ACL')
        self.helper = None
        self.clone = -1
        self.status = None
        # Its own lock directory, so a helper running for the real session
        # (started by the extension) does not make these tests fail.
        lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(lock_dir.cleanup)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=lock_dir.name)

    def _close_source(self):
        if self.source >= 0:
            os.close(self.source)
            self.source = -1

    def tearDown(self):
        if self.clone >= 0:
            os.close(self.clone)
        if self.helper and self.helper.poll() is None:
            self.helper.kill()
            self.helper.wait()
        if self.helper:
            self.helper.stdin.close()
            self.helper.stdout.close()

    # -- plumbing

    def start(self, *args):
        self.helper = subprocess.Popen(
            [sys.executable, HELPER, '--device', self.node, '--stdin-control', *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)
        os.set_blocking(self.helper.stdout.fileno(), False)

    def configure(self, **config):
        self.helper.stdin.write((json.dumps(config) + '\n').encode())
        self.helper.stdin.flush()

    def wait_status(self, predicate, what):
        def poll():
            line = self.helper.stdout.readline()
            if line:
                self.status = json.loads(line)
            return self.status is not None and predicate(self.status)
        wait_for(poll, what=what)
        return self.status

    def wait_active(self):
        self.wait_status(lambda s: [d for d in s['devices'] if d['state'] == 'active'],
                         'an active device')
        if self.clone >= 0:
            os.close(self.clone)
        path = wait_for(find_clone, what='clone node')
        wait_for(lambda: os.access(path, os.R_OK), what='clone ACL')
        self.clone = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def emit(self, events):
        os.write(self.source, h.pack_events(events))

    def read_clone(self, quiet=0.25):
        """Collects (type, code, value) from the clone until it stays quiet."""
        events = []
        while select.select([self.clone], [], [], quiet)[0]:
            data = os.read(self.clone, h.EVENT.size * 64)
            events += [event[2:] for event in h.EVENT.iter_unpack(data)]
        return events

    def source_is_grabbed(self):
        fd = os.open(self.node, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, h.EVIOCGRAB, 1)
            fcntl.ioctl(fd, h.EVIOCGRAB, 0)
            return False
        except OSError as error:
            self.assertEqual(error.errno, errno.EBUSY)
            return True
        finally:
            os.close(fd)

    # -- scenarios

    def test_scales_wheel_and_forwards_everything_else(self):
        self.start('--vertical', '0.5')
        self.wait_active()
        self.assertTrue(self.source_is_grabbed())

        self.emit(detent())
        self.assertEqual(self.read_clone(), [(h.EV_REL, h.REL_WHEEL_HI_RES, 60), SYN])
        self.emit(detent())
        self.assertEqual(self.read_clone(), [
            (h.EV_REL, h.REL_WHEEL_HI_RES, 60), (h.EV_REL, h.REL_WHEEL, 1), SYN])

        self.emit([(h.EV_MSC, 4, 0x90001), (h.EV_KEY, BTN_TRIGGER, 1), SYN])
        self.emit([(h.EV_ABS, ABS_X, 1234), SYN])
        self.emit([(h.EV_MSC, 4, 0x90001), (h.EV_KEY, BTN_TRIGGER, 0), SYN])
        self.assertEqual(self.read_clone(), [
            (h.EV_MSC, 4, 0x90001), (h.EV_KEY, BTN_TRIGGER, 1), SYN,
            (h.EV_ABS, ABS_X, 1234), SYN,
            (h.EV_MSC, 4, 0x90001), (h.EV_KEY, BTN_TRIGGER, 0), SYN])

    def test_horizontal_axis_has_its_own_factor(self):
        self.start('--vertical', '0.5', '--horizontal', '3')
        self.wait_active()
        self.emit([(h.EV_REL, h.REL_HWHEEL, -1), (h.EV_REL, h.REL_HWHEEL_HI_RES, -120), SYN])
        self.assertEqual(self.read_clone(), [
            (h.EV_REL, h.REL_HWHEEL_HI_RES, -360), (h.EV_REL, h.REL_HWHEEL, -3), SYN])

    def test_configuration_changes_apply_live(self):
        self.start('--vertical', '0.5')
        self.wait_active()
        self.configure(vertical=2.0, horizontal=2.0)
        self.wait_status(lambda s: s['config']['vertical'] == 2.0, 'the new factor')
        self.emit(detent(-1))
        self.assertEqual(self.read_clone(), [
            (h.EV_REL, h.REL_WHEEL_HI_RES, -240), (h.EV_REL, h.REL_WHEEL, -2), SYN])

    def test_neutral_configuration_gives_the_mouse_back(self):
        self.start('--vertical', '0.5')
        self.wait_active()
        self.configure(vertical=1.0, horizontal=1.0)
        self.wait_status(lambda s: s['neutral'] and not s['devices'], 'a neutral status')
        wait_for(lambda: find_clone() is None, what='the clone to disappear')
        self.assertFalse(self.source_is_grabbed())

        self.configure(enabled=True, vertical=0.25)
        self.wait_active()
        self.assertTrue(self.source_is_grabbed())
        self.emit(detent())
        self.assertEqual(self.read_clone(), [(h.EV_REL, h.REL_WHEEL_HI_RES, 30), SYN])

        self.configure(enabled=False)
        self.wait_status(lambda s: s['neutral'] and not s['devices'], 'a disabled status')
        self.assertFalse(self.source_is_grabbed())

    def test_does_not_grab_while_a_button_is_held(self):
        self.emit([(h.EV_KEY, BTN_TRIGGER, 1), SYN])
        self.start('--vertical', '0.5')
        self.wait_status(lambda s: [d for d in s['devices'] if d['state'] == 'waiting'],
                         'a waiting device')
        time.sleep(0.6)  # well past the clone settle delay
        self.assertFalse(self.source_is_grabbed())
        self.emit([(h.EV_KEY, BTN_TRIGGER, 0), SYN])
        self.wait_active()
        self.assertTrue(self.source_is_grabbed())
        # Nothing from before the grab leaks into the clone.
        self.assertEqual(self.read_clone(), [])

    def test_exits_cleanly_when_stdin_closes(self):
        self.start('--vertical', '0.5')
        self.wait_active()
        self.helper.stdin.close()
        self.assertEqual(self.helper.wait(timeout=5), 0)
        wait_for(lambda: find_clone() is None, what='the clone to disappear')
        self.assertFalse(self.source_is_grabbed())
        self.helper.stdin = open(os.devnull, 'wb')  # for tearDown

    def test_exits_cleanly_on_sigterm(self):
        self.start('--vertical', '0.5')
        self.wait_active()
        self.helper.terminate()
        self.assertEqual(self.helper.wait(timeout=5), 0)
        wait_for(lambda: find_clone() is None, what='the clone to disappear')
        self.assertFalse(self.source_is_grabbed())

    def test_survives_the_mouse_being_unplugged(self):
        self.start('--vertical', '0.5')
        self.wait_active()
        self._close_source()
        self.wait_status(lambda s: not s['devices'], 'the device to be dropped')
        wait_for(lambda: find_clone() is None, what='the clone to disappear')
        self.assertIsNone(self.helper.poll())

    def test_second_instance_refuses_to_start(self):
        self.start('--vertical', '0.5')
        self.wait_active()
        second = subprocess.run([sys.executable, HELPER, '--device', self.node],
                                capture_output=True, text=True, timeout=10, env=self.env)
        self.assertEqual(second.returncode, 3)
        self.assertIn('already running', second.stderr)
        self.emit(detent())  # the first one is unaffected
        self.assertEqual(self.read_clone(), [(h.EV_REL, h.REL_WHEEL_HI_RES, 60), SYN])

    def test_mouse_grabbed_by_another_program_is_left_alone(self):
        other = os.open(self.node, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, other)
        fcntl.ioctl(other, h.EVIOCGRAB, 1)
        self.start('--vertical', '0.5')
        self.wait_status(lambda s: [d for d in s['devices'] if d['state'] == 'busy'],
                         'a busy device')
        time.sleep(2.5)  # more than one rescan: no clone may come and go
        self.assertIsNone(find_clone())
        self.assertIsNone(self.helper.poll())

    def test_probe_reports_permissions(self):
        result = subprocess.run([sys.executable, HELPER, '--probe'],
                                capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report['uinput'], 'ok')
        self.assertIsInstance(report['devices'], list)


if __name__ == '__main__':
    unittest.main()

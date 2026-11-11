# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for touchpad and pointing stick scrolling: python3 -m unittest -v"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'extension', 'helper'))

import scroll_speed_helper as h  # noqa: E402

SYN = (h.EV_SYN, h.SYN_REPORT, 0)
MSC_TIMESTAMP = 5
RES = 12            # units per millimetre
WIDTH, HEIGHT = 1200, 800
FRAME = 0.007       # a 140 Hz touchpad


def absinfo(value, low, high, res=0):
    return struct.pack('iiiiii', value, low, high, 0, 0, res)


def description(res=RES, slots=5, single_touch=True):
    axes = {
        h.ABS_MT_SLOT: absinfo(0, 0, slots - 1),
        h.ABS_MT_POSITION_X: absinfo(0, 0, WIDTH, res),
        h.ABS_MT_POSITION_Y: absinfo(0, 0, HEIGHT, res),
        h.ABS_MT_TRACKING_ID: absinfo(0, 0, 65535),
    }
    if single_touch:
        axes[h.ABS_X] = absinfo(0, 0, WIDTH, res)
        axes[h.ABS_Y] = absinfo(0, 0, HEIGHT, res)
    return {'abs': axes}


def config(touchpad=1.0, pointingstick=1.0):
    return h.normalize_config({'touchpad': touchpad, 'pointingstick': pointingstick})


class Pad:
    """Drives a TouchpadTransformer the way the kernel would (unchanged values
    are not repeated, the slot is only named when it changes) and replays the
    output the way the clone's kernel side would."""

    def __init__(self, factor=1.0, **kwargs):
        self.transformer = h.TouchpadTransformer()
        self.transformer.configure(description(**kwargs))
        self.config = config(touchpad=factor)
        self.now = 100.0
        self.pending = []
        self.source_slot = 0
        self.source = {}        # slot -> [x, y] as last reported by the device
        self.next_id = 0
        # What the clone shows.
        self.clone_slot = 0
        self.ids = {}
        self.shown = {}
        self.pointer = [0, 0]
        self.keys = set()
        self.frames = []

    def _slot(self, slot):
        if slot != self.source_slot:
            self.source_slot = slot
            self.pending.append((h.EV_ABS, h.ABS_MT_SLOT, slot))

    def _position(self, slot, x, y):
        stored = self.source.setdefault(slot, [0, 0])
        for axis, code, value in ((0, h.ABS_MT_POSITION_X, x), (1, h.ABS_MT_POSITION_Y, y)):
            if stored[axis] != value:
                stored[axis] = value
                self.pending.append((h.EV_ABS, code, value))

    def down(self, slot, x, y):
        self._slot(slot)
        self.next_id += 1
        self.pending.append((h.EV_ABS, h.ABS_MT_TRACKING_ID, self.next_id))
        self._position(slot, x, y)

    def move(self, slot, x, y):
        self._slot(slot)
        self._position(slot, x, y)

    def shift(self, slot, dx, dy):
        x, y = self.source[slot]
        self.move(slot, x + dx, y + dy)

    def up(self, slot):
        self._slot(slot)
        self.pending.append((h.EV_ABS, h.ABS_MT_TRACKING_ID, -1))

    def key(self, code, value):
        self.pending.append((h.EV_KEY, code, value))

    def sync(self, dt=FRAME):
        self.now += dt
        frame = None
        events = self.pending + [(h.EV_MSC, MSC_TIMESTAMP, int(self.now * 1e6) % 2 ** 31), SYN]
        self.pending = []
        for event in events:
            frame = self.transformer.feed(*event, self.now, self.config)
        self.replay(frame)
        return frame

    def replay(self, frame):
        self.frames.append(frame)
        for etype, code, value in frame:
            if etype == h.EV_KEY:
                (self.keys.add if value else self.keys.discard)(code)
            elif etype != h.EV_ABS:
                continue
            elif code == h.ABS_MT_SLOT:
                self.clone_slot = value
            elif code == h.ABS_MT_TRACKING_ID:
                self.ids[self.clone_slot] = value
            elif code == h.ABS_MT_POSITION_X:
                self.shown.setdefault(self.clone_slot, [0, 0])[0] = value
            elif code == h.ABS_MT_POSITION_Y:
                self.shown.setdefault(self.clone_slot, [0, 0])[1] = value
            elif code == h.ABS_X:
                self.pointer[0] = value
            elif code == h.ABS_Y:
                self.pointer[1] = value

    def two_fingers(self, y=100):
        """Two fingers side by side, already past the distance libinput needs
        to recognise a scroll (30 units = 2.5 mm, moved unscaled)."""
        self.down(0, 400, y)
        self.sync()
        self.down(1, 600, y)
        self.sync()
        for _ in range(3):
            self.shift(0, 0, 10)
            self.shift(1, 0, 10)
            self.sync()

    def scroll(self, frames, dy=10, dx=0):
        for _ in range(frames):
            self.shift(0, dx, dy)
            self.shift(1, dx, dy)
            self.sync()


class TouchpadTransformerTest(unittest.TestCase):
    def test_one_finger_is_forwarded_unchanged(self):
        pad = Pad(0.3)
        pad.down(0, 300, 200)
        pad.sync()
        for step in range(1, 40):
            pad.move(0, 300 + 7 * step, 200 + 3 * step)
            pad.sync()
            self.assertEqual(pad.shown[0], pad.source[0])
            self.assertEqual(pad.pointer, pad.source[0])
        pad.up(0)
        pad.sync()
        self.assertEqual(pad.ids[0], -1)

    def test_the_start_of_a_scroll_is_not_rescaled(self):
        # libinput decides between tap, scroll and pinch during the first
        # millimetres; that must look the same whatever the factor.
        for factor in (0.2, 5.0):
            pad = Pad(factor)
            pad.down(0, 400, 100)
            pad.sync()
            pad.down(1, 600, 100)
            pad.sync()
            pad.shift(0, 0, 10)
            pad.shift(1, 0, 10)
            pad.sync()
            pad.shift(0, 0, 10)
            pad.shift(1, 0, 10)
            pad.sync()
            self.assertEqual(pad.shown, {0: [400, 120], 1: [600, 120]})

    def test_half_speed_scroll(self):
        pad = Pad(0.5)
        pad.two_fingers()
        pad.scroll(40)
        self.assertEqual(pad.source[0], [400, 530])
        self.assertEqual(pad.shown, {0: [400, 330], 1: [600, 330]})  # 130 + 400 / 2

    def test_double_speed_scroll(self):
        pad = Pad(2.0)
        pad.two_fingers()
        pad.scroll(20)
        self.assertEqual(pad.shown, {0: [400, 530], 1: [600, 530]})  # 130 + 200 * 2

    def test_horizontal_scroll(self):
        pad = Pad(0.5)
        pad.down(0, 400, 300)
        pad.sync()
        pad.down(1, 400, 500)
        pad.sync()
        pad.scroll(3, dy=0, dx=10)
        pad.scroll(20, dy=0, dx=10)
        self.assertEqual(pad.shown, {0: [530, 300], 1: [530, 500]})

    def test_fractions_are_not_lost(self):
        pad = Pad(0.3)
        pad.two_fingers()
        pad.scroll(100, dy=1)
        self.assertEqual(pad.shown[0][1], 130 + 30)

    def test_factor_one_changes_nothing(self):
        pad = Pad(1.0)
        pad.two_fingers()
        pad.scroll(30)
        self.assertEqual(pad.shown, pad.source)

    def test_fingers_reporting_in_alternate_frames_still_scroll(self):
        # A slow scroll: each finger moves less than one unit per frame, and
        # the two reports rarely land in the same frame.
        pad = Pad(0.5)
        pad.two_fingers()
        for step in range(200):
            pad.shift(step % 2, 0, 1)
            pad.sync()
        self.assertEqual(pad.source[0][1], 230)
        self.assertAlmostEqual(pad.shown[0][1], 130 + 50, delta=1)
        self.assertAlmostEqual(pad.shown[1][1], 130 + 50, delta=1)

    def test_pinch_is_left_alone(self):
        pad = Pad(0.3)
        pad.two_fingers(y=300)
        for _ in range(30):
            pad.shift(0, -6, -4)
            pad.shift(1, 6, 4)
            pad.sync()
        self.assertEqual(pad.shown, pad.source)

    def test_distance_between_the_fingers_survives_a_scroll(self):
        pad = Pad(0.4)
        pad.two_fingers()
        for step in range(60):
            pad.shift(0, 1, 9)
            pad.shift(1, -1 if step % 3 else 0, 11)
            pad.sync()
        for axis in (0, 1):
            self.assertEqual(pad.shown[1][axis] - pad.shown[0][axis],
                             pad.source[1][axis] - pad.source[0][axis])

    def test_finger_moving_next_to_a_resting_thumb_is_left_alone(self):
        pad = Pad(0.3)
        pad.down(0, 400, 700)       # the thumb
        pad.sync()
        pad.down(1, 600, 100)
        pad.sync()
        for _ in range(60):
            pad.shift(1, 4, 9)
            pad.sync()
        self.assertEqual(pad.shown, pad.source)

    def test_twitching_thumb_barely_changes_the_pointer(self):
        pad = Pad(0.3)
        pad.down(0, 400, 700)
        pad.sync()
        pad.down(1, 600, 100)
        pad.sync()
        for step in range(60):
            pad.shift(1, 0, 9)
            if step % 10 == 0:
                pad.shift(0, 0, 1)
            pad.sync()
        travelled = pad.shown[1][1] - 100
        self.assertGreater(travelled, 540 * 0.95)

    def test_click_and_drag_with_two_fingers_is_left_alone(self):
        pad = Pad(0.3)
        pad.two_fingers()
        pad.key(h.BTN_LEFT, 1)
        pad.sync()
        before = [list(pad.shown[0]), list(pad.shown[1])]
        pad.scroll(30)
        self.assertEqual(pad.shown[0], [before[0][0], before[0][1] + 300])
        self.assertEqual(pad.shown[1], [before[1][0], before[1][1] + 300])
        self.assertIn(h.BTN_LEFT, pad.keys)

    def test_three_fingers_are_left_alone(self):
        pad = Pad(0.3)
        pad.two_fingers()
        pad.down(2, 800, 130)
        pad.sync()
        for _ in range(20):
            for slot in range(3):
                pad.shift(slot, 0, 10)
            pad.sync()
        self.assertEqual(pad.shown, pad.source)

    def test_no_jump_when_a_finger_lifts_after_a_scroll(self):
        pad = Pad(0.5)
        pad.two_fingers()
        pad.scroll(40)
        shown = list(pad.shown[0])
        pad.up(1)
        pad.sync()
        self.assertEqual(pad.shown[0], shown)
        # The remaining finger moves the pointer at the normal speed.
        for _ in range(10):
            pad.shift(0, 5, -5)
            pad.sync()
        self.assertEqual(pad.shown[0], [shown[0] + 50, shown[1] - 50])
        self.assertEqual(pad.pointer, pad.shown[0])

    def test_new_touch_starts_where_the_finger_is(self):
        pad = Pad(0.5)
        pad.two_fingers()
        pad.scroll(40)
        pad.up(0)
        pad.up(1)
        pad.sync()
        self.assertEqual((pad.ids[0], pad.ids[1]), (-1, -1))
        # Same slot, and the very position the previous finger left: the
        # kernel does not repeat it, the clone still has to be told.
        pad.down(0, 400, 530)
        pad.sync()
        self.assertEqual(pad.shown[0], [400, 530])
        self.assertEqual(pad.pointer, [400, 530])

    def test_positions_stay_inside_the_touchpad(self):
        pad = Pad(3.0)
        pad.two_fingers(y=500)
        pad.scroll(25)       # 250 units real, 750 wanted from y = 530
        self.assertEqual(pad.shown[0][1], HEIGHT)
        self.assertEqual(pad.shown[1][1], HEIGHT)
        for frame in pad.frames:
            for etype, code, value in frame:
                if etype == h.EV_ABS and code == h.ABS_MT_POSITION_Y:
                    self.assertTrue(0 <= value <= HEIGHT)

    def test_amplified_flick_stays_below_libinput_jump_detection(self):
        pad = Pad(5.0)
        pad.two_fingers(y=10)
        previous = {0: pad.shown[0][1], 1: pad.shown[1][1]}
        last_step = {0: 10 / RES * 12 / 7, 1: 10 / RES * 12 / 7}
        for speed in (10, 30, 60, 60, 60, 40, 20):   # units per 7 ms frame
            pad.scroll(1, dy=speed)
            for slot in (0, 1):
                step = (pad.shown[slot][1] - previous[slot]) / RES * 12 / 7
                # Never amplified past what libinput accepts as a first step.
                self.assertLessEqual(step, max(7.0, speed / RES * 12 / 7 + 0.2))
                previous[slot] = pad.shown[slot][1]
                last_step[slot] = step
        # Still faster than the fingers.
        self.assertGreater(pad.shown[0][1] - 40, pad.source[0][1] - 40)

    def test_moderate_scroll_gets_the_full_factor(self):
        pad = Pad(3.0)
        pad.two_fingers(y=10)
        pad.scroll(15, dy=8)        # 95 mm/s, tripled: well below the cap
        self.assertEqual(pad.shown[0][1], 40 + 15 * 8 * 3)

    def test_slot_events_keep_the_clone_consistent(self):
        pad = Pad(0.5)
        pad.two_fingers()
        pad.scroll(10)
        # The fingers report one after the other. Finger 1 says nothing in
        # the second frame, yet it has to move on the clone as well.
        pad.shift(1, 0, 10)
        pad.sync()
        pad.shift(0, 0, 10)
        frame = pad.sync()
        slots = [value for etype, code, value in frame
                 if etype == h.EV_ABS and code == h.ABS_MT_SLOT]
        self.assertIn(1, slots)
        self.assertEqual(frame[-1], SYN)
        # The device goes on with slot 0 without naming it again.
        pad.shift(0, 0, 10)
        pad.shift(1, 0, 10)
        pad.sync()
        self.assertEqual(pad.shown[1][1] - pad.shown[0][1], pad.source[1][1] - pad.source[0][1])

    def test_other_events_pass_through(self):
        pad = Pad(0.5)
        pad.down(0, 400, 100)
        pad.pending.append((h.EV_ABS, 0x37, 0))            # ABS_MT_TOOL_TYPE
        pad.key(0x14A, 1)                                  # BTN_TOUCH
        pad.key(h.BTN_TOOL_FINGER, 1)
        frame = pad.sync()
        self.assertIn((h.EV_ABS, 0x37, 0), frame)
        self.assertIn((h.EV_KEY, 0x14A, 1), frame)
        self.assertIn((h.EV_KEY, h.BTN_TOOL_FINGER, 1), frame)
        self.assertEqual([event for event in frame if event[0] == h.EV_MSC][0][1], MSC_TIMESTAMP)

    def test_touchpad_without_a_resolution(self):
        pad = Pad(0.5, res=0)
        pad.down(0, 400, 100)
        pad.sync()
        pad.down(1, 600, 100)
        pad.sync()
        pad.scroll(40)
        self.assertEqual(pad.shown, {0: [400, 300], 1: [600, 300]})

    def test_touchpad_without_single_touch_axes(self):
        pad = Pad(0.5, single_touch=False)
        pad.two_fingers()
        pad.scroll(10)
        for frame in pad.frames:
            self.assertFalse([e for e in frame if e[0] == h.EV_ABS and e[1] in (h.ABS_X, h.ABS_Y)])

    def test_resync_adopts_fingers_already_down(self):
        pad = Pad(0.5)
        events = pad.transformer.resync(
            {h.BTN_LEFT}, ([-1, 7, -1, -1, -1], [0, 640, 0, 0, 0], [0, 222, 0, 0, 0], 1))
        pad.replay(events)
        self.assertEqual(pad.ids, {1: 7})
        self.assertEqual(pad.shown[1], [640, 222])
        self.assertEqual(pad.transformer.pressed, {h.BTN_LEFT})
        self.assertEqual(events[-1], SYN)

    def test_resync_ends_fingers_that_left(self):
        pad = Pad(0.5)
        pad.two_fingers()
        events = pad.transformer.resync(
            set(), ([-1, pad.ids[1], -1, -1, -1], [0, 610, 0, 0, 0], [0, 140, 0, 0, 0], 1))
        pad.replay(events)
        self.assertEqual(pad.ids[0], -1)
        self.assertEqual(pad.shown[1], [610, 140])
        self.assertEqual(events.count(SYN), 2)

    def test_not_a_multi_touch_device(self):
        with self.assertRaises(OSError):
            h.TouchpadTransformer().configure({'abs': {h.ABS_X: absinfo(0, 0, 100)}})


def stick_frames(transformer, frames, cfg):
    out = []
    for index, frame in enumerate(frames):
        result = None
        for event in list(frame) + [SYN]:
            result = transformer.feed(*event, 100.0 + index * 0.01, cfg)
        out.append(result)
    return out


def motion(frames, axis):
    return sum(value for frame in frames for etype, code, value in frame
               if etype == h.EV_REL and code == axis)


class StickTransformerTest(unittest.TestCase):
    PRESS = [(h.EV_MSC, 4, 0x90003), (h.EV_KEY, h.BTN_MIDDLE, 1)]
    RELEASE = [(h.EV_MSC, 4, 0x90003), (h.EV_KEY, h.BTN_MIDDLE, 0)]

    def test_pointer_motion_is_untouched(self):
        frames = [[(h.EV_REL, h.REL_X, 3), (h.EV_REL, h.REL_Y, -2)],
                  [(h.EV_KEY, h.BTN_LEFT, 1)], [(h.EV_REL, h.REL_X, 1)]]
        out = stick_frames(h.StickTransformer(), frames, config(pointingstick=0.2))
        self.assertEqual(out, [frame + [SYN] for frame in frames])

    def test_motion_with_the_middle_button_held_is_scaled(self):
        frames = [self.PRESS] + [[(h.EV_REL, h.REL_Y, 3)] for _ in range(40)] + [self.RELEASE]
        out = stick_frames(h.StickTransformer(), frames, config(pointingstick=0.5))
        self.assertEqual(out[0], self.PRESS + [SYN])
        self.assertEqual(out[-1], self.RELEASE + [SYN])
        self.assertEqual(motion(out, h.REL_Y), 60)

    def test_faster(self):
        frames = [self.PRESS] + [[(h.EV_REL, h.REL_X, -2), (h.EV_REL, h.REL_Y, 1)] for _ in range(10)]
        out = stick_frames(h.StickTransformer(), frames, config(pointingstick=3.0))
        self.assertEqual((motion(out, h.REL_X), motion(out, h.REL_Y)), (-60, 30))

    def test_small_factor_never_rounds_everything_away(self):
        frames = [self.PRESS] + [[(h.EV_REL, h.REL_Y, 1)] for _ in range(100)]
        out = stick_frames(h.StickTransformer(), frames, config(pointingstick=0.1))
        self.assertEqual(motion(out, h.REL_Y), 10)
        self.assertEqual(out[1], [])        # a frame scaled to nothing is dropped

    def test_release_frame_and_what_follows_are_not_scaled(self):
        frames = [self.PRESS, [(h.EV_REL, h.REL_Y, 4)],
                  self.RELEASE + [(h.EV_REL, h.REL_Y, 4)], [(h.EV_REL, h.REL_Y, 4)]]
        out = stick_frames(h.StickTransformer(), frames, config(pointingstick=0.5))
        self.assertEqual([motion([frame], h.REL_Y) for frame in out], [0, 2, 4, 4])

    def test_resync_reads_the_button(self):
        stick = h.StickTransformer()
        stick.resync({h.BTN_MIDDLE})
        out = stick_frames(stick, [[(h.EV_REL, h.REL_Y, 10)]], config(pointingstick=0.5))
        self.assertEqual(motion(out, h.REL_Y), 5)


class KindTest(unittest.TestCase):
    # Capability bitmasks of a ThinkPad, as sysfs shows them.
    TOUCHPAD = dict(rel='0', key='e520 10000 0 0 0 0', abs='2e0800000000003', prop='5')
    STICK = dict(rel='3', key='70000 0 0 0 0', abs='0', prop='21')

    def kind(self, rel='0', key='0', abs='0', prop='0'):  # noqa: A002
        return h.device_kind(*(h.parse_bitmask(text) for text in (rel, key, abs, prop)))

    def test_real_devices(self):
        self.assertEqual(self.kind(**self.TOUCHPAD), 'touchpad')
        self.assertEqual(self.kind(**self.STICK), 'pointingstick')

    def test_wheel_mouse(self):
        self.assertEqual(self.kind(rel='903', key='1f0000 0 0 0 0'), 'mouse')
        self.assertIsNone(self.kind(rel='3', key='70000 0 0 0 0'))          # no wheel

    def test_touchscreen_and_semi_mt_are_left_alone(self):
        self.assertIsNone(self.kind(**dict(self.TOUCHPAD, prop='2')))
        self.assertIsNone(self.kind(**dict(self.TOUCHPAD, prop='d')))

    def test_keyboard_is_never_a_candidate(self):
        keyboard = 'e520 10000 0 0 0 fffffffe'
        self.assertIsNone(self.kind(**dict(self.TOUCHPAD, key=keyboard)))

    def test_neutral_per_kind(self):
        cfg = h.normalize_config({'touchpad': 0.5})
        self.assertFalse(h.kind_is_neutral(cfg, 'touchpad'))
        self.assertTrue(h.kind_is_neutral(cfg, 'mouse'))
        self.assertTrue(h.kind_is_neutral(cfg, 'pointingstick'))
        self.assertFalse(h.is_neutral(cfg))
        self.assertTrue(h.kind_is_neutral(dict(cfg, enabled=False), 'touchpad'))


if __name__ == '__main__':
    unittest.main()

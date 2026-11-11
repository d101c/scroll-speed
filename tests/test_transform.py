# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for the pure wheel-scaling logic: python3 -m unittest -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'extension', 'helper'))

import scroll_speed_helper as h  # noqa: E402

SYN = (h.EV_SYN, h.SYN_REPORT, 0)


def config(vertical=1.0, horizontal=1.0, acceleration=0.0):
    return {'enabled': True, 'vertical': vertical, 'horizontal': horizontal,
            'acceleration': acceleration}


def run_frames(transformer, frames, cfg, start=100.0, step=0.5):
    """Feeds frames (lists of events without the SYN) and returns the output frames."""
    out = []
    now = start
    for frame in frames:
        result = None
        for event in list(frame) + [SYN]:
            result = transformer.feed(*event, now, cfg)
        out.append(result)
        now += step
    return out


def detent(direction=1):
    return [(h.EV_REL, h.REL_WHEEL, direction),
            (h.EV_REL, h.REL_WHEEL_HI_RES, direction * 120)]


def wheel_total(frames, code):
    return sum(value for frame in frames for etype, c, value in frame
               if etype == h.EV_REL and c == code)


class AxisScalerTest(unittest.TestCase):
    def test_identity(self):
        axis = h.AxisScaler()
        self.assertEqual(axis.feed(120, 1.0, 1.0), (120, 1))
        self.assertEqual(axis.feed(-120, 2.0, 1.0), (-120, -1))

    def test_half_speed_emits_a_detent_every_other_click(self):
        axis = h.AxisScaler()
        results = [axis.feed(120, float(i), 0.5) for i in range(4)]
        self.assertEqual(results, [(60, 0), (60, 1), (60, 0), (60, 1)])

    def test_remainder_is_carried_so_nothing_is_lost(self):
        axis = h.AxisScaler()
        total = sum(axis.feed(15, i * 0.5, 0.3)[0] for i in range(80))
        self.assertEqual(total, 360)  # 80 * 15 * 0.3

    def test_small_factor_never_rounds_everything_away(self):
        axis = h.AxisScaler()
        total = sum(axis.feed(1, i * 0.5, 0.05)[0] for i in range(400))
        self.assertEqual(total, 20)

    def test_direction_change_drops_the_remainder(self):
        axis = h.AxisScaler()
        axis.feed(120, 1.0, 0.7)       # 84 emitted, nothing pending
        axis.feed(1, 1.5, 0.7)         # 0.7 pending
        hi_res, _ = axis.feed(-120, 2.0, 0.7)
        self.assertEqual(hi_res, -84)

    def test_double_speed(self):
        axis = h.AxisScaler()
        self.assertEqual(axis.feed(120, 1.0, 2.0), (240, 2))

    def test_acceleration_only_boosts_fast_spins(self):
        slow, fast = h.AxisScaler(), h.AxisScaler()
        slow_total = sum(slow.feed(120, i * 0.2, 1.0, 1.0)[0] for i in range(10))
        fast_total = sum(fast.feed(120, i * 0.01, 1.0, 1.0)[0] for i in range(10))
        self.assertEqual(slow_total, 1200)   # 5 detents/s: below the threshold
        self.assertGreater(fast_total, 1200 * 3)  # 100 detents/s: saturated

    def test_acceleration_never_slows_down(self):
        axis = h.AxisScaler()
        for i in range(50):
            hi_res, _ = axis.feed(120, i * 0.03, 1.0, 0.5)
            self.assertGreaterEqual(hi_res, 120)

    def test_first_event_of_a_gesture_is_not_boosted(self):
        axis = h.AxisScaler()
        axis.feed(120, 1.0, 1.0, 1.0)
        axis.feed(120, 1.005, 1.0, 1.0)
        self.assertEqual(axis.feed(120, 5.0, 1.0, 1.0)[0], 120)


class FrameTransformerTest(unittest.TestCase):
    def test_non_wheel_events_pass_through_unchanged(self):
        frames = [[(h.EV_REL, h.REL_X, 5), (h.EV_REL, h.REL_Y, -3)],
                  [(h.EV_MSC, 4, 0x90001), (h.EV_KEY, h.BTN_LEFT, 1)]]
        out = run_frames(h.FrameTransformer(), frames, config(0.3))
        self.assertEqual(out, [frame + [SYN] for frame in frames])

    def test_identity_keeps_wheel_frames(self):
        out = run_frames(h.FrameTransformer(), [detent(), detent(-1)], config())
        self.assertEqual(out, [
            [(h.EV_REL, h.REL_WHEEL_HI_RES, 120), (h.EV_REL, h.REL_WHEEL, 1), SYN],
            [(h.EV_REL, h.REL_WHEEL_HI_RES, -120), (h.EV_REL, h.REL_WHEEL, -1), SYN],
        ])

    def test_half_speed(self):
        out = run_frames(h.FrameTransformer(), [detent(), detent()], config(0.5))
        self.assertEqual(out, [
            [(h.EV_REL, h.REL_WHEEL_HI_RES, 60), SYN],
            [(h.EV_REL, h.REL_WHEEL_HI_RES, 60), (h.EV_REL, h.REL_WHEEL, 1), SYN],
        ])

    def test_low_resolution_only_mouse_gets_high_resolution_output(self):
        frames = [[(h.EV_REL, h.REL_WHEEL, 1)], [(h.EV_REL, h.REL_WHEEL, -2)]]
        out = run_frames(h.FrameTransformer(), frames, config(0.25))
        self.assertEqual(out, [
            [(h.EV_REL, h.REL_WHEEL_HI_RES, 30), SYN],
            [(h.EV_REL, h.REL_WHEEL_HI_RES, -60), SYN],
        ])

    def test_high_resolution_value_wins_over_the_detent_event(self):
        # A hi-res mouse mid-detent: 30 units, no low-res event.
        frames = [[(h.EV_REL, h.REL_WHEEL_HI_RES, 30)] for _ in range(4)]
        out = run_frames(h.FrameTransformer(), frames, config(2.0))
        self.assertEqual(wheel_total(out, h.REL_WHEEL_HI_RES), 240)
        self.assertEqual(wheel_total(out, h.REL_WHEEL), 2)

    def test_axes_use_their_own_factor(self):
        frame = [(h.EV_REL, h.REL_WHEEL_HI_RES, 120), (h.EV_REL, h.REL_HWHEEL_HI_RES, 120)]
        out = run_frames(h.FrameTransformer(), [frame], config(0.5, 2.0))
        self.assertEqual(out, [[
            (h.EV_REL, h.REL_WHEEL_HI_RES, 60),
            (h.EV_REL, h.REL_HWHEEL_HI_RES, 240), (h.EV_REL, h.REL_HWHEEL, 2), SYN]])

    def test_wheel_and_motion_in_one_frame(self):
        frame = [(h.EV_REL, h.REL_X, 2)] + detent()
        out = run_frames(h.FrameTransformer(), [frame], config(0.5))
        self.assertEqual(out, [[(h.EV_REL, h.REL_X, 2),
                                (h.EV_REL, h.REL_WHEEL_HI_RES, 60), SYN]])

    def test_frame_scaled_to_nothing_is_dropped(self):
        frames = [[(h.EV_REL, h.REL_WHEEL_HI_RES, 1)] for _ in range(3)]
        out = run_frames(h.FrameTransformer(), frames, config(0.4))
        self.assertEqual(out, [[], [], [(h.EV_REL, h.REL_WHEEL_HI_RES, 1), SYN]])

    def test_totals_match_the_factor_over_a_long_scroll(self):
        frames = [detent() for _ in range(100)]
        out = run_frames(h.FrameTransformer(), frames, config(0.35))
        self.assertEqual(wheel_total(out, h.REL_WHEEL_HI_RES), 4200)
        self.assertEqual(wheel_total(out, h.REL_WHEEL), 35)


class HelpersTest(unittest.TestCase):
    def test_parse_bitmask(self):
        self.assertEqual(h.parse_bitmask('1943'), 0x1943)
        self.assertEqual(h.parse_bitmask('1 0'), 1 << h.LONG_BITS)
        self.assertEqual(h.parse_bitmask(''), 0)

    def test_is_wheel_mouse(self):
        rel = 1 << h.REL_X | 1 << h.REL_Y | 1 << h.REL_WHEEL
        key = 1 << h.BTN_LEFT
        self.assertTrue(h.is_wheel_mouse(rel, key, 0))
        self.assertFalse(h.is_wheel_mouse(rel, 0, 0))                      # no button
        self.assertFalse(h.is_wheel_mouse(1 << h.REL_X | 1 << h.REL_Y, key, 0))  # no wheel
        self.assertFalse(h.is_wheel_mouse(rel, key, 1 << h.INPUT_PROP_POINTING_STICK))
        self.assertFalse(h.is_wheel_mouse(1 << h.REL_WHEEL, key, 0))       # dial, no motion
        self.assertFalse(h.is_wheel_mouse(rel, key | h.KEYBOARD_KEYS, 0))  # keyboard combo
        self.assertTrue(h.is_wheel_mouse(rel, key | 1 << 0x113 | 1 << 0x114, 0))  # side buttons

    def test_normalize_config_clamps_and_merges(self):
        cfg = h.normalize_config({'vertical': 0.0, 'acceleration': 7})
        self.assertEqual(cfg['vertical'], h.FACTOR_MIN)
        self.assertEqual(cfg['acceleration'], 1.0)
        cfg = h.normalize_config({'horizontal': 3}, cfg)
        self.assertEqual((cfg['vertical'], cfg['horizontal']), (h.FACTOR_MIN, 3.0))

    def test_is_neutral(self):
        self.assertTrue(h.is_neutral(h.normalize_config({})))
        self.assertTrue(h.is_neutral(h.normalize_config({'enabled': False, 'vertical': 2})))
        self.assertFalse(h.is_neutral(h.normalize_config({'vertical': 0.5})))
        self.assertFalse(h.is_neutral(h.normalize_config({'acceleration': 0.2})))

    def test_clone_always_offers_high_resolution(self):
        clone = h.clone_description({'rel': [h.REL_X, h.REL_Y, h.REL_WHEEL], 'name': 'm'})
        self.assertIn(h.REL_WHEEL_HI_RES, clone['rel'])
        self.assertNotIn(h.REL_HWHEEL_HI_RES, clone['rel'])

    def test_ioctl_numbers_match_the_kernel_headers(self):
        self.assertEqual(h.EVIOCGRAB, 0x40044590)
        self.assertEqual(h.EVIOCGID, 0x80084502)
        self.assertEqual(h.UI_DEV_CREATE, 0x5501)
        self.assertEqual(h.UI_DEV_DESTROY, 0x5502)
        self.assertEqual(h.UI_DEV_SETUP, 0x405C5503)
        self.assertEqual(h.UI_ABS_SETUP, 0x401C5504)
        self.assertEqual(h.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(h.UI_SET_PROPBIT, 0x4004556E)


if __name__ == '__main__':
    unittest.main()

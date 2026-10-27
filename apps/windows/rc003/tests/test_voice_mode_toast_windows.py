import sys
import threading
import time
import unittest
from unittest import mock

from ovb_rc003 import input_method_watch_windows
from ovb_rc003.voice_mode_toast_windows import VoiceModeToast


class VoiceModeToastWin32BindingTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "needs the Win32 API")
    def test_every_win32_entry_point_binds(self):
        # _bind_win32 会在符号缺失时抛 AttributeError；这里把它整条跑一遍，
        # 免得气泡线程起来后才在后台静默失败。
        user32, gdi32, kernel32 = VoiceModeToast()._bind_win32()
        for handle in (user32, gdi32, kernel32):
            self.assertIsNotNone(handle)
        self.assertTrue(callable(user32.DrawTextW))
        self.assertTrue(callable(user32.SetTimer))
        self.assertTrue(callable(gdi32.GetTextExtentPoint32W))


class VoiceModeToastComposeTests(unittest.TestCase):
    WIDTH = 40
    HEIGHT = 24
    PALETTE = {"fill": (18, 18, 20), "border": (92, 92, 98), "text": (255, 255, 255)}

    def _pixel(self, pixels, visual_row, column):
        # 输出缓冲区自底向上：视觉第 row 行在缓冲区的倒数第 row 行。
        offset = ((self.HEIGHT - 1 - visual_row) * self.WIDTH + column) * 4
        return tuple(pixels[offset : offset + 4])

    def test_mask_rows_land_on_the_matching_screen_rows(self):
        mask = bytearray(self.WIDTH * self.HEIGHT * 4)
        # 蒙版缓冲区同样自底向上，所以视觉第 8 行要写在倒数第 8 行。
        mask_row = self.HEIGHT - 1 - 8
        mask[(mask_row * self.WIDTH + self.WIDTH // 2) * 4] = 0xFF
        pixels = VoiceModeToast._compose_pixels(
            self.WIDTH, self.HEIGHT, bytes(mask), self.PALETTE, 1
        )
        top = self._pixel(pixels, 8, self.WIDTH // 2)
        mirrored = self._pixel(pixels, self.HEIGHT - 1 - 8, self.WIDTH // 2)
        self.assertGreater(top[0], 200)
        self.assertLess(mirrored[0], 60)


class VoiceModeToastInkOffsetTests(unittest.TestCase):
    WIDTH = 40
    HEIGHT = 24
    PALETTE = {"fill": (18, 18, 20), "border": (92, 92, 98), "text": (255, 255, 255)}

    def _mask_with_rows(self, rows):
        mask = bytearray(self.WIDTH * self.HEIGHT * 4)
        for row in rows:
            mask_row = self.HEIGHT - 1 - row
            for column in range(self.WIDTH):
                mask[(mask_row * self.WIDTH + column) * 4] = 0xFF
        return bytes(mask)

    def _pixel(self, pixels, visual_row, column):
        offset = ((self.HEIGHT - 1 - visual_row) * self.WIDTH + column) * 4
        return tuple(pixels[offset : offset + 4])

    def test_ink_below_center_asks_to_move_up(self):
        # 墨迹 14..17 的中心是 16，比盒子中心 12 低 4 行。
        mask = self._mask_with_rows(range(14, 18))
        offset = VoiceModeToast._ink_vertical_offset(self.WIDTH, self.HEIGHT, mask)
        self.assertEqual(offset, 4)

    def test_empty_mask_needs_no_shift(self):
        mask = bytes(self.WIDTH * self.HEIGHT * 4)
        offset = VoiceModeToast._ink_vertical_offset(self.WIDTH, self.HEIGHT, mask)
        self.assertEqual(offset, 0)

    def test_compose_moves_the_text_up_by_the_offset(self):
        mask = self._mask_with_rows(range(14, 18))
        pixels = VoiceModeToast._compose_pixels(
            self.WIDTH, self.HEIGHT, mask, self.PALETTE, 1, 4
        )
        moved = self._pixel(pixels, 10, self.WIDTH // 2)
        original = self._pixel(pixels, 14, self.WIDTH // 2)
        self.assertGreater(moved[0], 200)
        self.assertLess(original[0], 60)


class InputMethodWatchTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(input_method_watch_windows, "_bind_user32", lambda: object()),
            mock.patch.object(input_method_watch_windows, "_own_pid", lambda: 4321),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_new_foreign_window_reports_once(self):
        snapshots = [{1}, {1}, {1, 2}]
        cursor = {"index": 0}

        def visible(_user32, _own_pid):
            index = min(cursor["index"], len(snapshots) - 1)
            cursor["index"] += 1
            return set(snapshots[index])

        calls = []
        reported = threading.Event()

        def on_popup():
            calls.append(1)
            reported.set()

        watch = input_method_watch_windows.InputMethodWatch(
            duration_ms=800, interval_ms=40
        )
        with mock.patch.object(input_method_watch_windows, "_visible_windows", visible):
            watch.watch(on_popup)
            self.assertTrue(reported.wait(2.0))
            watch.cancel()
        self.assertEqual(calls, [1])

    def test_cancel_stops_the_watch_before_any_window_appears(self):
        watch = input_method_watch_windows.InputMethodWatch(
            duration_ms=800, interval_ms=40
        )
        calls = []
        with mock.patch.object(
            input_method_watch_windows, "_visible_windows", lambda *a: {1}
        ):
            watch.watch(lambda: calls.append(1))
            watch.cancel()
        time.sleep(0.15)
        self.assertEqual(calls, [])

    def test_a_panel_visible_before_the_toast_still_reports_once(self):
        # Chatterfly 的面板按下就翻成可见，气泡松手后才显示：按下时 arm() 记的
        # 基线里没有它，watch() 就要立刻认出来，而不是把它当成"一直都在"。
        snapshots = [{1}, {1, 2}]
        cursor = {"index": 0}

        def visible(_user32, _own_pid):
            index = min(cursor["index"], len(snapshots) - 1)
            cursor["index"] += 1
            return set(snapshots[index])

        calls = []
        reported = threading.Event()

        def on_popup():
            calls.append(1)
            reported.set()

        watch = input_method_watch_windows.InputMethodWatch(
            duration_ms=800, interval_ms=40
        )
        with mock.patch.object(input_method_watch_windows, "_visible_windows", visible):
            watch.arm()
            watch.watch(on_popup)
            self.assertTrue(reported.wait(2.0))
            watch.cancel()
        self.assertEqual(calls, [1])

    def test_an_armed_baseline_expires_instead_of_matching_later_windows(self):
        watch = input_method_watch_windows.InputMethodWatch(
            duration_ms=400, interval_ms=40
        )
        calls = []
        with mock.patch.object(
            input_method_watch_windows, "_visible_windows", lambda *a: {1}
        ), mock.patch.object(
            input_method_watch_windows, "_BASELINE_MAX_AGE_SECONDS", 0.0
        ):
            watch.arm()
            watch.watch(lambda: calls.append(1))
            time.sleep(0.2)
            watch.cancel()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()


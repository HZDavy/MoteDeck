import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()

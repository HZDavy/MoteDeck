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


if __name__ == "__main__":
    unittest.main()

"""谷歌遥控器 ATVV v1.0 CAPS 回包的能力判定。"""
import unittest

from motedeck.chromecast_voice import supported_capabilities


class SupportedCapabilitiesTests(unittest.TestCase):
    def test_nine_byte_reply_is_supported(self):
        self.assertTrue(supported_capabilities(bytes.fromhex("0b0100020300a00100")))

    def test_rev_0110_reply_with_trailing_byte_is_supported(self):
        # 18D1:9450 REV 0110 实测回 10 字节：前 9 字节字段合规，末尾多一个字节。
        self.assertTrue(supported_capabilities(bytes.fromhex("0b0100020300f001001a")))

    def test_trailing_byte_does_not_hide_invalid_leading_fields(self):
        for payload in (
            bytes.fromhex("0b0100020000f001001a"),  # interaction 不是 3
            bytes.fromhex("0b0100010300f001001a"),  # 不支持 16 kHz ADPCM
            bytes.fromhex("0b0001020300f001001a"),  # 版本不是 v1.0
            bytes.fromhex("0b01000203020101001a"),  # 帧长超过 512
            bytes.fromhex("0b0100020300f001"),  # 不足 9 字节
        ):
            with self.subTest(payload=payload.hex()):
                self.assertFalse(supported_capabilities(payload))


if __name__ == "__main__":
    unittest.main()

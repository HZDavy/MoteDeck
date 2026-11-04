import unittest
from unittest import mock

from ovb_rc003 import input_method_profile_windows as input_method


class InputMethodProfileTests(unittest.TestCase):
    def profile(self, description="", service="{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}",
                profile="{BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB}"):
        return input_method.InputMethodProfile(service, 0x0804, profile, description)

    def test_exact_doubao_description_wins(self):
        partial = self.profile("豆包拼音")
        exact = self.profile("豆包输入法")
        self.assertIs(input_method.select_doubao_profile((partial, exact)), exact)

    def test_known_ids_are_a_description_independent_fallback(self):
        known = self.profile(
            service=input_method._DOUBAO_SERVICE_CLSID,
            profile=input_method._DOUBAO_PROFILE_GUID,
        )
        self.assertIs(input_method.select_doubao_profile((known,)), known)

    def test_activate_uses_discovered_profile(self):
        profile = self.profile("豆包输入法")
        with mock.patch.object(input_method, "find_doubao_profile", return_value=profile), \
                mock.patch.object(input_method, "_activate_profile") as activate:
            self.assertIs(input_method.activate_doubao_input_method(), profile)
        activate.assert_called_once_with(profile)

    def test_activate_wraps_unexpected_windows_errors(self):
        with mock.patch.object(
            input_method, "find_doubao_profile", side_effect=ValueError("bad GUID")
        ):
            with self.assertRaises(input_method.InputMethodUnavailableError):
                input_method.activate_doubao_input_method()

    def test_guid_round_trip(self):
        value = "{71C6E74C-0F28-11D8-A82A-00065B84435C}"
        self.assertEqual(input_method._GUID.from_string(value).to_string(), value)


if __name__ == "__main__":
    unittest.main()

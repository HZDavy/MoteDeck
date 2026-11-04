"""Independent Google TV identity, safe legacy upgrade and runtime routing."""
import unittest
from unittest.mock import patch
from ovb_rc003 import remote_selection as selection, remote_layout, device_catalog, resources

A, B = 'a' * 64, 'b' * 64


class GoogleTvProfileTests(unittest.TestCase):
    def test_catalog_and_layout_are_independent_from_chromecast(self):
        google = device_catalog.profile_for(selection.GOOGLE_TV_PROFILE)
        chrome = device_catalog.profile_for(selection.CHROMECAST_PROFILE)
        self.assertEqual(google.display_name, 'Google TV Remote')
        self.assertNotEqual(google.device_id, chrome.device_id)
        self.assertEqual(google.bluetooth_names, ('Google TV Remote',))
        self.assertEqual(chrome.bluetooth_names, ('Chromecast Remote',))
        for profile in selection.GOOGLE_REMOTE_PROFILES:
            self.assertTrue(selection.runtime_ready(profile))
            self.assertIn('volume_up', remote_layout.button_order(profile))
            self.assertIn('youtube', remote_layout.default_actions(profile))
            self.assertIsNotNone(remote_layout.hotspot_for('mic', profile))
            self.assertEqual(next(resources._candidate_paths(profile)).name, 'Chromecast-remote-photo.png')

    def test_confirmed_scan_upgrades_same_identity_without_changing_other_device(self):
        saved = {'schema': 1, 'active': A, 'devices': [
            {'key': A, 'profile': selection.CHROMECAST_PROFILE},
            {'key': B, 'profile': selection.CHROMECAST_PROFILE}]}
        upgraded, legacy = selection.select_paired_device(saved, A, [
            {'key': A, 'profile': selection.GOOGLE_TV_PROFILE}])
        self.assertEqual(upgraded['active'], A)
        self.assertEqual(selection.profile_for_key(upgraded, A), selection.GOOGLE_TV_PROFILE)
        self.assertEqual(selection.profile_for_key(upgraded, B), selection.CHROMECAST_PROFILE)
        self.assertEqual(selection.profile_for_key(saved, A), selection.CHROMECAST_PROFILE)
        self.assertFalse(legacy)
        with self.assertRaises(selection.SelectionError):
            selection.select_paired_device(upgraded, A, [{'key': A, 'profile': selection.RC003_PROFILE}])

    def test_both_google_profiles_are_accepted_by_receive_workers(self):
        from ovb_rc003 import chromecast_hid_worker, chromecast_worker
        for profile in selection.GOOGLE_REMOTE_PROFILES:
            settings = {selection.KEY: {'schema':1, 'active': A,
                'devices':[{'key':A, 'profile':profile}]}}
            with self.subTest(profile=profile), patch.object(chromecast_hid_worker.config, 'load_config', return_value=settings):
                self.assertTrue(chromecast_hid_worker._selected(A))
                self.assertFalse(chromecast_hid_worker._selected(B))
                self.assertTrue(chromecast_worker._selected(A))

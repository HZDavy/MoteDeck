"""Shutdown evidence with fake processes only; no device or input operations."""
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import bridge_launcher, diagnostic_trace, log_export


class ShutdownDiagnosticsTests(unittest.TestCase):
    def test_timeout_snapshots_threads_but_success_does_not(self):
        for stopped in (False, True):
            handle = SimpleNamespace(is_alive=True, request_stop=mock.Mock(), wait=mock.Mock(return_value=stopped))
            with mock.patch.object(bridge_launcher, '_in_process_handle', handle), \
                 mock.patch.object(diagnostic_trace, 'log_shutdown_threads') as snapshot:
                self.assertEqual(bridge_launcher.stop_in_process_bridge(timeout=.01), stopped)
                self.assertEqual(snapshot.call_count, int(not stopped))

    def test_snapshot_exports_code_locations_without_source_or_locals(self):
        code = SimpleNamespace(co_name='stop', co_filename='C:/PRIVATE/user/program.py')
        frame = SimpleNamespace(f_globals={'__name__': 'ovb_rc003.pipe_windows'},
            f_code=code, f_lineno=105, f_back=None, f_locals={'token': 'PRIVATE_TOKEN'})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'logs').mkdir()
            handler = logging.FileHandler(root / 'logs/app.log', encoding='utf-8')
            logger = logging.getLogger('shutdown-export-test')
            logger.addHandler(handler)
            try:
                with mock.patch('sys._current_frames', return_value={threading.get_ident(): frame}):
                    diagnostic_trace.log_shutdown_threads(logger)
            finally:
                logger.removeHandler(handler)
                handler.close()
            destination = root / 'logs.zip'
            self.assertEqual(log_export.export_logs(destination, root=root).outcome, 'exported')
            with zipfile.ZipFile(destination) as archive:
                output = archive.read('app.log').decode('utf-8')
            self.assertIn('ovb_rc003.pipe_windows:stop:105', output)
            self.assertNotIn('PRIVATE', output)

    def test_snapshot_failure_is_diagnostic_only(self):
        with mock.patch('sys._current_frames', side_effect=RuntimeError('PRIVATE')):
            with self.assertLogs('ovb_rc003', level='WARNING') as captured:
                diagnostic_trace.log_shutdown_threads(logging.getLogger('ovb_rc003'))
        self.assertIn('error_type=RuntimeError', '\n'.join(captured.output))
        self.assertNotIn('PRIVATE', '\n'.join(captured.output))

    def test_compiled_frame_absence_still_reports_cleanup_and_collection_result(self):
        logger = logging.getLogger('ovb_rc003')
        progress = diagnostic_trace.CleanupProgress(logger, 'receiver', 'a' * 12)
        progress.update('pipe_close', 'begin')
        progress.update('pipe_close', 'done')
        progress.update('process_exit_wait', 'begin')
        # Native builds may expose only standard-library frames, or none.
        frame = SimpleNamespace(f_globals={'__name__': 'threading'},
            f_code=SimpleNamespace(co_name='wait'), f_lineno=331, f_back=None)
        for frames in ({}, {threading.get_ident(): frame}):
            with mock.patch('sys._current_frames', return_value=frames), \
                 self.assertLogs('ovb_rc003', level='WARNING') as captured:
                diagnostic_trace.log_shutdown_threads(logger)
            output = '\n'.join(captured.output)
            self.assertIn('business_locations=unavailable', output)
            self.assertIn('stage=process_exit_wait state=begin', output)
            self.assertIn('last_completed=pipe_close', output)
            self.assertIn('native_stack=unavailable', output)

    def test_progress_duration_and_logging_failure_do_not_break_cleanup(self):
        logger = mock.Mock()
        with mock.patch.object(diagnostic_trace.time, 'monotonic', side_effect=[10, 10, 10.25]):
            progress = diagnostic_trace.CleanupProgress(logger, 'receiver')
            progress.update('pipe_close', 'begin')
            logger.info.side_effect = OSError('PRIVATE')
            progress.update('pipe_close', 'done')
        self.assertEqual(progress.state[4], 250)
        self.assertEqual(progress.state[3], 'pipe_close')

    def test_stop_origin_is_recorded_and_arbitrary_text_is_not(self):
        from ovb_rc003 import bridge_control_windows as control
        with mock.patch.object(bridge_launcher, 'stop_in_process_bridge', return_value=True) as stop, \
             mock.patch.object(control.bridge_runtime_status, 'read_status', return_value=None), \
             self.assertLogs('ovb_rc003', level='INFO') as captured:
            self.assertTrue(control.request_bridge_exit(reason='user_restart').stopped)
        stop.assert_called_once_with(timeout=5.0, reason='user_restart')
        self.assertIn('reason=user_restart', '\n'.join(captured.output))
        self.assertEqual(bridge_launcher.stop_reason('PRIVATE'), 'unspecified')

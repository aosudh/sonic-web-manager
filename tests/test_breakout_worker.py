import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('breakout_worker', Path(__file__).resolve().parents[1] / 'deploy/breakout_worker.py')
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)


class BreakoutWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / ('a' * 32)
        self.directory.mkdir()
        (self.directory / 'plan.json').write_text('{}')
        WORKER.status(self.directory, 'queued', 'queued')

    def tearDown(self):
        self.temp.cleanup()

    def read_status(self):
        return json.loads((self.directory / 'status.json').read_text())

    def test_completed_operation_cannot_repeat_reboot(self):
        WORKER.status(self.directory, 'succeeded', 'done', verified=True)
        with patch('server.port_breakout.stage') as stage, patch.object(WORKER.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                WORKER.apply_operation(self.directory)
            stage.assert_not_called()
            run.assert_not_called()

    def test_stage_failure_never_reboots(self):
        with patch('server.port_breakout.stage', side_effect=ValueError('source changed')), patch.object(WORKER.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                WORKER.apply_operation(self.directory)
            run.assert_not_called()
        self.assertEqual(self.read_status()['status'], 'failed')

    def test_reboot_failure_restores_boot_files(self):
        with patch('server.port_breakout.stage'), patch('server.port_breakout.rollback', return_value={'success': True, 'requires_reboot': False}) as rollback, patch.object(WORKER, 'boot_id', return_value='boot-one'), patch.object(WORKER.time, 'sleep'), patch.object(WORKER.subprocess, 'run', side_effect=FileNotFoundError('not started')):
            with self.assertRaises(FileNotFoundError):
                WORKER.apply_operation(self.directory)
            rollback.assert_called_once_with(self.directory)
        self.assertEqual(self.read_status()['status'], 'rolled_back')
        self.assertTrue(self.read_status()['files_restored'])

    def test_reboot_failure_does_not_claim_failed_rollback_succeeded(self):
        with patch('server.port_breakout.stage'), patch('server.port_breakout.rollback', side_effect=ValueError('external change')), patch.object(WORKER, 'boot_id', return_value='boot-one'), patch.object(WORKER.time, 'sleep'), patch.object(WORKER.subprocess, 'run', side_effect=FileNotFoundError('not started')):
            with self.assertRaises(FileNotFoundError):
                WORKER.apply_operation(self.directory)
        self.assertEqual(self.read_status()['status'], 'failed')
        self.assertFalse(self.read_status()['files_restored'])

    def test_reboot_timeout_does_not_race_shutdown_with_rollback(self):
        with patch('server.port_breakout.stage'), patch('server.port_breakout.rollback') as rollback, patch.object(WORKER, 'boot_id', return_value='boot-one'), patch.object(WORKER.time, 'sleep'), patch.object(WORKER.subprocess, 'run', side_effect=subprocess.TimeoutExpired('reboot', 180)):
            with self.assertRaises(subprocess.TimeoutExpired):
                WORKER.apply_operation(self.directory)
            rollback.assert_not_called()
        self.assertEqual(self.read_status()['status'], 'pending_reboot')
        self.assertTrue(self.read_status()['reboot_result_unknown'])

    def test_rollback_failure_keeps_writes_blocked(self):
        WORKER.status(self.directory, 'succeeded', 'done', verified=True)
        with patch('server.port_breakout.rollback', side_effect=ValueError('write failed')), patch.object(WORKER.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                WORKER.rollback_operation(self.directory)
            run.assert_not_called()
        self.assertEqual(self.read_status()['status'], 'failed')

    def test_same_boot_cannot_be_marked_verified(self):
        WORKER.status(self.directory, 'pending_reboot', 'waiting', boot_id_before='same-boot')
        with patch.object(WORKER, 'boot_id', return_value='same-boot'), patch('server.port_breakout.verify') as verify:
            result = WORKER.verify_operation(self.directory)
            verify.assert_not_called()
        self.assertEqual(result['status'], 'pending_reboot')
        self.assertFalse(result['verified'])

    def test_hardware_readback_required_for_completion(self):
        WORKER.status(self.directory, 'pending_reboot', 'waiting', boot_id_before='old-boot')
        with patch.object(WORKER, 'boot_id', return_value='new-boot'), patch.object(WORKER.time, 'sleep'), patch('server.port_breakout.verify', side_effect=[{'verified': False, 'errors': ['ASIC not ready']}, {'verified': True, 'errors': []}]) as verify:
            result = WORKER.verify_operation(self.directory)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['verified'])

    def test_identifier_cannot_select_arbitrary_paths(self):
        for value in ('../escape', '/tmp/operation', 'a' * 31, 'a' * 33, 'g' * 32):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WORKER.checked_directory(value, Path(self.temp.name))

    def test_status_refuses_symlink(self):
        destination = self.directory / 'status.json'
        destination.unlink()
        outside = Path(self.temp.name) / 'outside.json'
        outside.write_text('original')
        try:
            destination.symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks require platform privileges')
        with self.assertRaises(ValueError):
            WORKER.status(self.directory, 'queued', 'queued')
        self.assertEqual(outside.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()

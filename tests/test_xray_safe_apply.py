"""Deployment regressions: unreadable files and delayed service crashes must not cause an outage."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('xray_safe_apply', ROOT / 'bin/xray-safe-apply.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class SafeApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text('{"old": true}')
        self.deploy = mod.XrayDeployment(self.config, self.root / 'state', settle=0)
        self.deploy.state.mkdir()

    def test_checks_as_service_user_and_respects_group(self):
        props = {'User': 'nobody', 'Group': 'nogroup', 'SupplementaryGroups': '', 'Environment': ''}
        with patch.object(self.deploy, 'properties', return_value=props), patch.object(self.deploy, 'command') as command:
            self.deploy.validate(self.config)
        self.assertEqual(command.call_args_list[0].args[0],
                         ['runuser', '-u', 'nobody', '-g', 'nogroup', '--', 'test', '-r', str(self.config)])
        self.assertIn('/usr/local/bin/xray', command.call_args_list[1].args[0])

    def test_bad_candidate_never_replaces_current_config(self):
        candidate = self.root / 'bad.json'
        candidate.write_text('{invalid')
        before = self.config.read_bytes()
        with self.assertRaises(mod.DeploymentError):
            self.deploy.install(candidate)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.root.glob('.candidate-*')), [])

    def test_unreadable_current_config_never_stops_running_service(self):
        with patch.object(self.deploy, 'validate', side_effect=mod.DeploymentError('permission denied')), \
                patch.object(self.deploy, 'command') as command:
            with self.assertRaises(mod.DeploymentError):
                self.deploy.restart()
        command.assert_not_called()

    def test_delayed_start_failure_rolls_back_and_reports_failure(self):
        previous = self.config.read_bytes()
        self.deploy.good.write_bytes(previous)
        self.config.write_text('{"new": true}')
        with patch.object(self.deploy, 'validate'), patch.object(self.deploy, 'command') as command, \
                patch.object(self.deploy, 'stable', side_effect=[mod.DeploymentError('delayed crash'), None]):
            with self.assertRaisesRegex(mod.DeploymentError, 'previous checked config restored'):
                self.deploy.restart()
        self.assertEqual(self.config.read_bytes(), previous)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(self.deploy.good.read_bytes(), previous)

    def test_pid_change_is_not_a_successful_start(self):
        original = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '100'}
        changed = dict(original, MainPID='101')
        with patch.object(self.deploy, 'properties', side_effect=[original, changed]):
            with self.assertRaisesRegex(mod.DeploymentError, 'same running process'):
                self.deploy.stable()

    def test_active_state_followed_by_crash_is_not_a_successful_start(self):
        running = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '100'}
        failed = {'ActiveState': 'failed', 'SubState': 'failed', 'MainPID': '0'}
        self.deploy.settle = 5
        with patch.object(self.deploy, 'properties', side_effect=[running, running, failed]), \
                patch.object(mod.time, 'sleep'):
            with self.assertRaisesRegex(mod.DeploymentError, 'same running process'):
                self.deploy.stable()

    def test_checkpoint_is_only_replaced_after_stable_start(self):
        self.deploy.good.write_text('{"old": true}')
        self.config.write_text('{"new": true}')
        with patch.object(self.deploy, 'validate'), patch.object(self.deploy, 'stable'), \
                patch.object(self.deploy, 'command'):
            self.deploy.restart()
        self.assertEqual(self.deploy.good.read_bytes(), self.config.read_bytes())

    @unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0,
                         'requires Linux root to reproduce nobody access')
    def test_root_readable_600_source_is_staged_for_nobody(self):
        self.root.chmod(0o755)
        source = self.root / 'source.json'
        source.write_text('{"new": true}')
        source.chmod(0o600)
        self.assertNotEqual(subprocess.run(['runuser', '-u', 'nobody', '--', 'test', '-r', str(source)]).returncode, 0)
        def validate_as_nobody(path):
            subprocess.run(['runuser', '-u', 'nobody', '--', 'python3', '-c',
                            'import json,sys; json.load(open(sys.argv[1]))', str(path)], check=True)
        with patch.object(self.deploy, 'validate', side_effect=validate_as_nobody), \
                patch.object(self.deploy, 'properties', return_value={'ActiveState': 'inactive'}):
            self.deploy.install(source)
        validate_as_nobody(self.config)


if __name__ == '__main__':
    unittest.main()

"""Journal lookup must preserve time arguments and distinguish missing logs from failed reads."""
import importlib.util
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vpn_log', ROOT / 'bin/vpn-log.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class JournalTests(unittest.TestCase):
    def test_time_with_spaces_survives_local_and_remote_invocation(self):
        for host in (None, '192.0.2.1'):
            with patch.object(mod.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'line\n', '')) as run:
                self.assertEqual(mod.journal(host, '6 hours ago', None, 'tiktok', False), ['line'])
            args = run.call_args.args[0]
            command = args if host is None else shlex.split(args[-1])
            self.assertEqual(command[command.index('--since') + 1], '6 hours ago')
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_failed_journal_lookup_does_not_report_no_traffic(self):
        with patch.object(mod.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'bad time argument')):
            with self.assertRaisesRegex(RuntimeError, 'journal unavailable'):
                mod.journal(None, 'bad time', None, 'tiktok', False)

    def test_no_matches_is_an_empty_successful_lookup(self):
        with patch.object(mod.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            self.assertEqual(mod.journal(None, '-1h', None, 'tiktok', False), [])

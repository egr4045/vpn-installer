import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vpn_probe', ROOT / 'bin/vpn-probe.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ProbeTests(unittest.TestCase):
    def test_plain_http_200_or_captcha_does_not_prove_region(self):
        self.assertIsNone(mod.tiktok_context('<html>Please verify you are human</html>'))
        self.assertIsNone(mod.tiktok_context('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">invalid</script>'))

    def test_region_is_read_from_structured_app_context(self):
        html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{"__DEFAULT_SCOPE__":{"webapp.app-context":{"region":"NL"}}}</script>'
        self.assertEqual(mod.tiktok_context(html), 'NL')

    def test_alerts_only_after_two_failures_and_once_on_recovery(self):
        state, event = mod.notification_transition({}, True)
        self.assertIsNone(event)
        state, event = mod.notification_transition(state, True)
        self.assertEqual(event, 'failure')
        state, event = mod.notification_transition(state, True)
        self.assertIsNone(event)
        state, event = mod.notification_transition(state, False)
        self.assertEqual(event, 'recovery')
        _, event = mod.notification_transition(state, False)
        self.assertIsNone(event)

    def test_persistent_latency_alert_has_hysteresis_and_frozen_baseline(self):
        def result(control, tiktok, ok=True):
            return {'network_ok': ok, 'control': {'ttfb_s': control}, 'tiktok': {'ttfb_s': tiktok}}
        state = {}
        for _ in range(6):
            state, event, _ = mod.latency_transition(state, result(.08, .4))
            self.assertIsNone(event)
        baseline = list(state['history'])
        for expected in [None, None, 'slow', None]:
            state, event, _ = mod.latency_transition(state, result(.6, 2))
            self.assertEqual(event, expected)
            self.assertEqual(state['history'], baseline)
        state, event, _ = mod.latency_transition(state, result(.08, .4))
        self.assertIsNone(event)
        state, event, _ = mod.latency_transition(state, result(.08, .4))
        self.assertEqual(event, 'recovery')
        state, event, reasons = mod.latency_transition(state, result(0, 0, False))
        self.assertIsNone(event)
        self.assertEqual(reasons, [])

    def test_one_bad_transport_does_not_reset_its_failure_counter_when_others_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(mod, 'LIB', root), patch.object(mod, 'LOG', root / 'checks.jsonl'):
                for run in range(2):
                    snapshot = {'results': [
                        {'path': 'windows-primary', 'transport': 'tcp', 'network_ok': False, 'delay_ms': 0},
                        {'path': 'windows-primary', 'transport': 'xhttp', 'network_ok': True, 'delay_ms': 45}]}
                    mod.watch(snapshot, {}, client=True)
                self.assertEqual(len(snapshot['notification_events']), 1)
                self.assertIn('windows-primary/tcp', snapshot['notification_events'][0])
                state = json.loads((root / 'client-probe-state.json').read_text())
                self.assertTrue(state['paths']['windows-primary/tcp']['health']['alerted'])
                self.assertFalse(state['paths']['windows-primary/xhttp']['health']['alerted'])
                self.assertFalse((root / 'probe-state.json').exists())

    def test_client_latency_is_measured_as_delay_and_not_mislabelled_ttfb(self):
        state = {}
        for _ in range(6):
            state, _, _ = mod.latency_transition(state, {'network_ok': True, 'delay_ms': 40})
        for expected in [None, None, 'slow']:
            state, event, reasons = mod.latency_transition(state, {'network_ok': True, 'delay_ms': 200})
            self.assertEqual(event, expected)
            self.assertIn('Mihomo delay', reasons[0])

    def test_failed_telegram_delivery_is_retried_instead_of_losing_the_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {'TG_BOT_TOKEN': 'test-token', 'TG_CHAT_ID': 'test-chat'}
            with patch.object(mod, 'LIB', root), patch.object(mod, 'LOG', root / 'checks.jsonl'), \
                    patch.object(mod.urllib.request, 'urlopen', side_effect=[OSError('offline'), io.StringIO('{"ok":true}')]) as send:
                for _ in range(3):
                    snapshot = {'results': [{'path': 'windows-primary', 'transport': 'tcp',
                        'network_ok': False, 'delay_ms': 0}]}
                    mod.watch(snapshot, env, client=True)
                self.assertEqual(send.call_count, 2)
                state = json.loads((root / 'client-probe-state.json').read_text())
                self.assertTrue(state['paths']['windows-primary/tcp']['health']['alerted'])
                self.assertNotIn('test-token', (root / 'checks.jsonl').read_text())

#!/usr/bin/env python3
"""Opt-in live integration check: isolated loopback Xray, never stops production services."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--primary', required=True)
    parser.add_argument('--standby', required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('probe', Path(__file__).resolve().parents[1] / 'bin/vpn-probe.py')
    probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
    original = json.loads(args.config.read_text())
    assert any(b['tag'] == 'tiktok-exits' for b in original['routing']['balancers'])
    for failed in (False, True):
        config = copy.deepcopy(original)
        with socket.socket() as bound:
            bound.bind(('127.0.0.1', 0)); port = bound.getsockname()[1]
        config['inbounds'] = [{'tag': 'isolated-check', 'listen': '127.0.0.1', 'port': port,
                              'protocol': 'socks', 'settings': {'auth': 'noauth'}}]
        config['log'] = {'loglevel': 'error'}
        config['observatory']['probeInterval'] = '1s'
        config['routing']['rules'].insert(0, {'type': 'field', 'domain': ['full:api.ipify.org'],
                                             'balancerTag': 'tiktok-exits'})
        if failed:
            outbound = next(o for o in config['outbounds'] if o['tag'] == 'tiktok-standby')
            server = outbound['settings']['vnext'][0]
            server['address'], server['port'] = '127.0.0.1', 9
        with tempfile.TemporaryDirectory(prefix='safechill-failover-') as directory:
            path = Path(directory) / 'config.json'; path.write_text(json.dumps(config)); path.chmod(0o600)
            with (Path(directory) / 'client.log').open('w') as log:
                child = subprocess.Popen(['/usr/local/bin/xray', 'run', '-c', str(path)], stdout=log, stderr=log)
                try:
                    for _ in range(50):
                        if child.poll() is not None:
                            raise RuntimeError('isolated Xray exited')
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.1): pass
                            break
                        except OSError: time.sleep(.1)
                    time.sleep(3)
                    ip_status, address = probe.fetch(port, 'https://api.ipify.org')
                    expected = args.primary if failed else args.standby
                    assert ip_status['http'] == 200 and address.strip() == expected, (failed, ip_status, address)
                    status, body = probe.fetch(port, 'https://www.tiktok.com/explore')
                    assert status['http'] == 200 and status['curl_exit'] == 0, status
                    print(json.dumps({'isolated': True, 'preferred_unreachable': failed,
                        'egress': address.strip(), 'tiktok_http': status['http'],
                        'web_region': probe.tiktok_context(body), 'app_feed_checked': False}))
                finally:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()


if __name__ == '__main__':
    main()

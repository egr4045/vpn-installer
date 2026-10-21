#!/usr/bin/env python3
"""Windows-only: read-only Mihomo delay checks; send sanitized metrics to the VPN monitor via SSH."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    args = parser.parse_args()
    settings = Path(os.environ['APPDATA']) / 'clashmi/clashmi/service.json'
    log = Path(os.environ['LOCALAPPDATA']) / 'SafeChillVPN/client-probes.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'source': 'windows'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        config = json.loads(settings.read_text(encoding='utf-8'))
        base = 'http://127.0.0.1:' + str(int(config.get('control_port', 9090)))
        def get(path, timeout=7):
            request = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + config['secret']})
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        get('/version', 2)
        proxies = get('/proxies', 2)['proxies']
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        # The desktop app may be intentionally stopped. Do not report a VPN outage then.
        snapshot['skipped'] = 'Clash Mi controller unavailable'
    else:
        results = []
        for label, name in [('primary', '🇳🇱 Амстердам'), ('standby', '🇳🇱 Амстердам-2'), ('ru', '🇷🇺 Россия')]:
            for transport in ('xhttp', 'tcp'):
                proxy = name + (' TCP' if transport == 'tcp' else '')
                if proxy not in proxies:
                    snapshot['skipped'] = 'expected SafeChill proxies are absent'
                    break
                path = '/proxies/' + urllib.parse.quote(proxy, safe='') + '/delay?' + urllib.parse.urlencode({
                    'url': 'https://cp.cloudflare.com/generate_204', 'timeout': 4000})
                result = {'path': 'windows-' + label, 'transport': transport, 'delay_ms': 0, 'network_ok': False}
                try:
                    delay = get(path).get('delay')
                    if isinstance(delay, (int, float)) and 0 < delay <= 120000:
                        result.update(delay_ms=delay, network_ok=True)
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                results.append(result)
        if len(results) == 6:
            snapshot['results'] = results
            try:
                sent = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                    '-o', 'StrictHostKeyChecking=yes', 'root@' + args.server,
                    '/usr/local/bin/vpn-probe.py --client-watch'], input=json.dumps(snapshot),
                    capture_output=True, text=True, encoding='utf-8', timeout=25)
                snapshot['delivery'] = 'ok' if sent.returncode == 0 else 'failed'
            except (OSError, subprocess.SubprocessError):
                snapshot['delivery'] = 'failed'
    if log.exists() and log.stat().st_size > 2 * 1024 * 1024:
        os.replace(log, log.with_suffix('.jsonl.1'))
    with log.open('a', encoding='utf-8') as output:
        output.write(json.dumps(snapshot) + '\n')
    print(json.dumps(snapshot))


if __name__ == '__main__':
    main()

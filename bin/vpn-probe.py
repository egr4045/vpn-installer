#!/usr/bin/env python3
"""Authenticated VLESS path checks. TikTok web reachability is not proof of a working app feed."""
import argparse
import concurrent.futures
import copy
import datetime
import json
import os
from pathlib import Path
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ETC = Path('/etc/safechill')
LIB = Path('/var/lib/safechill')
LOG = Path('/var/log/safechill-probes.jsonl')
UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'


def load_env(path):
    result = {}
    for line in path.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\'"')
    return result


def tiktok_context(html):
    match = re.search(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1)).get('__DEFAULT_SCOPE__', {}).get('webapp.app-context', {}).get('region')
    except (ValueError, AttributeError):
        return None


def fetch(port, url):
    result = subprocess.run(['curl', '--proxy', f'socks5h://127.0.0.1:{port}', '--noproxy', '',
        '--proto', '=https', '-L', '--max-redirs', '3', '--max-time', '15', '--compressed', '-sS',
        '-A', UA, '-w', '\n%{http_code} %{time_starttransfer} %{time_total}', url],
        capture_output=True, text=True, timeout=20)
    body, _, trailer = result.stdout.rpartition('\n')
    fields = trailer.split()
    status = int(fields[0]) if fields and fields[0].isdigit() else 0
    return {'curl_exit': result.returncode, 'http': status,
            'ttfb_s': float(fields[1]) if len(fields) > 1 else None,
            'total_s': float(fields[2]) if len(fields) > 2 else None}, body


def probe(task):
    label, host, sni, transport, user, env, base = task
    result = {'path': label, 'transport': transport, 'app_feed_checked': False}
    with tempfile.TemporaryDirectory(prefix='safechill-probe-') as directory:
        outbound = copy.deepcopy(base)
        server = outbound['settings']['vnext'][0]
        server['address'] = host
        server['users'][0]['id'] = user['uuid']
        reality = outbound['streamSettings']['realitySettings']
        reality['serverName'] = sni
        if transport == 'tcp':
            server['port'] = int(env.get('TCP_PORT', '8443'))
            server['users'][0]['flow'] = 'xtls-rprx-vision'
            reality['serverName'] = env.get('RU_SNI', 'yandex.ru') if label == 'ru' else env.get('FALLBACK_SNI', 'gateway.icloud.com')
            reality['fingerprint'] = 'chrome'
            outbound['streamSettings'] = {'network': 'tcp', 'security': 'reality', 'realitySettings': reality}
        with socket.socket() as bound:
            bound.bind(('127.0.0.1', 0))
            port = bound.getsockname()[1]
        config = {'log': {'loglevel': 'error'}, 'inbounds': [{'listen': '127.0.0.1', 'port': port,
                  'protocol': 'socks', 'settings': {'auth': 'noauth'}}], 'outbounds': [outbound]}
        path = Path(directory) / 'client.json'
        path.write_text(json.dumps(config)); path.chmod(0o600)
        with (Path(directory) / 'client.log').open('w') as log:
            client = subprocess.Popen(['/usr/local/bin/xray', 'run', '-c', str(path)], stdout=log, stderr=log)
            try:
                for _ in range(50):
                    if client.poll() is not None:
                        raise RuntimeError('probe client failed to start')
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.05)
                result['control'], _ = fetch(port, 'https://cp.cloudflare.com/generate_204')
                result['tiktok'], html = fetch(port, 'https://www.tiktok.com/explore')
                result['web_region'] = tiktok_context(html)
                result['network_ok'] = (result['control']['curl_exit'] == 0 and result['control']['http'] == 204
                    and result['tiktok']['curl_exit'] == 0 and result['tiktok']['http'] == 200)
                result['web_check_ok'] = result['network_ok'] and result['web_region'] == 'NL'
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                result.update(network_ok=False, web_check_ok=False, error=type(error).__name__)
            finally:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill(); client.wait()
    return result


def notification_transition(previous, failed):
    """Two consecutive failed runs trigger one alert; recovery triggers one more."""
    count = previous.get('fails', 0) + 1 if failed else 0
    alerted = previous.get('alerted', False)
    event = 'failure' if count >= 2 and not alerted else 'recovery' if not failed and alerted else None
    return {'fails': count, 'alerted': failed and (alerted or count >= 2)}, event


def latency_transition(previous, result):
    """Alert after three slow runs; freeze the baseline while slow and clear after two good runs."""
    state = copy.deepcopy(previous)
    history = state.setdefault('history', [])
    if not result['network_ok']:
        state['slow_runs'] = state['good_runs'] = 0
        return state, None, []
    client = 'delay_ms' in result
    values = [result['delay_ms'] / 1000] if client else [result[k]['ttfb_s'] for k in ('control', 'tiktok')]
    limits = [.25] if client else [1.0, 3.0]
    floors = [.12] if client else [.35, 1.5]
    names = ['Mihomo delay'] if client else ['control TTFB', 'TikTok TTFB']
    if len(history) >= 6:
        limits = [min(absolute, max(floor, 4 * statistics.median(h[i] for h in history)))
                  for i, (absolute, floor) in enumerate(zip(limits, floors))]
    reasons = [f'{name} {value:.2f}s > {limit:.2f}s'
               for name, value, limit in zip(names, values, limits) if value > limit]
    slow = bool(reasons)
    state['slow_runs'] = state.get('slow_runs', 0) + 1 if slow else 0
    state['good_runs'] = 0 if slow else state.get('good_runs', 0) + 1
    event = None
    if state['slow_runs'] >= 3 and not state.get('alerted', False):
        state['alerted'] = True
        event = 'slow'
    elif state['good_runs'] >= 2 and state.get('alerted', False):
        state['alerted'] = False
        event = 'recovery'
    if not slow:
        state['history'] = (history + [values])[-24:]
    return state, event, reasons


def watch(snapshot, env, client=False):
    LIB.mkdir(parents=True, exist_ok=True)
    state_path = LIB / ('client-probe-state.json' if client else 'probe-state.json')
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    state = {'version': 2, 'paths': copy.deepcopy(previous.get('paths', {}))}
    messages = []
    for result in snapshot['results']:
        key = result['path'] + '/' + result['transport']
        old = state['paths'].get(key, {})
        health, event = notification_transition(old.get('health', {}), not result.get('web_check_ok', result['network_ok']))
        latency, latency_event, reasons = latency_transition(old.get('latency', {}), result)
        state['paths'][key] = {'health': health, 'latency': latency}
        result['latency_check'] = {'slow': bool(reasons), 'reasons': reasons}
        if event:
            messages.append(('🟠 Сетевая проверка дважды не прошла: ' if event == 'failure'
                             else '🟢 Сетевая проверка восстановилась: ') + key)
        if latency_event:
            messages.append(('🟡 Три проверки подряд с задержкой: ' + key + '; ' + ', '.join(reasons))
                            if latency_event == 'slow' else '🟢 Задержки нормализовались: ' + key)
    if messages:
        snapshot['notification_events'] = messages
    if messages and env.get('TG_BOT_TOKEN') and env.get('TG_CHAT_ID'):
        source = 'Проверка с Windows через Clash Mi.' if client else 'Проверка с NL-сервера, не с телефона/ПК.'
        message = '\n'.join(messages) + '\n' + source + ' Лента TikTok автоматически не проверяется. Лог: /var/log/safechill-probes.jsonl'
        payload = urllib.parse.urlencode({'chat_id': env['TG_CHAT_ID'], 'text': message}).encode()
        try:
            response = json.load(urllib.request.urlopen('https://api.telegram.org/bot' + env['TG_BOT_TOKEN'] + '/sendMessage', data=payload, timeout=8))
            if not response.get('ok'):
                raise RuntimeError('notification failed')
        except Exception:
            # Retry the same transition next run without logging a token-bearing URL.
            for key, path in state['paths'].items():
                old = previous.get('paths', {}).get(key, {})
                for kind in ('health', 'latency'):
                    path[kind]['alerted'] = old.get(kind, {}).get('alerted', False)
            snapshot['notification'] = 'failed; will retry'
    state['last'] = snapshot
    temp = state_path.with_suffix('.tmp')
    temp.write_text(json.dumps(state)); temp.chmod(0o600); os.replace(temp, state_path)
    with LOG.open('a', encoding='utf-8') as output:
        output.write(json.dumps(snapshot) + '\n')
    LOG.chmod(0o640)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', choices=['all', 'primary', 'standby', 'ru'], default='all')
    parser.add_argument('--transport', choices=['all', 'xhttp', 'tcp'], default='xhttp')
    parser.add_argument('--user')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--client-watch', action='store_true', help='ingest a Windows controller check over authenticated SSH')
    args = parser.parse_args()
    env = load_env(ETC / 'vpn.env')
    if not env.get('TG_CHAT_ID') and (ETC / 'tg_chat_id').exists():
        env['TG_CHAT_ID'] = (ETC / 'tg_chat_id').read_text().strip()
    if args.client_watch:
        incoming = json.loads(sys.stdin.read(32769))
        results = incoming['results']
        if not isinstance(results, list) or len(results) != 6:
            parser.error('expected six Windows IPv4 path checks')
        clean = []
        for result in results:
            if (result.get('path') not in ('windows-primary', 'windows-standby', 'windows-ru')
                    or result.get('transport') not in ('tcp', 'xhttp')
                    or type(result.get('network_ok')) is not bool
                    or type(result.get('delay_ms')) not in (int, float)
                    or not 0 <= result['delay_ms'] <= 120000):
                parser.error('invalid Windows path check')
            sanitized = {k: result[k] for k in ('path', 'transport', 'network_ok', 'delay_ms')}
            error = result.get('error_kind', '')
            if isinstance(error, str) and re.fullmatch(r'[A-Za-z0-9_]{1,40}', error):
                sanitized['error_kind'] = error
            if type(result.get('retry_ok')) is bool:
                sanitized['retry_ok'] = result['retry_ok']
            for key in ('test_elapsed_s', 'retry_delay_ms'):
                value = result.get(key)
                if type(value) in (int, float) and 0 <= value <= 120000:
                    sanitized[key] = value
            clean.append(sanitized)
        if len({(r['path'], r['transport']) for r in clean}) != 6:
            parser.error('duplicate Windows path check')
        snapshot = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'source': 'windows', 'app_feed_checked': False, 'results': clean}
        watch(snapshot, env, client=True)
        print(json.dumps(snapshot))
        return 0
    users = json.loads((ETC / 'users.json').read_text())
    if not users:
        parser.error('no configured user for an authenticated probe')
    user = next((u for u in users if u['name'] == args.user), None) if args.user else users[0]
    if not user:
        parser.error('unknown probe user')
    config = json.loads(Path('/usr/local/etc/xray/config.json').read_text())
    base = next((o for o in config['outbounds'] if o['tag'] == 'service-standby'), None)
    if base is None:
        secrets = load_env(ETC / 'secrets.env')
        base = {'tag': 'probe', 'protocol': 'vless', 'settings': {'vnext': [{
            'address': env['SERVER_IP'], 'port': 443,
            'users': [{'id': user['uuid'], 'encryption': 'none'}]}]},
            'streamSettings': {'network': 'xhttp', 'security': 'reality',
                'xhttpSettings': {'path': '/' + secrets['XHTTP_PATH'], 'mode': 'auto'},
                'realitySettings': {'serverName': env['DOMAIN'], 'fingerprint': 'firefox',
                    'publicKey': secrets['REALITY_PUB'], 'shortId': secrets['SHORT_ID']}}}
    paths = [('primary', env['SERVER_IP'], env['DOMAIN'])]
    if env.get('EXIT2_HOST'):
        paths += [('standby', env['EXIT2_HOST'], env['EXIT2_DOMAIN'])]
    if env.get('RU_HOST'):
        paths += [('ru', env['RU_HOST'], env.get('RU_SNI', 'yandex.ru'))]
    paths = [p for p in paths if args.path == 'all' or p[0] == args.path]
    transports = ['xhttp', 'tcp'] if args.transport == 'all' else [args.transport]
    tasks = [(*p, transport, user, env, base) for p in paths for transport in transports]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(probe, tasks))
    snapshot = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'source': 'server', 'results': results}
    if args.watch:
        watch(snapshot, env)
    print(json.dumps(snapshot))
    return 0 if all(r['web_check_ok'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())

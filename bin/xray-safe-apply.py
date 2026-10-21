#!/usr/bin/env python3
"""Validate as Xray's service user, publish atomically, and roll back a failed restart.

install FILE prepares a config without restarting (render.sh's local contract).
apply FILE also restarts; restart checks the current file before stopping Xray.
checkpoint records an already running, checked config as the rollback target.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time


class DeploymentError(RuntimeError):
    pass


class XrayDeployment:
    def __init__(self, config=Path('/usr/local/etc/xray/config.json'),
                 state=Path('/var/lib/safechill/xray'), settle=5):
        self.config = Path(config)
        self.state = Path(state)
        self.good = self.state / 'last-good.json'
        self.settle = settle

    def command(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise DeploymentError(f'{args[0]} failed: {(result.stderr or result.stdout)[-1200:].strip()}')
        return result.stdout.strip()

    def properties(self):
        names = ['User', 'Group', 'SupplementaryGroups', 'Environment', 'ActiveState', 'SubState', 'MainPID']
        result = self.command(['systemctl', 'show', 'xray', *['--property=' + n for n in names]])
        return dict(line.split('=', 1) for line in result.splitlines() if '=' in line)

    @contextmanager
    def locked(self):
        import fcntl  # Linux only; keep the module importable for offline tests elsewhere.
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        with (self.state / 'deploy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def atomic_write(path, data, mode):
        fd, name = tempfile.mkstemp(prefix='.safechill-', suffix='.json', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                out.flush()
                os.chmod(name, mode)
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def validate(self, path):
        # Parse without printing configuration contents (UUIDs and private keys).
        try:
            json.loads(path.read_bytes())
        except (ValueError, OSError) as error:
            raise DeploymentError(f'config is unreadable or invalid JSON: {type(error).__name__}') from error
        props = self.properties()
        user = props.get('User') or 'root'
        prefix = []
        if user != 'root':
            prefix = ['runuser', '-u', user]
            if props.get('Group'):
                prefix += ['-g', props['Group']]
            for group in props.get('SupplementaryGroups', '').split():
                prefix += ['-G', group]
            prefix += ['--']
        self.command(prefix + ['test', '-r', str(path)])
        environment = shlex.split(props.get('Environment', ''))
        self.command(prefix + ['env', *environment, '/usr/local/bin/xray', 'run', '-test', '-c', str(path)])

    def stable(self):
        first = self.properties()
        pid = first.get('MainPID')
        deadline = time.monotonic() + self.settle
        while True:
            props = self.properties()
            if (props.get('ActiveState') != 'active' or props.get('SubState') != 'running'
                    or not pid or pid == '0' or props.get('MainPID') != pid):
                raise DeploymentError('Xray did not keep the same running process during the startup check')
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        config = json.loads(self.config.read_bytes())
        for inbound in config.get('inbounds', []):
            network = inbound.get('streamSettings', {}).get('network', 'tcp')
            if network not in ('tcp', 'raw', 'xhttp', 'splithttp', 'ws', 'http', 'grpc', 'httpupgrade'):
                continue
            address = inbound.get('listen', '0.0.0.0')
            address = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(address, address)
            try:
                with socket.create_connection((address, int(inbound['port'])), timeout=2):
                    pass
            except (OSError, ValueError, KeyError) as error:
                raise DeploymentError('an expected Xray listener is unavailable') from error

    def checkpoint(self):
        self.validate(self.config)
        self.stable()
        self.atomic_write(self.good, self.config.read_bytes(), 0o600)

    def install(self, source):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        data = Path(source).read_bytes()
        # Test the exact final access mode in the final directory, as the service identity.
        fd, name = tempfile.mkstemp(prefix='.candidate-', suffix='.json', dir=self.config.parent)
        os.close(fd)
        candidate = Path(name)
        try:
            self.atomic_write(candidate, data, 0o644)
            self.validate(candidate)
            if not self.good.exists() and self.config.exists():
                props = self.properties()
                if props.get('ActiveState') == 'active' and props.get('SubState') == 'running':
                    self.checkpoint()
            self.atomic_write(self.config, data, 0o644)
        finally:
            candidate.unlink(missing_ok=True)

    def restart(self):
        # Failure here leaves the currently running process untouched.
        self.validate(self.config)
        expected = hashlib.sha256(self.config.read_bytes()).digest()
        try:
            self.command(['systemctl', 'restart', 'xray'])
            self.stable()
            if hashlib.sha256(self.config.read_bytes()).digest() != expected:
                raise DeploymentError('config changed during restart')
        except (DeploymentError, subprocess.SubprocessError, OSError) as error:
            if not self.good.exists():
                raise DeploymentError(f'{error}; no checked rollback config exists') from error
            self.atomic_write(self.config, self.good.read_bytes(), 0o644)
            self.validate(self.config)
            self.command(['systemctl', 'restart', 'xray'])
            self.stable()
            raise DeploymentError(f'{error}; previous checked config restored and running') from error
        self.atomic_write(self.good, self.config.read_bytes(), 0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'apply', 'restart', 'check', 'checkpoint'])
    parser.add_argument('source', nargs='?', type=Path)
    args = parser.parse_args()
    if args.action in ('install', 'apply') and args.source is None:
        parser.error('install/apply requires a candidate JSON file')
    if os.geteuid() != 0:
        parser.error('run as root')
    deploy = XrayDeployment()
    try:
        with deploy.locked():
            if args.action in ('install', 'apply'):
                deploy.install(args.source)
            if args.action in ('restart', 'apply'):
                deploy.restart()
            elif args.action == 'check':
                deploy.validate(deploy.config)
                deploy.stable()
            elif args.action == 'checkpoint':
                deploy.checkpoint()
        print(f'xray: {args.action} OK (checked as the service user)')
    except (DeploymentError, OSError, subprocess.SubprocessError) as error:
        print(f'xray: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

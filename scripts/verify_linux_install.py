"""Install/start/restart the Linux service on a disposable GitHub Actions runner only."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from verify_release import free_port, request


def verify(bundle):
    if os.name != 'posix' or os.geteuid() != 0 or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Use only as root on a disposable GitHub Actions Linux runner')
    bundle = Path(bundle).resolve()
    subprocess.run(['bash', str(bundle / 'install.sh'), str(bundle)], check=True)
    config = Path('/etc/chefas-proxy/chefas-proxy.json')
    environment = Path('/etc/chefas-proxy/chefas-proxy.env')
    assert stat.S_IMODE(environment.stat().st_mode) == 0o600
    values = dict(line.split('=', 1) for line in environment.read_text().splitlines()
                  if line and not line.startswith('#'))
    assert len(values['CHEFAS_PROXY_ADMIN_TOKEN']) >= 32
    original = environment.read_bytes()
    dashboard = free_port()
    listener = free_port()
    while dashboard == listener:
        listener = free_port()
    config.write_text(json.dumps({'pool_host': '127.0.0.1', 'pool_port': free_port(),
                                  'listen_host': '127.0.0.1', 'listen_port': listener,
                                  'dashboard_port': dashboard}))
    try:
        for operation in ('start', 'restart'):
            subprocess.run(['systemctl', operation, 'chefas-proxy'], check=True, timeout=30)
            for _ in range(100):
                try:
                    status, current = request(dashboard, '/api/config', values['CHEFAS_PROXY_ADMIN_TOKEN'])
                    if status == 200:
                        break
                except OSError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('Installed service did not become ready')
            assert current['fee_locked'] is True and current['fee_test'] is None
            assert environment.read_bytes() == original
            subprocess.run(['systemctl', 'is-active', '--quiet', 'chefas-proxy'], check=True)
    finally:
        subprocess.run(['systemctl', 'stop', 'chefas-proxy'], check=True, timeout=30)
    return {'linux_install': 'passed', 'private_credentials': 'passed',
            'systemd_start_restart_stop': 'passed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--report')
    args = parser.parse_args()
    report = verify(args.bundle)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))

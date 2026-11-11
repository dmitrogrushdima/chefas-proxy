"""Exercise a frozen operator release locally, without connecting an ASIC or public pool."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(port, route, token=None, data=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request('http://127.0.0.1:' + str(port) + route, headers=headers,
                                 data=json.dumps(data).encode() if data is not None else None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as reply:
            return reply.status, json.load(reply)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def verify(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / 'release-manifest.json').read_text())
    for name, metadata in manifest['files'].items():
        path = bundle / name
        value = os.readlink(path).encode() if 'symlink_sha256' in metadata else path.read_bytes()
        assert hashlib.sha256(value).hexdigest() == next(iter(metadata.values())), name
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('CHEFAS_PROXY_', 'PYTHON'))}
    with tempfile.TemporaryDirectory(prefix='chefas-release-check-') as directory:
        runtime = Path(directory) / 'release'
        shutil.copytree(bundle, runtime, symlinks=True)
        exe = runtime / ('chefas-proxy.exe' if os.name == 'nt' else 'chefas-proxy')
        def run(*args):
            return subprocess.run([str(exe), *args], cwd=runtime, env=environment,
                                  capture_output=True, text=True, timeout=30)
        assert run('--setup').returncode == 0
        env_file = runtime / '.env'
        secrets = dict(line.split('=', 1) for line in env_file.read_text().splitlines()
                       if line and not line.startswith('#'))
        assert len(secrets['CHEFAS_PROXY_ADMIN_TOKEN']) >= 32
        original = env_file.read_bytes()
        assert run('--setup').returncode == 0 and env_file.read_bytes() == original
        assert json.loads(run('--release-info').stdout)['frozen'] is True
        for override in (['--fee-test-worker', 'private.test'],
                         ['--devfee-worker', 'other.worker'], ['--tp-mode']):
            reply = run('--pool-host', '127.0.0.1', '--pool-port', '4444', *override)
            assert reply.returncode == 2 and 'locked' in reply.stderr
        config = runtime / 'chefas-proxy.json'
        config.write_text(json.dumps({'pool_host': '127.0.0.1', 'pool_port': 4444,
                                      'devfee_worker': 'other.worker'}))
        assert run('--config', str(config)).returncode == 2
        dashboard, stratum = free_port(), free_port()
        while dashboard == stratum:
            stratum = free_port()
        config.write_text(json.dumps({'pool_host': '127.0.0.1', 'pool_port': free_port(),
                                      'listen_host': '127.0.0.1', 'listen_port': stratum,
                                      'dashboard_port': dashboard}))
        admin = secrets['CHEFAS_PROXY_ADMIN_TOKEN']
        with (runtime / 'verification.log').open('wb') as output:
            process = subprocess.Popen([str(exe), '--config', str(config)], cwd=runtime,
                                       env=environment, stdout=output, stderr=output)
            try:
                for _ in range(100):
                    try:
                        status, current = request(dashboard, '/api/config', admin)
                        break
                    except OSError:
                        if process.poll() is not None:
                            raise RuntimeError('Frozen runtime exited before becoming ready')
                        time.sleep(.1)
                else:
                    raise RuntimeError('Frozen dashboard did not become ready')
                assert status == 200 and current['fee_locked'] is True
                assert current['fee_test'] is None and current['devfee_pool'] is None
                assert request(dashboard, '/api/config')[0] == 403
                assert request(dashboard, '/api/fee-test', admin,
                               {'worker': 'private.test', 'percent': 50})[0] == 404
                assert request(dashboard, '/api/config', admin, {'devfee_pool': None})[0] == 400
                assert request(dashboard, '/api/mode', admin, {'tp_mode': True})[0] == 400
                status, observer = request(dashboard, '/api/observer-links', admin, {})
                assert status == 200
                assert request(dashboard, '/api/snapshot', observer['token'])[0] == 200
                assert request(dashboard, '/api/fee-test', observer['token'],
                               {'worker': 'private.test', 'percent': 50})[0] == 403
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    return {'frozen_operator': 'passed', 'first_run': 'passed', 'private_credentials': 'passed',
            'fixed_fee_policy': 'passed', 'farm_admin_and_observer': 'passed', 'manifest': 'passed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--report')
    args = parser.parse_args()
    report = verify(args.bundle)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))

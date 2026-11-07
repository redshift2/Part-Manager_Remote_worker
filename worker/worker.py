#!/usr/bin/env python3
"""Parts Manager worker phase 2: signed job polling, isolated job execution and ZIP results."""
import base64
import hashlib
import subprocess
import tempfile
import zipfile
import shutil
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(os.environ.get('PM_WORKER_HOME', str(Path.home() / '.parts-manager-worker')))
ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
IDENTITY = ROOT / 'identity.json'
CONFIG = ROOT / 'config.json'


def save_private(path, data):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)


def signed_post(session, base, identity, key, action, payload_hash='-', *, params=None, json_body=None, data=None):
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    message = f"{identity['workerId']}:{timestamp}:{nonce}:{action}:{payload_hash}".encode()
    headers = {'X-Worker-Id': identity['workerId'], 'X-Worker-Timestamp': timestamp,
               'X-Worker-Nonce': nonce, 'X-Worker-Signature': base64.b64encode(key.sign(message)).decode()}
    response = session.post(base + '/serverAdmin/worker' + action.title(), params=params,
                            json=json_body, data=data, headers=headers, timeout=180)
    response.raise_for_status()
    return response


def safe_extract(archive, destination):
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 10000:
            raise ValueError('Too many ZIP entries')
        for entry in z.infolist():
            name = entry.filename
            if (name.startswith('/') or '\\' in name or '..' in name.split('/') or
                    entry.is_dir() and not name.endswith('/') or
                    (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe ZIP entry: ' + name)
            target = (destination / name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError('ZIP path escapes job directory')
        z.extractall(destination)


def report(session, base, identity, key, job_id, status, percent, message=''):
    message = message[:1800]
    payload = {'id': job_id, 'status': status, 'percent': percent, 'message': message}
    raw = f"{job_id}:{status}:{percent}:{message}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    signed_post(session, base, identity, key, 'progress', digest, json_body=payload)


def process_job(session, base, identity, key, job, config):
    job_id = int(job['id'])
    with tempfile.TemporaryDirectory(prefix=f'pm-job-{job_id}-') as temporary:
        root = Path(temporary)
        inputs = root / 'input'; inputs.mkdir()
        outputs = root / 'output'; outputs.mkdir()
        script = root / 'action.py'
        for kind, path in [('input', root / 'input.zip'), ('script', script)]:
            response = signed_post(session, base, identity, key, 'fetch', f'{job_id}:{kind}',
                                   params={'id': job_id, 'kind': kind})
            path.write_bytes(response.content)
        if hashlib.sha256(script.read_bytes()).hexdigest() != job['scriptSha256']:
            raise ValueError('Script SHA-256 mismatch')
        safe_extract(root / 'input.zip', inputs)
        report(session, base, identity, key, job_id, 'RUNNING', 10, 'Script and input downloaded')
        command = [config.get('python_executable', sys.executable), str(script), str(inputs), str(outputs)]
        if job['jobType'].startswith('CAD_') and config.get('freecad_executable'):
            command = [config['freecad_executable'], str(script), str(inputs), str(outputs)]
        # Script is administrator-controlled code. Use a dedicated unprivileged worker OS user.
        try:
            proc = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                  timeout=int(config.get('job_timeout_seconds', 1200)),
                                  env={**os.environ, 'PM_JOB_ID': str(job_id), 'PM_JOB_TYPE': job['jobType']})
            log = (proc.stdout + '\n' + proc.stderr)[-100000:]
            (outputs / 'execution.log').write_text(log)
            if proc.returncode:
                raise RuntimeError(f'Exit code {proc.returncode}: {log[-1000:]}')
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Job exceeded execution timeout') from exc
        report(session, base, identity, key, job_id, 'UPLOADING_RESULTS', 95, 'Uploading result ZIP')
        archive = root / 'result.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for path in outputs.rglob('*'):
                if path.is_file() and not path.is_symlink():
                    z.write(path, path.relative_to(outputs))
        data = archive.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        signed_post(session, base, identity, key, 'result', f'{job_id}:{digest}',
                    params={'id': job_id}, data=data)
        print('Completed job', job_id)


def main():
    if not CONFIG.exists():
        save_private(CONFIG, {'server_url': 'https://YOUR_SERVER', 'worker_name': socket.gethostname(),
                              'capabilities': ['ALL_JOBS', 'FREECAD'], 'max_concurrent_jobs': 1,
                              'poll_interval_seconds': 15})
        print('Edit configuration:', CONFIG)
        return 1
    config = json.loads(CONFIG.read_text())
    base = config['server_url'].rstrip('/')
    if not base.startswith('https://') and not os.environ.get('PM_WORKER_ALLOW_INSECURE_HTTP'):
        raise RuntimeError('HTTPS required (for local development only: PM_WORKER_ALLOW_INSECURE_HTTP=1)')
    if IDENTITY.exists():
        identity = json.loads(IDENTITY.read_text())
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(identity['privateKey']))
    else:
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        identity = {'privateKey': base64.b64encode(raw).decode(), 'workerId': None}
        save_private(IDENTITY, identity)
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    session = requests.Session()
    if not identity['workerId']:
        payload = {'name': config['worker_name'], 'publicKey': public,
                   'capabilities': config.get('capabilities', ['ALL_JOBS']),
                   'maxConcurrentJobs': config.get('max_concurrent_jobs', 1)}
        r = session.post(base + '/serverAdmin/registerWorker', json=payload, timeout=20)
        r.raise_for_status()
        response = r.json()
        identity['workerId'] = response['workerId']
        save_private(IDENTITY, identity)
        print('Registered worker:', identity['workerId'], 'status:', response['status'])
    print('Worker identity:', identity['workerId'])
    while True:
        heartbeat_ok = False
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        message = f"{identity['workerId']}:{timestamp}:{nonce}:heartbeat".encode()
        signature = base64.b64encode(key.sign(message)).decode()
        try:
            r = session.post(base + '/serverAdmin/workerHeartbeat', json={
                'workerId': identity['workerId'], 'timestamp': timestamp,
                'nonce': nonce, 'signature': signature}, timeout=20)
            if r.status_code == 403:
                print('Awaiting approval (or identity revoked).')
            else:
                r.raise_for_status()
                heartbeat_ok = True
                print('Heartbeat accepted:', time.strftime('%Y-%m-%d %H:%M:%S'))
        except requests.RequestException as e:
            print('Connection error:', e, file=sys.stderr)
        try:
            if heartbeat_ok:
                job = signed_post(session, base, identity, key, 'poll').json().get('job')
                if job:
                    print('Claimed job:', job['id'], job['jobType'])
                    try:
                        process_job(session, base, identity, key, job, config)
                    except Exception as exc:
                        print('Job failed:', exc, file=sys.stderr)
                        try:
                            report(session, base, identity, key, int(job['id']), 'FAILED', 0, str(exc))
                        except Exception as report_error:
                            print('Could not report failure:', report_error, file=sys.stderr)
        except requests.RequestException as exc:
            print('Polling error:', exc, file=sys.stderr)
        time.sleep(max(5, int(config.get('poll_interval_seconds', 15))))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nStopped')

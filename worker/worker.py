#!/usr/bin/env python3
"""Parts Manager worker phase 2: signed job polling, isolated job execution and ZIP results."""
import base64
import ast
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

WORKER_BUILD_VERSION = "1.3"

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


def signed_post(session, base, identity, key, action, payload_hash='-', *, params=None, json_body=None, data=None, timeout=180):
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    message = f"{identity['workerId']}:{timestamp}:{nonce}:{action}:{payload_hash}".encode()
    headers = {'X-Worker-Id': identity['workerId'], 'X-Worker-Timestamp': timestamp,
               'X-Worker-Nonce': nonce, 'X-Worker-Signature': base64.b64encode(key.sign(message)).decode()}
    response = session.post(base + '/serverAdmin/worker' + action.title(), params=params,
                            json=json_body, data=data, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response



def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def upload_result(session, base, identity, key, job_id, archive, config):
    """Send raw ZIP bytes using bounded memory, preserving the existing signed protocol.

    Each attempt opens the file afresh and creates a new signed request/nonce.
    A lost response may mean the server already accepted the result; the server
    must handle a repeated completion safely for automatic retries.
    """
    size = archive.stat().st_size
    digest = file_sha256(archive)
    retries = max(0, min(5, int(config.get('result_upload_retries', 2))))
    connect_timeout = max(5, int(config.get('result_upload_connect_timeout_seconds', 30)))
    read_timeout = max(60, int(config.get('result_upload_read_timeout_seconds', 3600)))
    print(f'Uploading job {job_id} result: {size} bytes, SHA-256 {digest}', flush=True)
    for attempt in range(retries + 1):
        try:
            with archive.open('rb') as stream:
                # A regular file is handled by requests as a streamed body with
                # Content-Length; no multipart encoding or in-memory ZIP copy.
                signed_post(session, base, identity, key, 'result', f'{job_id}:{digest}',
                            params={'id': job_id}, data=stream,
                            timeout=(connect_timeout, read_timeout))
            return
        except (requests.RequestException, OSError) as exc:
            if attempt == retries:
                raise
            delay = min(60, 5 * (2 ** attempt))
            print(f'Job {job_id} result upload attempt {attempt + 1} failed: {exc}; '
                  f'retrying in {delay}s', flush=True)
            time.sleep(delay)


def retain_failed_result(job_id, archive):
    """Keep generated ZIP outside the temporary job directory after failure."""
    if not archive.is_file():
        return
    destination_dir = ROOT / 'failed-results'
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = destination_dir / f'job-{job_id}-{int(time.time())}.zip'
    shutil.copyfile(archive, destination)
    destination.chmod(0o600)
    print(f'Job {job_id} result retained at {destination}', flush=True)

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


def execution_timeout_for_script(script_path, config, inputs=None):
    """Read the trusted, signed script's embedded JSON schema without executing it.

    A missing timeout uses the worker's configured default (normally 1200 seconds).
    Per-script overrides are capped at 24 hours.
    """
    default = int(config.get('job_timeout_seconds', 1200))
    source = script_path.read_text(encoding='utf-8-sig')
    tree = ast.parse(source, filename=str(script_path))
    schema_text = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == 'SCRIPT_SCHEMA_JSON'
                   for target in targets):
                try:
                    schema_text = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                    raise ValueError('SCRIPT_SCHEMA_JSON must be a literal JSON string')
    if schema_text is None:
        return default
    schema = json.loads(schema_text)
    if not isinstance(schema, dict):
        raise ValueError('SCRIPT_SCHEMA_JSON must contain a JSON object')
    value = schema.get('executionTimeoutSeconds')
    if value is None:
        # A schema field is user-configurable; read its submitted value from manifest.
        fields = schema.get('fields', [])
        timeout_field = next((f for f in fields if isinstance(f, dict) and f.get('name') == 'executionTimeoutSeconds'), None)
        if timeout_field is None:
            return default
        value = timeout_field.get('default', default)
        if inputs is not None and (inputs / 'manifest.json').is_file():
            manifest = json.loads((inputs / 'manifest.json').read_text(encoding='utf-8'))
            options = manifest.get('scriptOptions', {})
            if isinstance(options, str):
                options = json.loads(options)
            if isinstance(options, dict):
                value = options.get('executionTimeoutSeconds', value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86400:
        raise ValueError('executionTimeoutSeconds must be an integer from 1 to 86400')
    return value


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
        timeout_seconds = execution_timeout_for_script(script, config, inputs)
        print(f'Job {job_id} execution timeout: {timeout_seconds} seconds', flush=True)
        # Script is administrator-controlled code. Use a dedicated unprivileged worker OS user.
        try:
            proc = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                  timeout=timeout_seconds,
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
        try:
            upload_result(session, base, identity, key, job_id, archive, config)
        except Exception:
            try:
                retain_failed_result(job_id, archive)
            except Exception as preserve_error:
                print(f'Could not retain job {job_id} result: {preserve_error}', file=sys.stderr)
            raise
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
                   'maxConcurrentJobs': config.get('max_concurrent_jobs', 1),
                   'buildVersion': WORKER_BUILD_VERSION}
        r = session.post(base + '/serverAdmin/registerWorker', json=payload, timeout=20)
        r.raise_for_status()
        response = r.json()
        identity['workerId'] = response['workerId']
        save_private(IDENTITY, identity)
        print('Registered worker:', identity['workerId'], 'status:', response['status'])
    print('Worker identity:', identity['workerId'])
    print('Worker build version:', WORKER_BUILD_VERSION)
    while True:
        heartbeat_ok = False
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        message = f"{identity['workerId']}:{timestamp}:{nonce}:heartbeat".encode()
        signature = base64.b64encode(key.sign(message)).decode()
        try:
            r = session.post(base + '/serverAdmin/workerHeartbeat', json={
                'workerId': identity['workerId'], 'timestamp': timestamp,
                'nonce': nonce, 'signature': signature,
                'buildVersion': WORKER_BUILD_VERSION}, timeout=20)
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

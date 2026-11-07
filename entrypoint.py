#!/usr/bin/env python3
"""Initialize config only once; preserve existing identity and settings."""
import json
import os
import socket
from pathlib import Path

home = Path(os.environ.get('PM_WORKER_HOME', '/var/lib/pm-worker'))
home.mkdir(parents=True, exist_ok=True)
config = home / 'config.json'
if not config.exists():
    server = os.environ.get('PM_SERVER_URL', '').strip().rstrip('/')
    if not server.startswith('https://'):
        raise SystemExit('PM_SERVER_URL must be an HTTPS URL')
    settings = {
        'server_url': server,
        'worker_name': os.environ.get('PM_WORKER_NAME', socket.gethostname()),
        'capabilities': ['ALL_JOBS', 'FREECAD'],
        'max_concurrent_jobs': 1,
        'poll_interval_seconds': int(os.environ.get('PM_POLL_SECONDS', '15')),
        'job_timeout_seconds': 1200,
    }
    fd = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(settings, out, indent=2)
    print('Created persistent worker config:', config, flush=True)
else:
    print('Using existing persistent worker config:', config, flush=True)

os.execv('/opt/pm-venv/bin/python', ['/opt/pm-venv/bin/python', '/app/worker.py'])

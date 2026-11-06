"""Public static Mini App shell; no bot data or credentials are exposed."""
import os
from pathlib import Path

DEFAULT_APP_URL = 'https://luckyjet-signal-monitor-production.up.railway.app/app'


def app_url():
    return os.getenv('MINI_APP_URL', DEFAULT_APP_URL).strip()


def response(path):
    path = path.split('?', 1)[0]
    if path in {'/app', '/app/'}:
        return '200 OK', 'text/html; charset=utf-8', (Path(__file__).parent / 'web' / 'mini-app.html').read_bytes()
    if path in {'/', '/health'}:
        return '200 OK', 'application/json', b'{"status":"ok"}'
    return '404 Not Found', 'text/plain; charset=utf-8', b'Not found'

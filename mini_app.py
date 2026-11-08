"""Public static Mini App shell; no bot data or credentials are exposed."""
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_APP_URL = 'https://luckyjet-signal-monitor-production.up.railway.app/app'
APP_VERSION = '0.2.19'


def app_url():
    """Force a new Telegram WebView URL on each release, preserving custom URL options."""
    original = os.getenv('MINI_APP_URL', DEFAULT_APP_URL).strip()
    parts = urlsplit(original)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != 'v']
    params.append(('v', APP_VERSION))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def response(path):
    path = path.split('?', 1)[0]
    if path in {'/app', '/app/'}:
        shell = (Path(__file__).parent / 'web' / 'mini-app.html').read_bytes()
        return '200 OK', 'text/html; charset=utf-8', shell.replace(b'__APP_VERSION__', APP_VERSION.encode('ascii'))
    if path == '/app-version.json':
        return '200 OK', 'application/json; charset=utf-8', ('{"version":"' + APP_VERSION + '"}').encode('ascii')
    if path == '/summary.js':
        return '200 OK', 'application/javascript; charset=utf-8', (Path(__file__).parent / 'web' / 'summary.js').read_bytes()
    if path == '/history.js':
        return '200 OK', 'application/javascript; charset=utf-8', (Path(__file__).parent / 'web' / 'history.js').read_bytes()
    if path == '/controls.js':
        return '200 OK', 'application/javascript; charset=utf-8', (Path(__file__).parent / 'web' / 'controls.js').read_bytes()
    if path == '/admin.js':
        return '200 OK', 'application/javascript; charset=utf-8', (Path(__file__).parent / 'web' / 'admin.js').read_bytes()
    if path == '/site.js':
        return '200 OK', 'application/javascript; charset=utf-8', (Path(__file__).parent / 'web' / 'site.js').read_bytes()
    if path == '/access.js':
        return '200 OK', 'application/javascript; charset=utf-8', (Path(__file__).parent / 'web' / 'access.js').read_bytes()
    if path in {'/', '/health'}:
        return '200 OK', 'application/json', b'{"status":"ok"}'
    return '404 Not Found', 'text/plain; charset=utf-8', b'Not found'

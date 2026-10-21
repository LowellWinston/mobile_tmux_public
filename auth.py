"""Sign-in for the web page.

Anyone who reaches this server can type into your agents and run shell commands, so it is never open by default:
- `token` (default): one access token, created on first run and kept in the config folder; signing in sets a cookie.
- `proxy`: a reverse proxy in front already signs people in (nginx auth_request, Tailscale serve, Cloudflare Access, ...).
  The server then only accepts connections from this machine, so nothing can reach it around the proxy.
"""
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time

import paths

MODE = os.environ.get('MOBILE_TMUX_AUTH', 'token')
COOKIE = 'mobile_tmux_session'
MAX_AGE = 30 * 86400
TOKEN_FILE = paths.CONFIG / 'token'
LIMIT, WINDOW = 5, 300   # failed sign-ins allowed per client per window (seconds)
_failures = {}
_token = [None, '']
_lock = threading.Lock()


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def check_startup(host):
    """Refuse a setup that would leave the server open."""
    if MODE not in ('token', 'proxy'):
        raise SystemExit('MOBILE_TMUX_AUTH must be "token" or "proxy".')
    if MODE == 'proxy' and not is_loopback(host):
        raise SystemExit('MOBILE_TMUX_AUTH=proxy trusts whatever reaches the server, so MOBILE_TMUX_HOST must be a loopback address.')


def token():
    """The access token; created (owner-only file) the first time it is needed."""
    try:
        stamp = TOKEN_FILE.stat().st_mtime_ns
        if _token[0] != stamp:  # re-read only when the file changed
            value = TOKEN_FILE.read_text().strip()
            _token[:] = [stamp, value]
        if _token[1]:
            return _token[1]
    except OSError:
        pass
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = secrets.token_urlsafe(24)
    descriptor = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(value + '\n')
    return value


def session_value(secret=None):
    return hmac.new((secret or token()).encode(), b'mobile-tmux-session', hashlib.sha256).hexdigest()


def cookie_header(secure):
    return '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s' % (COOKIE, session_value(), MAX_AGE, '; Secure' if secure else '')


def signed_in(cookie_text):
    """True when the Cookie header carries a valid session (always true in proxy mode)."""
    if MODE == 'proxy':
        return True
    for part in (cookie_text or '').split(';'):
        name, _, value = part.strip().partition('=')
        if name == COOKIE and hmac.compare_digest(value.encode(), session_value().encode()):
            return True
    return False


def attempt(client, candidate):
    """Check a submitted token; 'ok', 'wrong' or 'blocked' (too many recent failures from this client)."""
    now = time.time()
    with _lock:
        recent = [moment for moment in _failures.get(client, []) if now - moment < WINDOW]
        if len(recent) >= LIMIT:
            _failures[client] = recent
            return 'blocked'
        if isinstance(candidate, str) and hmac.compare_digest(candidate.strip().encode(), token().encode()):
            _failures.pop(client, None)
            return 'ok'
        _failures[client] = recent + [now]
    return 'wrong'


def same_origin(origin, host):
    """The Origin header must name this very host (http or https); a different site cannot post here."""
    from urllib.parse import urlsplit
    parts = urlsplit(origin or '')
    return parts.scheme in ('http', 'https') and parts.netloc == host


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mobile Tmux</title><style>
:root{color-scheme:dark light}body{margin:0;min-height:100dvh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;font:16px system-ui,sans-serif}
form{width:min(340px,86vw);display:grid;gap:12px}h1{font-size:1.2rem;margin:0}input,button{font:inherit;padding:12px;border-radius:8px;border:1px solid #30363d}
input{background:#161b22;color:inherit}button{background:#2f81f7;color:#fff;border:0}p{margin:0;color:#f85149;min-height:1.2em;font-size:.9rem}
@media(prefers-color-scheme:light){body{background:#fff;color:#1f2328}input{background:#f6f8fa}}
</style></head><body><form method="post" action="/mobile/login"><h1>Mobile Tmux</h1>
<input name="token" type="password" placeholder="Access token" autocomplete="current-password" autofocus required>
<button>Sign in</button><p>__ERROR__</p></form>
<script>var t=location.hash.slice(1);if(t){history.replaceState(null,'',location.pathname);document.querySelector('input').value=decodeURIComponent(t);document.querySelector('form').submit()}</script>
</body></html>"""

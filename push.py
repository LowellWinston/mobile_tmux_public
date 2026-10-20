"""Web Push notifications for agent state changes (finished, needs approval, needs input)."""
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
import paths

# A private copy of pywebpush lives here; system packages keep priority.
sys.path.append(str(paths.HOME / '.local/share/mobile-tmux/pylib'))
try:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
except ImportError:  # Notifications are optional; the rest of the app must keep working without them.
    webpush = None

KEYS = paths.private_file('push_keys.json')
STORE = paths.CACHE / 'push_subscriptions.json'
SUBJECT = os.environ.get('MOBILE_TMUX_PUSH_SUBJECT') or 'mailto:mobile-tmux@localhost'
PUSH_SERVICES = ('googleapis.com', 'push.services.mozilla.com', 'push.apple.com', 'notify.windows.com')
LOCK = threading.Lock()


def available():
    return webpush is not None


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def vapid_keys(path=KEYS):
    """The server's VAPID identity, created on first use and kept private to the owner."""
    with LOCK:
        try:
            stored = json.loads(path.read_text())
            return stored['private_pem'], stored['public']
        except (OSError, ValueError, KeyError):
            pass
        vapid = Vapid02()
        vapid.generate_keys()
        pem = vapid.private_pem().decode()
        public = b64url(vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'private_pem': pem, 'public': public}))
        temporary.chmod(0o600)
        temporary.replace(path)
        return pem, public


def valid_subscription(value):
    if not isinstance(value, dict) or not isinstance(value.get('endpoint'), str) or len(value['endpoint']) > 1000:
        return False
    keys = value.get('keys')
    if not isinstance(keys, dict) or not all(isinstance(keys.get(name), str) and 0 < len(keys[name]) < 200 for name in ('p256dh', 'auth')):
        return False
    parts = urlsplit(value['endpoint'])
    host = parts.hostname or ''
    # The server will POST to this address, so only the browsers' own push services are acceptable.
    return parts.scheme == 'https' and any(host == service or host.endswith('.' + service) for service in PUSH_SERVICES)


def load(store=STORE):
    try:
        data = json.loads(store.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data, store=STORE):
    store.parent.mkdir(parents=True, exist_ok=True)
    temporary = store.with_suffix('.tmp')
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o600)
    temporary.replace(store)


def subscribe(subscription, store=STORE):
    if not valid_subscription(subscription):
        raise ValueError('Invalid push subscription.')
    with LOCK:
        data = load(store)
        data[subscription['endpoint']] = {'subscription': {'endpoint': subscription['endpoint'], 'keys': subscription['keys']},
                                          'created': int(time.time())}
        save(data, store)


def unsubscribe(endpoint, store=STORE):
    with LOCK:
        data = load(store)
        if data.pop(endpoint, None) is not None:
            save(data, store)


def send_all(payload, urgent=False, store=STORE, sender=None):
    """Deliver one notification to every subscribed browser; forget subscriptions the push service has dropped."""
    sender = sender or webpush
    if sender is None:
        return 0
    pem, _ = vapid_keys()
    delivered = 0
    for endpoint, record in list(load(store).items()):
        try:
            sender(subscription_info=record['subscription'], data=json.dumps(payload), vapid_private_key=Vapid02.from_pem(pem.encode()),
                   vapid_claims={'sub': SUBJECT}, ttl=3600, headers={'Urgency': 'high' if urgent else 'normal'}, timeout=10)
            delivered += 1
        except WebPushException as exc:
            if getattr(exc.response, 'status_code', None) in (404, 410):
                unsubscribe(endpoint, store)
        except (OSError, ValueError):
            continue
    return delivered


class Notifier:
    """Turns agent state changes into notifications, once per change and never while the page is being watched."""

    MESSAGES = {'done': 'Finished', 'approval': 'Needs approval', 'input': 'Needs your input'}

    def __init__(self, send, watching=lambda session: False, settle=4, minimum_work=5, clock=time.monotonic):
        self.send, self.watching, self.settle, self.minimum_work, self.clock = send, watching, settle, minimum_work, clock
        self.seen = {}

    def observe(self, entry, status):
        if not isinstance(status, dict):
            return
        state, now = status.get('state'), self.clock()
        record = self.seen.setdefault(entry['id'], {'state': state, 'since': now, 'worked': None, 'sent': None})
        if state != record['state']:
            record['state'], record['since'] = state, now
        if state == 'Working':
            record['worked'] = record['worked'] if record['worked'] is not None else now
            record['sent'] = None
            return
        kind, token = None, None
        if state == 'Approval':
            kind, token = 'approval', (status.get('approval') or {}).get('id')
        elif state == 'Input needed':
            kind, token = 'input', (status.get('attention') or {}).get('id')
        elif state == 'Idle' and record['worked'] is not None and record['since'] - record['worked'] >= self.minimum_work:
            kind = 'done'
        if state == 'Idle' and kind is None and record['worked'] is not None and now - record['since'] >= self.settle:
            record['worked'] = None  # a turn too short to be worth a notification
        if kind is None or now - record['since'] < self.settle or record['sent'] == (kind, token):
            return
        record['sent'] = (kind, token)
        if kind == 'done':
            record['worked'] = None
        if self.watching(entry['id']):
            return
        self.send({'title': entry.get('label') or 'Agent', 'body': self.MESSAGES[kind], 'tag': 'session-' + entry['id'],
                   'url': '/mobile/#session=' + quote(entry['id'], safe='')}, kind != 'done')

"""Mobile transcript and message bridge to one existing Codex CLI thread."""
from datetime import datetime
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from agents import claude_snapshot, discover, usage
from agents import records
from antigravity import snapshot as antigravity_snapshot, window_active
from terminal_input import DraftError, clear_draft as terminal_clear_draft, type_text, live_status, capture, capture_screen, submit as terminal_submit, respond_approval, start_agent, create_session, shell_snapshot, shell_submit, verify
from quota_notices import error_message, merge as merge_quota_notices
import activity
import history
import model_picker
import push
import shortcuts
import shell_log
import terminal_replies
import uploads
import auth
import select
import socket
import paths

ROOT = Path(__file__).resolve().parent
KEEPALIVE = 10
UI_ASSETS = {name: (ROOT / name).read_text() for name in ('index.html', 'app.js')}
STATIC_ASSETS = {'/mobile/' + p.name: p.read_bytes() for p in [*(ROOT / 'vendor').glob('*.js'), ROOT / 'sw.js', ROOT / 'manifest.webmanifest', *ROOT.glob('*.png')]}
TERMINAL_ASSETS = {'html': (ROOT / 'termius.html').read_text(), 'js': (ROOT / 'termius.js').read_text()}
CONFIG = paths.private_file('default_conversation.json')
TTYD_PORT = int(os.environ.get('MOBILE_TMUX_TTYD_PORT', '7682'))
SEND_LOCK = threading.Lock()
WATCHING = {}
WATCHING_LOCK = threading.Lock()


def watching(session):
    with WATCHING_LOCK:
        return WATCHING.get(session, 0) > 0


def notify(payload, urgent):
    threading.Thread(target=push.send_all, args=(payload, urgent), daemon=True).start()


NOTIFIER = push.Notifier(notify, watching)


def monitor_notices():
    """Keep native quota notices even when the phone page is closed."""
    while True:
        try:
            entries = sessions()
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            entries = []
        for entry in entries:
            try:
                if entry.get('owner_pid') and entry.get('writable'):
                    NOTIFIER.observe(entry, shared_status(entry))
            except Exception:  # one odd pane must never stop the monitor for everyone
                continue
        time.sleep(2)


def display_text(text):
    def fence(value, language=''):
        delimiter = '`' * max(3, max((len(part) + 1 for part in re.findall(r'`+', value)), default=3))
        return delimiter + language + '\n' + value + '\n' + delimiter

    def shell_command(match):
        command, result = match[1].strip(), match[2].strip()
        ticks = '`' * max(1, max((len(part) + 1 for part in re.findall(r'`+', command)), default=1))
        rendered = '! ' + ticks + ' ' + command + ' ' + ticks if '\n' not in command else '!\n\n' + fence(command, 'sh')
        exit_code = re.search(r'(?m)^Exit code:\s*(-?\d+)', result)
        output_header = re.search(r'(?m)^Output:[ \t]*(?:\r?\n)?', result)
        output = result[output_header.end():].strip('\r\n') if output_header else ''
        if output:
            rendered += '\n\n' + fence(output, 'text')
        if exit_code and exit_code[1] != '0':
            rendered += '\n\nExit code: ' + exit_code[1]
        elif not output_header and not exit_code and result:
            rendered += '\n\n' + fence(result, 'text')
        return rendered

    text = re.sub(r'<user_shell_command>\s*<command>(.*?)</command>\s*<result>(.*?)</result>\s*</user_shell_command>',
                  shell_command, text, flags=re.DOTALL)
    def answer(match):
        try:
            reply = json.loads(match.group(1))
            return '\n'.join(item['answer'] for item in reply
                             if isinstance(item, dict) and isinstance(item.get('answer'), str))
        except (ValueError, TypeError):
            return ''
    return re.sub(r'<send_user_message_question_reply>(.*?)</send_user_message_question_reply>',
                  answer, text, flags=re.DOTALL)


def settings():
    """The saved default conversation (optional: a fresh install has none)."""
    try:
        saved = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        saved = {}
    return {'thread': saved.get('thread'), 'transcript': saved.get('transcript')}


STATUS_CACHE = {}  # entry id -> (when, status)
STATUS_LOCK = threading.Lock()


def shared_status(entry):
    """live_status for the session list and the monitor, which would otherwise each re-read the pane every few seconds."""
    with STATUS_LOCK:
        when, status = STATUS_CACHE.get(entry['id'], (0.0, None))
        if time.monotonic() - when > 1:
            try:
                status = live_status(entry)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):  # a pane that just closed must not fail the whole tab list
                status = {'state': 'Unavailable', 'approval': None}
            STATUS_CACHE[entry['id']] = (time.monotonic(), status)
        return status


SESSIONS_CACHE = [0.0, []]
SESSIONS_LOCK = threading.Lock()


def sessions():
    """Discovery shells out (ps, tmux, sqlite); every open page and the monitor ask every 2s, so share one result for a second."""
    with SESSIONS_LOCK:
        if time.monotonic() - SESSIONS_CACHE[0] > 1:
            SESSIONS_CACHE[1] = discover(settings(), subprocess.run)
            SESSIONS_CACHE[0] = time.monotonic()
        return SESSIONS_CACHE[1]


def choose(identifier=None):
    entries = sessions()
    if identifier is None:
        if not entries:
            raise ValueError('No tmux sessions found. Start one (tmux new -s work) and run an agent in it.')
        return next((entry for entry in entries if entry.get('thread') and entry.get('thread') == settings()['thread']), entries[0])
    entry = next((entry for entry in entries if entry['id'] == identifier), None)
    if not entry:
        raise ValueError('That session has ended. Select another tab.')
    return entry


CODEX_COMMAND = re.compile(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*")')
QUIET_CALLS = {'wait', 'sleep'}


def codex_action_lines(payload):
    """One short line per command or tool call in a Codex response item."""
    name = payload.get('name') or 'tool'
    raw = payload.get('input') if payload.get('type') == 'custom_tool_call' else payload.get('arguments')
    if not isinstance(raw, str):
        return ['Ran a script' if name == 'exec' else name]
    commands = []
    for found in CODEX_COMMAND.findall(raw):
        try:
            commands.append(json.loads(found))
        except ValueError:
            continue
    if not commands and payload.get('type') == 'function_call':
        try:
            arguments = json.loads(raw)
            commands = [arguments.get('cmd') or arguments.get('command')] if isinstance(arguments, dict) else []
        except ValueError:
            pass
    commands = [command for command in commands if isinstance(command, str) and command.strip()]
    if commands:
        return ['Ran ' + command.strip().split('\n')[0] for command in commands]
    return [] if name in QUIET_CALLS else ['Ran a script' if name == 'exec' else name]


def codex_state():
    return {'messages': [], 'dropped': 0, 'busy': False, 'usage': None, 'model': ''}


def codex_consume(state, index, entry):
    """Fold one rollout record into the chat messages, busy flag and latest usage figures."""
    messages = state['messages']
    payload = entry.get('payload', {})
    kind = payload.get('type')
    if entry.get('type') == 'turn_context' and payload.get('model'):
        state['model'] = str(payload['model']) + (' ' + str(payload['effort']) if payload.get('effort') else '')
    if entry.get('type') == 'event_msg':
        if kind in ('error', 'warning'):
            text = error_message(payload)
            if text:
                messages.append({'id': str(index), 'role': 'assistant', 'kind': 'notice',
                                 'text': text, 'time': entry.get('timestamp')})
        if kind == 'token_count':
            info = payload.get('info') or {}
            tokens = (info.get('last_token_usage') or {}).get('total_tokens')
            limit = info.get('model_context_window')
            rates = payload.get('rate_limits') or {}
            windows = [{'label': f"{value.get('window_minutes', 0) / 60:g}h", 'percent': value.get('used_percent'),
                        'resets_at': value.get('resets_at')}
                       for key in ('primary', 'secondary') if isinstance((value := rates.get(key)), dict)]
            state['usage'] = ({'percent': tokens / limit * 100 if tokens is not None and limit else None,
                               'tokens': tokens, 'limit': limit}, windows, entry.get('timestamp'))
        if kind == 'task_started':
            state['busy'] = True
        elif kind in ('task_complete', 'turn_aborted'):
            state['busy'] = False
    if entry.get('type') == 'compacted' or (entry.get('type') == 'response_item' and kind == 'compaction'):
        messages.append(activity.notice(index, entry.get('timestamp'), 'Context compacted.'))
        return
    if entry.get('type') == 'event_msg' and kind == 'turn_aborted':
        messages.append(activity.notice(index, entry.get('timestamp'), 'Turn ' + str(payload.get('reason') or 'aborted') + '.'))
        return
    if entry.get('type') == 'response_item' and kind in ('custom_tool_call', 'function_call'):
        for position, line in enumerate(codex_action_lines(payload)):
            messages.append(activity.action(f'{index}.{position}', entry.get('timestamp'), line))
        return
    if entry.get('type') != 'response_item' or kind != 'message':
        return
    role = payload.get('role')
    if role not in ('user', 'assistant'):
        return
    text = '\n'.join(item.get('text', '') for item in payload.get('content', [])
                     if item.get('type') in ('input_text', 'output_text', 'text'))
    text = display_text(text)
    if not text.strip() or text.startswith('# AGENTS.md instructions'):
        return
    messages.append({'id': str(index), 'role': role, 'text': text,
                     'time': entry.get('timestamp'), 'phase': payload.get('phase')})


def claude_cwd(entry):
    """Where a Claude or Antigravity session is working now: its transcript records the folder, which changes as it moves around."""
    try:
        with open(entry['transcript'], 'rb') as handle:
            size = handle.seek(0, 2)
            window = 131072
            pattern = rb'"Cwd":"(?:\\")?(/[^"\\]+)' if entry.get('agent') == 'antigravity' else rb'"cwd":"((?:[^"\\]|\\.)*)"'
            while True:  # a long run of tool output can push the last folder record far from the end: widen until found
                handle.seek(max(0, size - window))
                found = re.findall(pattern, handle.read())
                if found or window >= size or window >= 33554432:
                    break
                window *= 4
        return json.loads('"' + found[-1].decode('utf-8', 'ignore') + '"') if found else None
    except (OSError, KeyError, ValueError):
        return None


def codex_cwd(entry):
    """Where a Codex session works now: each turn records its folder, so a /cd shows from the next turn on."""
    try:
        with open(entry['transcript'], 'rb') as handle:
            size = handle.seek(0, 2)
            window = 262144
            while True:  # long tool output can push the last turn record far from the end, so widen the search until it is found
                handle.seek(max(0, size - window))
                found = re.findall(rb'"type":"turn_context","payload":\{[^\n]*?"cwd":"((?:[^"\\]|\\.)*)"', handle.read())
                if found or window >= size or window >= 33554432:
                    break
                window *= 4
        return json.loads('"' + found[-1].decode('utf-8', 'ignore') + '"') if found else None
    except (OSError, KeyError, ValueError):
        return None


def antigravity_model(entry, usage):
    """agy's status report can be minutes behind a model change, but its history notes every change on the next message
    ("changed setting `Model Selection` from A to B"); when that is newer than the report, show B."""
    try:
        reported = (usage or {}).get('reported_at') or 0
        path = entry['transcript']
        if os.stat(path).st_mtime <= reported:
            return None
        with open(path, 'rb') as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - 262144))
            found = re.findall(rb'Model Selection` from .{1,120}? to (.{1,120}?)\.\s+No need', handle.read())
        return found[-1].decode('utf-8', 'ignore') if found else None
    except (OSError, KeyError, ValueError, TypeError):
        return None


def antigravity_cwd(entry):
    """Antigravity reports its folder with every status update; its transcript only sometimes records one."""
    try:
        reported = json.loads((paths.CACHE / 'antigravity' / (entry['thread'] + '.json')).read_text())
        return reported.get('cwd') or None
    except (OSError, KeyError, ValueError):
        return None


def agent_folder(entry):
    """Where the agent itself says it is working."""
    agent = entry.get('agent')
    found = (claude_cwd(entry) if agent in ('claude', 'antigravity') else codex_cwd(entry) if agent == 'codex' else None) \
        or (antigravity_cwd(entry) if agent == 'antigravity' else None)
    return found or entry.get('thread_cwd') or entry.get('cwd')


def tab_folder(entry):
    """The folder a tab shows: where the agent works, except that a `!cd` made in the tab sticks until the agent moves somewhere else
    (agents often snap back to their start folder after a command, which must not undo the user's cd)."""
    folder = agent_folder(entry)
    shell = shell_log.moved(entry['id'])
    if shell and shell[3] in (None, str(entry.get('process_start'))) and (shell[2] is None or shell[2] == folder):
        return shell[0]
    return folder


def home_path(path):
    """A folder as shown to people: <home>/x becomes ~/x."""
    if not path:
        return ''
    home = str(Path.home())
    return '~' + path[len(home):] if path == home or path.startswith(home + '/') else path


RATES_FILE = paths.CACHE / 'codex_rates.json'
LAST_RATES = [None]
FOOTER_MODEL = re.compile(r'^\s*(\S+)\s+(minimal|low|medium|high|xhigh|extra high)\s+·\s+Context', re.M | re.I)
FOOTER_CONTEXT = re.compile(r'Context\s+(\d+(?:\.\d+)?)%\s+(left|used)')


def recent_rates(limit=6, tail=400_000):
    """The newest allowance report found in the most recent Codex history files, as (windows, reported_at)."""
    files = sorted((paths.CODEX / 'sessions').glob('**/rollout-*.jsonl'), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
    for path in files:
        try:
            with open(path, 'rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - tail))
                lines = stream.read().decode('utf-8', 'ignore').splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            rates = (entry.get('payload') or {}).get('rate_limits') or {}
            windows = [{'label': f"{value.get('window_minutes', 0) / 60:g}h", 'percent': value.get('used_percent'), 'resets_at': value.get('resets_at')}
                       for key in ('primary', 'secondary') if isinstance((value := rates.get(key)), dict)]
            if windows:
                return windows, entry.get('timestamp')
    return [], None


def fresh_codex_screen(screen):
    """True when the pane shows Codex's start banner and nothing said yet (a new session, or one just cleared with /clear)."""
    lines = [line.strip() for line in screen.splitlines()]
    if not any('OpenAI Codex' in line for line in lines):
        return False
    return not any(line.startswith('•') for line in lines) and sum(line.startswith('›') for line in lines) <= 1


def codex_metrics(config, reported_usage, model=''):
    """Rings for a Codex tab. Allowances are account-wide, so a tab that has not reported yet reuses the last report seen;
    the context figure falls back to the terminal footer ("Context 97% left") until the first turn is recorded."""
    context, windows, reported = reported_usage or ({}, [], None)
    inherited = False
    if windows:
        saved = json.dumps({'windows': windows, 'reported_at': reported})
        if (str(RATES_FILE), saved) != LAST_RATES[0] or not RATES_FILE.exists():  # rewrite if the file moved or was deleted
            try:
                RATES_FILE.parent.mkdir(parents=True, exist_ok=True)
                RATES_FILE.write_text(saved)
                LAST_RATES[0] = (str(RATES_FILE), saved)
            except OSError:
                pass
    else:
        try:
            saved = json.loads(RATES_FILE.read_text())
            windows, reported, inherited = saved['windows'], saved.get('reported_at'), True
        except (OSError, ValueError, KeyError, TypeError):
            windows, reported = recent_rates()
            inherited = bool(windows)
    try:
        screen = capture(config)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        screen = ''
    shown = FOOTER_MODEL.search(screen)  # the footer is the live value; the history only knows the model of the last turn
    if shown:
        model = shown[1] + ' ' + shown[2].lower()
    if context.get('percent') is None:
        try:
            match = FOOTER_CONTEXT.search(screen)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            match = None
        if match:
            value = float(match[1])
            context = {'percent': 100 - value if match[2] == 'left' else value, 'tokens': None, 'limit': None}
    report = usage(context, windows, reported)
    report['model'] = model
    if inherited:
        stamp = reported
        try:
            stamp = datetime.fromisoformat(str(reported).replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
        report.update(allowance_inherited=any(report[key] for key in ('five_hour', 'weekly')), allowance_reported_at=stamp)
    return report


def snapshot(config=None):
    config = config or settings()
    if config.get('agent') == 'claude':
        terminal_replies.capture_banner(config, lambda entry: capture_screen(entry, 2000))  # the banner scrolls away in a long session
        data = claude_snapshot(config, display_text)
        data = dict(data, messages=terminal_replies.merge(config, data['messages']))
        return dict(data, thread=config['thread'], agent='claude', writable=config['writable'], session=config['id'])
    if config.get('agent') == 'antigravity' and config.get('transcript'):
        terminal_replies.capture_banner(config, capture)
        data = antigravity_snapshot(config, display_text, records, usage)
        shown = antigravity_model(config, data.get('usage'))
        if shown:
            windows = [dict(w, active=window_active(w['label'], shown)) for w in data['usage']['windows']]
            data = dict(data, usage=dict(usage(data['usage']['context'], windows, data['usage']['reported_at']), model=shown))
        return dict(data, messages=terminal_replies.merge(config, data['messages'])) if isinstance(data.get('messages'), list) else data
    if config.get('agent') in ('shell', 'antigravity') or config.get('ambiguous'):
        return {'messages': [], 'thread': None, 'agent': config['agent'], 'busy': False,
                'writable': False, 'session': config['id'], 'usage': usage(),
                'unsupported': ('Multiple Codex sessions share this directory; their conversation mapping is ambiguous. Use Termius until each is mapped explicitly.'
                                if config.get('ambiguous') else ''
                                if config.get('agent') == 'shell' else 'This session does not expose a supported chat transcript. Use Termius for this session.')}
    terminal_replies.capture_banner(config, capture)
    path = Path(config['transcript'])
    metrics = usage()
    if not path.is_file():
        return {'messages': terminal_replies.merge(config, merge_quota_notices(dict(config, agent='codex'), [])), 'busy': False, 'thread': config['thread'], 'usage': codex_metrics(config, None),
                'agent': 'codex', 'writable': config.get('writable', True), 'session': config.get('id')}
    state = history.read(path, codex_state, codex_consume)
    messages, busy = state['messages'], state['busy']
    try:
        cleared = fresh_codex_screen(capture(config))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        cleared = False
    if cleared:  # /clear starts a new conversation that has no history file yet; the old one must not show through
        messages, busy = [], False
        if state['usage']:
            state['usage'] = ({'percent': 0, 'tokens': 0, 'limit': state['usage'][0].get('limit')}, *state['usage'][1:])
    metrics = codex_metrics(config, state['usage'], state.get('model', ''))
    return {'messages': terminal_replies.merge(config, merge_quota_notices(dict(config, agent='codex'), activity.group_actions(activity.merge(list(messages))))), 'dropped': state['dropped'], 'busy': busy, 'thread': config['thread'], 'usage': metrics,
            'agent': 'codex', 'writable': config.get('writable', True), 'session': config.get('id')}


def submit(message, config=None):
    config = config or settings()
    if not config.get('writable', True):
        raise RuntimeError('Messaging is unavailable for this session. Use Termius.')
    with SEND_LOCK:
        terminal_submit(config, message)


DEFAULT_WINDOW = 150
MAX_WINDOW = 2000


def window_limit(query):
    try:
        return max(20, min(int(query.get('limit', [DEFAULT_WINDOW])[0]), MAX_WINDOW))
    except ValueError:
        return DEFAULT_WINDOW


def chat_payload(config, shell_mode=False, limit=DEFAULT_WINDOW):
    """One chat view: the latest `limit` messages plus how many older ones were left out."""
    shell = config['agent'] == 'shell' and shell_mode
    state = {'state': 'Shell', 'approval': None} if shell else live_status(config)
    data = shell_snapshot(config) if shell else snapshot(config)
    if not shell and isinstance(data.get('messages'), list):
        data = dict(data, messages=shell_log.merge(config['id'], data['messages']))
    if data.get('busy') and state['state'] == 'Idle':
        state['state'] = 'Working'
    messages = data.get('messages')
    earlier = max(0, len(messages) - limit) if isinstance(messages, list) else 0
    dropped = data.pop('dropped', 0) if isinstance(data, dict) else 0
    if earlier:
        data = dict(data, messages=messages[-limit:])
    return dict(data, earlier=earlier + dropped, limit=limit, max_limit=MAX_WINDOW, **state)


def change_signature(config):
    """Cheap fingerprint of everything a chat view is built from: history file, tmux screen, agent status file."""
    parts = []
    for path in (config.get('transcript'), config.get('registry')):
        try:
            info = os.stat(path)
            parts.append((info.st_mtime_ns, info.st_size))
        except (OSError, TypeError):
            parts.append(None)
    try:
        parts.append(hashlib.sha1(capture(config).encode()).hexdigest())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        parts.append(None)
    return tuple(parts)


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # a client that stalls mid-request must not hold a thread forever

    def respond(self, status, body, content_type='application/json', cache='no-store'):
        data = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def stream_chat(self, identifier, shell_mode, limit=DEFAULT_WINDOW, lifetime=600):
        """Server-sent events: push a fresh chat payload whenever something the chat view depends on changes."""
        config = choose(identifier)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        watched = config.get('id')
        with WATCHING_LOCK:
            WATCHING[watched] = WATCHING.get(watched, 0) + 1
        started = discovered = full = wrote = changed = time.monotonic()
        signature = last_body = None
        try:
            self.wfile.write(b'retry: 2000\n\n')
            while time.monotonic() - started < lifetime:
                now = time.monotonic()
                try:
                    if now - discovered >= 2:
                        config, discovered = choose(identifier), now
                    current = change_signature(config)
                    if current != signature:
                        changed = now
                    if current != signature or now - full >= 5:
                        signature, full = current, now
                        body = json.dumps(chat_payload(config, shell_mode, limit))
                        if body != last_body:
                            last_body = body
                            self.wfile.write(('data: ' + body + '\n\n').encode())
                            wrote = now
                except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
                    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                        raise
                    self.wfile.write(b'event: unavailable\ndata: {}\n\n')
                    wrote = now
                    time.sleep(1)
                if now - wrote >= KEEPALIVE:  # a named event: the page counts it as proof the stream is alive (comments are invisible to it)
                    self.wfile.write(b'event: idle\ndata: {}\n\n')
                    wrote = now
                self.wfile.flush()
                time.sleep(.4 if now - changed < 10 else .8)  # idle chats poll tmux half as often
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        finally:
            with WATCHING_LOCK:
                WATCHING[watched] = max(0, WATCHING.get(watched, 1) - 1)

    def deny(self):
        """Answer and return True when the request is not signed in."""
        if auth.signed_in(self.headers.get('Cookie')):
            return False
        if self.command == 'GET' and not self.path.startswith('/mobile/api/'):
            self.send_response(302)
            self.send_header('Location', '/mobile/login')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.respond(401, {'error': 'Sign in required.'})
        return True

    def login(self, error=''):
        self.respond(200 if not error else 401, auth.LOGIN_PAGE.replace('__ERROR__', error), 'text/html')

    def do_login(self):
        if not auth.same_origin(self.headers.get('Origin', ''), self.headers.get('Host', '')):
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0') or 0)
        except ValueError:
            size = 0
        if not 0 < size <= 1024:
            self.respond(400, {'error': 'Invalid request.'})
            return
        candidate = parse_qs(self.rfile.read(size).decode('utf-8', 'ignore')).get('token', [''])[0]
        outcome = auth.attempt(auth.client_id(self.client_address[0], self.headers.get('X-Forwarded-For')), candidate)
        if outcome == 'ok':
            secure = self.headers.get('X-Forwarded-Proto') == 'https' or self.headers.get('Origin', '').startswith('https://')
            self.send_response(303)
            self.send_header('Set-Cookie', auth.cookie_header(secure))
            self.send_header('Location', '/mobile/')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.login('Too many attempts. Try again in a few minutes.' if outcome == 'blocked' else 'That token is not right.')

    def proxy_terminal(self):
        """Pass /terminal/live/ (ttyd, the full interactive terminal) through to the local ttyd, WebSocket included, after sign-in."""
        origin = self.headers.get('Origin')
        if origin and not auth.same_origin(origin, self.headers.get('Host', '')):
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        try:
            upstream = socket.create_connection(('127.0.0.1', TTYD_PORT), timeout=5)
        except OSError:
            self.respond(503, {'error': 'The live terminal is not running. Start ttyd (see the README) to use Interact.'})
            return
        upgrading = 'upgrade' in self.headers.get('Connection', '').lower()
        lines = ['%s %s HTTP/1.1' % (self.command, self.path)]
        lines += ['%s: %s' % (key, value) for key, value in self.headers.items()
                  if key.lower() not in ('cookie', 'connection') or (key.lower() == 'connection' and upgrading)]
        if not upgrading:
            lines.append('Connection: close')
        with upstream:
            upstream.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
            upstream.settimeout(None)
            pair = {self.connection: upstream, upstream: self.connection}
            try:
                while True:
                    ready, _, _ = select.select(list(pair), [], [], 600)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        pair[source].sendall(data)
            except OSError:
                return

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path.startswith('/terminal/live/'):
            if not self.deny():
                self.proxy_terminal()
            return
        if path == '/mobile/login':
            self.login()
            return
        if path not in ('/mobile/manifest.webmanifest', '/mobile/icon-192.png', '/mobile/icon-512.png', '/mobile/apple-touch-icon.png') and self.deny():
            return
        try:
            if path in ('/mobile/terminal/', '/mobile/terminal'):
                self.respond(200, TERMINAL_ASSETS['html'], 'text/html')
            elif path == '/mobile/termius.js':
                self.respond(200, TERMINAL_ASSETS['js'], 'text/javascript')
            elif path in ('/', '/mobile/', '/mobile/chat/', '/mobile/chat'):
                self.respond(200, UI_ASSETS['index.html'], 'text/html')
            elif path == '/mobile/app.js':
                self.respond(200, UI_ASSETS['app.js'], 'text/javascript')
            elif path.startswith('/mobile/vendor/') and '/mobile/' + path.rsplit('/', 1)[1] in STATIC_ASSETS:
                self.respond(200, STATIC_ASSETS['/mobile/' + path.rsplit('/', 1)[1]], 'text/javascript', 'max-age=86400')
            elif path == '/mobile/sw.js':  # no cache: browsers must see service-worker updates
                self.respond(200, STATIC_ASSETS[path], 'text/javascript')
            elif path == '/mobile/manifest.webmanifest':
                self.respond(200, STATIC_ASSETS[path], 'application/manifest+json', 'max-age=86400')
            elif path in ('/mobile/icon-192.png', '/mobile/icon-512.png', '/mobile/apple-touch-icon.png'):
                self.respond(200, STATIC_ASSETS[path], 'image/png', 'max-age=86400')
            elif path == '/mobile/api/shortcuts':
                self.respond(200, {'shortcuts': shortcuts.load()})
            elif path == '/mobile/api/push/config':
                self.respond(200, {'supported': push.available(), 'publicKey': push.vapid_keys()[1] if push.available() else None})
            elif path == '/mobile/api/chat':
                query = parse_qs(urlsplit(self.path).query)
                self.respond(200, chat_payload(choose(query.get('session', [None])[0]), query.get('mode') == ['shell'], window_limit(query)))
            elif path == '/mobile/api/screen':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                try:
                    lines = int(query.get('lines', [80])[0])
                except ValueError:
                    lines = 80
                self.respond(200, {'text': capture_screen(config, lines), 'session': config['id'], 'agent': config['agent']})
            elif path == '/mobile/api/stream':
                query = parse_qs(urlsplit(self.path).query)
                self.stream_chat(query.get('session', [None])[0], query.get('mode') == ['shell'], window_limit(query))
            elif path == '/mobile/api/sessions':
                self.respond(200, {'sessions': [dict({key: entry.get(key) for key in ('id', 'tmux_session', 'label', 'agent', 'thread', 'writable')}, dir=home_path(tab_folder(entry)), **shared_status(entry))
                                               for entry in sessions()], 'default_thread': settings()['thread']})
            elif path == '/mobile/api/tmux-sessions':
                result = subprocess.run([paths.TMUX, 'list-sessions', '-F', '#{session_id}\t#{session_name}\t#{session_windows}'],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode:
                    self.respond(200, {'sessions': []})
                else:
                    active = [line.split('\t', 2) for line in result.stdout.splitlines()]
                    self.respond(200, {'sessions': [{'id': item[0], 'name': item[1], 'windows': int(item[2])}
                                                     for item in active if len(item) == 3 and re.fullmatch(r'\$[0-9]+', item[0])]})
            else:
                self.respond(404, {'error': 'Not found'})
        except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            self.respond(503, {'error': 'The conversation is currently unavailable.'})

    def do_POST(self):
        if self.path == '/mobile/login':
            self.do_login()
            return
        if self.path not in ('/mobile/api/message', '/mobile/api/approval', '/mobile/api/start', '/mobile/api/session', '/mobile/api/tmux-sessions', '/mobile/api/key', '/mobile/api/clear-draft', '/mobile/api/type',
                         '/mobile/api/push/subscribe', '/mobile/api/push/unsubscribe', '/mobile/api/push/test', '/mobile/api/shortcuts', '/mobile/api/model-options', '/mobile/api/model', '/mobile/api/rename', '/mobile/api/shell', '/mobile/api/upload'):
            self.respond(404, {'error': 'Not found'})
            return
        # Reject unauthenticated and cross-origin submissions.
        if self.deny():
            return
        origin = self.headers.get('Origin', '')
        host = self.headers.get('Host', '')
        if not auth.same_origin(origin, host) or self.headers.get('X-Codex-Mobile') != '1':
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        if self.path == '/mobile/api/upload':
            self.do_upload()
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= (262144 if self.path == '/mobile/api/shortcuts' else 65536):
                self.respond(413, {'error': 'Message is too large.'})
                return
            body = json.loads(self.rfile.read(size))
            message = body.get('message') if isinstance(body, dict) else None
            if not isinstance(body, dict):
                raise ValueError('Invalid request.')
            if self.path == '/mobile/api/message' and (not isinstance(message, str) or not message.strip()):
                self.respond(400, {'error': 'Enter a message first.'})
                return
            if self.path == '/mobile/api/shortcuts':
                try:
                    self.respond(200, {'shortcuts': shortcuts.save(body.get('shortcuts'))})
                except ValueError as exc:
                    self.respond(400, {'error': str(exc)})
                return
            if self.path == '/mobile/api/shell':
                command = body.get('command')
                if not isinstance(command, str):
                    raise ValueError('Type a command after the !.')
                config = choose(body.get('session'))
                start = agent_folder(config)
                result = shell_log.run(config['id'], command, start, agent_folder(config), str(config.get('process_start')))
                self.respond(200, {'accepted': True, 'cwd': shell_log.short(shell_log.current(config['id'], start) or '')})
                return
            if self.path == '/mobile/api/rename':
                pane, name = body.get('pane'), body.get('name')
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    self.respond(400, {'error': 'Select an active chat tab.'})
                    return
                if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
                    self.respond(400, {'error': 'Use letters, numbers, - or _ (up to 64), starting with a letter or number.'})
                    return
                with SEND_LOCK:
                    result = subprocess.run([paths.TMUX, 'rename-session', '-t', pane, name], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    self.respond(409, {'error': 'That name is already used.' if 'duplicate' in result.stderr else 'Could not rename the session.'})
                    return
                self.respond(200, {'session': pane, 'name': name})
                return
            if self.path == '/mobile/api/session':
                with SEND_LOCK:
                    pane = create_session(body.get('name'), str(paths.HOME))
                self.respond(200, {'session': pane, 'label': body['name']})
                return
            if self.path == '/mobile/api/tmux-sessions':
                with SEND_LOCK:
                    pane = create_session(body.get('name'), str(paths.HOME))
                    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'],
                                            check=True, capture_output=True, text=True, timeout=5)
                self.respond(201, {'session': result.stdout.strip(), 'name': body['name']})
                return
            if self.path.startswith('/mobile/api/push/'):
                if not push.available():
                    raise RuntimeError('Push notifications are not installed on the server.')
                if self.path.endswith('/subscribe'):
                    push.subscribe(body.get('subscription'))
                elif self.path.endswith('/unsubscribe'):
                    if isinstance(body.get('endpoint'), str):
                        push.unsubscribe(body['endpoint'])
                else:
                    delivered = push.send_all({'title': 'Tmux Mobile', 'body': 'Notifications are working.', 'tag': 'test', 'url': '/mobile/'})
                    self.respond(200, {'accepted': True, 'delivered': delivered})
                    return
                self.respond(200, {'accepted': True})
                return
            config = choose(body.get('session'))
            if body.get('thread') != config.get('thread'):
                self.respond(409, {'error': 'This session changed conversations. Reload its tab before sending.'})
                return
            if self.path == '/mobile/api/message' and config['agent'] == 'shell' and body.get('mode') == 'shell':
                with SEND_LOCK:
                    shell_submit(config, message.strip())
                self.respond(200, {'accepted': True, 'delivery': 'shell'})
                return
            if self.path == '/mobile/api/start':
                with SEND_LOCK:
                    start_agent(config, body.get('agent'))
                self.respond(200, {'accepted': True})
                return
            if not config.get('writable'):
                raise RuntimeError('Messaging is unavailable for this session.')
            if self.path in ('/mobile/api/model-options', '/mobile/api/model'):
                with SEND_LOCK:
                    if self.path == '/mobile/api/model-options':
                        result = model_picker.options(config)
                    else:
                        model, effort = body.get('model'), body.get('effort')
                        if not isinstance(model, str) or not model or not (effort is None or isinstance(effort, str)):
                            raise ValueError('Choose a model first.')
                        result = model_picker.apply(config, model, effort or None)
                self.respond(200, result)
                return
            if self.path == '/mobile/api/key':
                key = body.get('key')
                if key not in ('Up', 'Down', 'Left', 'Right', 'Enter', 'Escape', 'Tab', 'Space', 'y', 'n', *'123456789'):
                    self.respond(400, {'error': 'Invalid key.'})
                    return
                with SEND_LOCK:
                    verify(config)
                    subprocess.run([paths.TMUX, 'send-keys', '-t', config['id'], key],
                                   check=True, capture_output=True, timeout=5)
                self.respond(200, {'accepted': True})
                return
            if self.path == '/mobile/api/type':
                with SEND_LOCK:
                    type_text(config, body.get('text'), enter=body.get('enter') is True)
                self.respond(200, {'accepted': True})
                return
            if self.path == '/mobile/api/clear-draft':
                with SEND_LOCK:
                    terminal_clear_draft(config)
                self.respond(200, {'accepted': True})
                return
            if self.path == '/mobile/api/approval':
                with SEND_LOCK:
                    respond_approval(config, body.get('approval'), body.get('action'), choice=body.get('choice'))
                self.respond(200, {'accepted': True})
            else:
                state = live_status(config)['state']
                if config['agent'] == 'codex' and re.match(r'/ls(\s|$)', message.strip()):
                    message = '!' + message.strip()[1:]   # Codex has no /ls; "!ls" runs it in the shell
                wants_reply = (config['agent'] == 'codex' and state != 'Working'  # while it works, new lines are its answer, not the command's
                               and message.strip().startswith('/') and message.strip() != '/model')
                before = capture(config) if wants_reply else ''
                note = '' if message.strip().startswith(('/', '!')) else shell_log.folder_note(config['id'], agent_folder(config))
                submit(note + message.strip(), config)
                if wants_reply:  # Codex answers its slash commands on the terminal only
                    threading.Thread(target=terminal_replies.record, args=(config, before, capture), daemon=True).start()
                self.respond(200, {'accepted': True, 'delivery': 'queued' if state == 'Working' else 'submitted'})
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {'error': str(exc) if self.path in ('/mobile/api/session', '/mobile/api/model', '/mobile/api/shell') else 'Invalid message.'})
        except (OSError, RuntimeError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            self.respond(503, {'error': str(exc) if isinstance(exc, RuntimeError)
                               else 'Could not confirm delivery. Check the terminal before retrying.',
                               'draft': isinstance(exc, DraftError)})

    def do_upload(self):
        """A raw file body for one session; the path of the saved copy goes back so the page can put it in the message."""
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= uploads.LIMIT:
                self.close_connection = True
                self.respond(413, {'error': 'That file is empty or too large (the limit is %d MB).' % (uploads.LIMIT // 1024 // 1024)})
                return
            config = choose(self.headers.get('X-Session'))
            if self.headers.get('X-Thread') != config.get('thread'):
                self.respond(409, {'error': 'This session changed conversations. Reload its tab before attaching.'})
                return
            if not config.get('writable') or config['agent'] == 'shell':
                raise RuntimeError('Attachments need a running agent in this session.')
            data = self.rfile.read(size)
            target = uploads.save(unquote(self.headers.get('X-Filename', '')), data)
            self.respond(200, {'path': str(target), 'name': target.name, 'size': len(data)})
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {'error': str(exc)})
        except (OSError, RuntimeError) as exc:
            self.respond(503, {'error': str(exc) if isinstance(exc, RuntimeError) else 'Could not save that file.'})

    def do_DELETE(self):
        if self.path not in ('/mobile/api/tmux-sessions', '/mobile/api/session'):
            self.respond(404, {'error': 'Not found'})
            return
        if self.deny():
            return
        origin = self.headers.get('Origin', '')
        host = self.headers.get('Host', '')
        if not auth.same_origin(origin, host) or self.headers.get('X-Codex-Mobile') != '1':
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                self.respond(413, {'error': 'Invalid request.'})
                return
            body = json.loads(self.rfile.read(size))
            if self.path == '/mobile/api/session':
                pane = body.get('pane') if isinstance(body, dict) else None
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    self.respond(400, {'error': 'Select an active chat tab.'})
                    return
                with SEND_LOCK:
                    active_panes = subprocess.run([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id}'],
                                                  check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
                    if pane not in active_panes:
                        self.respond(404, {'error': 'That chat tab has already ended.'})
                        return
                    subprocess.run([paths.TMUX, 'kill-pane', '-t', pane], check=True,
                                   capture_output=True, timeout=5)
                self.respond(200, {'closed': pane})
                return
            identifier = body.get('session') if isinstance(body, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r'\$[0-9]+', identifier):
                self.respond(400, {'error': 'Select an active tmux session.'})
                return
            with SEND_LOCK:
                active = subprocess.run([paths.TMUX, 'list-sessions', '-F', '#{session_id}'],
                                        check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
                if identifier not in active:
                    self.respond(404, {'error': 'That tmux session has already ended.'})
                    return
                subprocess.run([paths.TMUX, 'kill-session', '-t', identifier], check=True,
                               capture_output=True, timeout=5)
            self.respond(200, {'closed': identifier})
        except (ValueError, UnicodeError):
            self.respond(400, {'error': 'Invalid request.'})
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            self.respond(503, {'error': 'Could not close the tmux session.'})


if __name__ == '__main__':
    host = os.environ.get('MOBILE_TMUX_HOST', '127.0.0.1')
    port = int(os.environ.get('MOBILE_TMUX_PORT', '7683'))
    auth.check_startup(host)
    if auth.MODE == 'token':
        print('Mobile Tmux listening on http://%s:%d/mobile/ -- sign in with the token in %s' % (host, port, auth.TOKEN_FILE), flush=True)
        auth.token()
    threading.Thread(target=monitor_notices, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()

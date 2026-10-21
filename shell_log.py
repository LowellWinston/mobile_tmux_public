"""Run a chat line that starts with "!" as a real shell command on the server and keep the result for the chat.

The agent never sees these commands, so every agent behaves the same, and a `cd` sticks for that tab: the folder is
remembered between commands (each one is its own shell process, started in the remembered folder)."""
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import paths
from activity import insert_by_time

CACHE = paths.CACHE / 'shell'
HOME = str(Path.home())
TIMEOUT = 20
MAX_OUTPUT = 20000
KEEP = 40
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
LOCK = threading.Lock()
_running = {}


def _file(pane):
    return CACHE / (re.sub(r'[^0-9A-Za-z]', '_', str(pane)) + '.json')


def load(pane):
    try:
        value = json.loads(_file(pane).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(pane, state):
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = _file(pane)
    temporary = target.with_name(target.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    temporary.replace(target)


# ~/.bashrc stops early for non-interactive shells and starts a tmux menu for interactive ones, so neither can run here:
# take just its alias lines, so "!dust" or "!ll" mean what they do in the terminal.
ALIASES = ("shopt -s expand_aliases\n"
           "for __f in ~/.bashrc ~/.bash_aliases; do [ -f \"$__f\" ] && eval \"$(grep -E '^[[:space:]]*alias[[:space:]]' \"$__f\")\"; done\n")


def short(path):
    return '~' + path[len(HOME):] if path == HOME or path.startswith(HOME + '/') else path


def run(pane, command, start, agent_folder=None, owner=None):
    """Run `command` in the tab's folder (`start` until a cd says otherwise). Returns the chat message that was recorded."""
    command = command.strip()
    if not command:
        raise ValueError('Type a command after the !.')
    if len(command) > 4000:
        raise ValueError('That command is too long.')
    with LOCK:
        gate = _running.setdefault(pane, threading.Lock())
    if not gate.acquire(blocking=False):
        raise ValueError('A command is still running in this tab.')
    try:
        state = load(pane)
        folder = state.get('cwd') if os.path.isdir(state.get('cwd') or '') else start if os.path.isdir(start or '') else HOME
        token = uuid.uuid4().hex
        script = ALIASES + command + '\n__rc=$?\nprintf "\\n' + token + '%s\\n" "$PWD"\nexit $__rc'
        env = dict(os.environ, TERM='dumb', PAGER='cat', GIT_PAGER='cat', GIT_TERMINAL_PROMPT='0')
        process = subprocess.Popen(['/bin/bash', '-lc', script], cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        stopped = False
        try:
            raw, _ = process.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            stopped = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:  # it exited between the timeout and the kill
                pass
            raw, _ = process.communicate()
        text = ANSI.sub('', raw.decode('utf-8', 'replace')).replace('\r\n', '\n')
        arrived = folder
        found = text.rfind(token)
        if found != -1:
            arrived = text[found + len(token):].split('\n', 1)[0].strip() or folder
            text = text[:found].rstrip('\n')
        text = text.rstrip()
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + '\n… output cut at %d characters' % MAX_OUTPUT
        lines = [short(folder) + ' $ ' + command]
        if text:
            lines.append(text)
        if stopped:
            lines.append('[stopped after %d s]' % TIMEOUT)
        elif process.returncode:
            lines.append('[exit %d]' % process.returncode)
        if arrived != folder:
            lines.append('→ ' + short(arrived))
        message = {'id': 'shell:' + uuid.uuid4().hex[:12], 'role': 'assistant', 'kind': 'terminal', 'text': '\n'.join(lines),
                   'time': datetime.now(timezone.utc).isoformat()}
        state['cwd'] = arrived if os.path.isdir(arrived) else folder
        state['moved_at'] = time.time()  # every ! command fixes the tab's folder: the agent may not know about it, but this tool does
        state['agent_at_move'], state['moved_process'] = agent_folder, owner  # the agent's own folder then; the tab follows ! until the agent moves
        state['entries'] = (state.get('entries', []) + [message])[-KEEP:]
        _save(pane, state)
        return message
    finally:
        gate.release()


def moved(pane):
    """(folder, when) of the last `!cd` in this tab, or None."""
    state = load(pane)
    folder = state.get('cwd')
    return (folder, state['moved_at'], state.get('agent_at_move'), state.get('moved_process')) if folder and state.get('moved_at') and os.path.isdir(folder) else None


def folder_note(pane, agent_folder):
    """Once per `!cd`, a line telling the agent where the tab now works (it never sees `!` commands); '' if nothing to say."""
    state = load(pane)
    folder, when = state.get('cwd'), state.get('moved_at')
    if not folder or not when or not os.path.isdir(folder) or folder == agent_folder or state.get('told_at') == when:
        return ''
    _save(pane, dict(state, told_at=when))
    return '[Working directory changed to %s. Run commands and resolve relative paths there.] ' % folder


def current(pane, start=None):
    """The folder `!` commands will run in for this tab."""
    folder = load(pane).get('cwd')
    return folder if folder and os.path.isdir(folder) else start


def merge(pane, messages):
    """Put saved shell results among the chat messages by the time they ran."""
    entries = load(pane).get('entries') or []
    if not entries:
        return messages

    return insert_by_time(messages, entries)

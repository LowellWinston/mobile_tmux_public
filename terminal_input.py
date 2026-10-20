"""Guarded input and visible permission prompts for live agent tmux panes."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

from antigravity import process_start, ROOT as AGY_ROOT, CACHE as AGY_CACHE, capture_notices
from quota_notices import capture as capture_quota_notices
import paths

LAUNCHERS = {'codex': 'codex',
             'claude': 'claude',
             'antigravity': 'agy'}


def create_session(name, cwd, run=subprocess.run):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('Use letters, numbers, - or _ for the session name (up to 64 characters).')
    result = run([paths.TMUX, 'new-session', '-d', '-s', name, '-c', cwd, '-P', '-F', '#{pane_id}'],
                 capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('A session with that name already exists.' if 'duplicate session' in result.stderr
                           else 'Could not create the tmux session.')
    pane = result.stdout.strip()
    if not re.fullmatch(r'%\d+', pane):
        raise RuntimeError('Session creation could not be confirmed. Refresh the tabs before retrying.')
    return pane


def capture(entry, run=subprocess.run, styled=False):
    return run([paths.TMUX, 'capture-pane', *(['-e'] if styled else []), '-p', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout


def capture_screen(entry, lines=80, run=subprocess.run):
    """The last `lines` lines of the pane (scrollback included), for the read-only live screen view."""
    lines = max(20, min(int(lines), 500))
    text = run([paths.TMUX, 'capture-pane', '-p', '-t', entry['id'], '-S', '-' + str(lines)],
               check=True, capture_output=True, text=True, timeout=5).stdout
    return '\n'.join(line.rstrip() for line in text.rstrip().splitlines()[-lines:])[-200000:]


def approval(screen):
    # Expose the native chooser; every submitted choice requires an explicit
    # user selection tied to this exact request. Never auto-accept a default.
    lines = screen.splitlines()
    questions = [i for i, line in enumerate(lines) if re.search(
        r'^\s*(?:[│┃╭╰┌└]\s*)?(?:Quick safety check|Run this command\?|Would you like to \w+|Do you want to (?:proceed|allow|approve|make this edit|create)|Do you trust|Trust this (?:folder|directory|project)|Allow access to|Allow this|(?:trust.{0,100}(?:folder|directory|project)|(?:folder|directory|project).{0,100}trust))', line, re.I)]
    if not questions:
        return None
    start = questions[-1]
    trust = bool(re.search(r'Quick safety check|trust.*(?:folder|directory|project)|(?:folder|directory|project).*trust', lines[start], re.I))
    choices = []
    choice_numbers = []
    for i, line in enumerate(lines[start + 1:], start + 1):
        match = re.match(r'^\s*([>❯›]?)\s*(\d+)\.\s+(.+)', line)
        if match:
            choices.append((i, bool(match[1]), match[3]))
            choice_numbers.append(int(match[2]))
        elif trust and (match := re.match(r'^\s*([>❯›]?)\s*((?:Yes|No|Trust|Don.t trust|Do not trust|Exit)\b.*)', line, re.I)):
            choices.append((i, bool(match[1]), match[2]))
            choice_numbers.append(len(choices))
    reject = r'(?:No\b|Reject\b|Cancel\b|Exit\b|Don.t trust\b|Do not trust\b)'
    if not choices or not any(re.match(reject, c[2], re.I) for c in choices):
        return None
    selected = next((i for i, c in enumerate(choices) if c[1]), None)
    once = next((i for i, c in enumerate(choices) if re.match(r'Yes\b', c[2], re.I)
                 and not re.search(r'always|don.t ask|remember', c[2], re.I)), None)
    # Fingerprint includes the visible tool request, independent of cursor position.
    preceding = [i for i in range(start) if re.search(r'^\s*(?:Requesting permission for:|Command|\$ )', lines[i])]
    begin = preceding[-1] if preceding else max(0, start - 15)
    full_text = '\n'.join(lines[begin:choices[-1][0] + 1]).strip()
    text = '\n'.join(lines[begin:choices[0][0]]).strip()
    options = []
    numbers = []
    for index, (line, _, label) in enumerate(choices):
        number = choice_numbers[index]
        numbers.append(number)
        end = choices[index + 1][0] if index + 1 < len(choices) else line + 1
        continuation = [part.strip() for part in lines[line + 1:end] if part.strip()]
        options.append({'id': number, 'text': ' '.join([label, *continuation])})
    canonical = re.sub(r'(?m)^\s*[>❯›]?\s*(\d+)\.\s+', r'\1. ', full_text)
    if trust:
        canonical = re.sub(r'(?m)^[ \t]*[>❯›]?[ \t]*((?:Yes|No|Trust|Don.t trust|Do not trust|Exit)\b.*)$', r'\1', full_text)
    return {'id': hashlib.sha256(canonical.encode()).hexdigest(), 'text': text,
            'actionable': selected is not None, 'options': options,
            '_numbers': numbers,
            '_selected': selected, '_once': once,
            '_reject': next(i for i, c in enumerate(choices) if re.match(reject, c[2], re.I))}


ATTENTION_DELAY = 3
_attention_seen = {}
NUMBERED_ITEM = re.compile(r'^\s*[>❯›]?\s*\d+[.)]\s+\S')
CURSOR_ITEM = re.compile(r'^\s*[>❯›]\s*\d+[.)]\s+\S')
INPUT_HINT = re.compile(r'esc Close|↑/↓ Scroll|Enter to (?:select|confirm|continue)|↑/?↓ (?:to )?(?:navigate|select)|enter Select|Esc to cancel|press enter|press any key|'
                        r'\(y/n\)|\[y/n\]|\(yes/no\)', re.I)


RULE_LINE = re.compile(r'^[\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║-]*$')


def menu_question(lines):
    """Split a numbered terminal menu into its question and options so a phone can show it as a chat card, not a screen dump."""
    first = next((i for i, line in enumerate(lines) if NUMBERED_ITEM.match(line)), None)
    if first is None:
        return None
    clean = lambda line: re.sub(r'^\s*[│┃]\s*|\s*[│┃]\s*$', '', line).strip()
    above, index = [], first - 1
    while index >= 0 and not clean(lines[index]):  # the paragraph right above the options, not the chat before it
        index -= 1
    while index >= 0 and clean(lines[index]) and not RULE_LINE.match(lines[index]):
        above.insert(0, clean(lines[index]))
        index -= 1
    question = re.sub(r'^Question \d+/\d+:\s*', '', ' '.join(above))
    options = []
    for line in lines[first:]:
        match = re.match(r'^\s*([>❯›]?)\s*(\d+)[.)]\s+(.*\S)', line)
        if match:
            options.append({'id': int(match[2]), 'text': match[3].strip(), 'detail': '', 'selected': bool(match[1])})
        elif options and clean(line) and not RULE_LINE.match(line) and not INPUT_HINT.search(line):
            options[-1]['detail'] = (options[-1]['detail'] + ' ' + clean(line)).strip()
    return {'question': question[-400:], 'options': options} if len(options) >= 2 else None


def attention(entry, screen, working, now=None):
    """Catch-all for any screen that waits on the user but is not a recognized approval.

    Returns the visible screen once the same situation has lasted ATTENTION_DELAY seconds, so redraws and
    transient states never flash a panel.
    """
    now = time.monotonic() if now is None else now
    lines = screen.rstrip().splitlines()
    tail = lines[-30:]
    marker = {'claude': '❯', 'codex': '›', 'antigravity': '>'}.get(entry['agent'])
    reason = None
    numbered = [line for line in tail if NUMBERED_ITEM.match(line)]
    if len(numbered) >= 2 and any(CURSOR_ITEM.match(line) for line in tail):
        reason = 'menu'
    elif any(INPUT_HINT.search(line) for line in lines[-8:]):
        reason = 'prompt'
    elif marker and not working and lines and not any(line.startswith(marker) for line in lines[-15:]):
        reason = 'no-input-box'
    key = entry['id']
    if not reason:
        _attention_seen.pop(key, None)
        return None
    # The cursor moves while the user answers; it must not restart the delay.
    text = '\n'.join(tail).strip('\n')
    fingerprint = hashlib.sha256((reason + re.sub(r'[>❯›]|\s+', '', text)).encode()).hexdigest()
    seen = _attention_seen.get(key)
    if not seen or seen[0] != fingerprint:
        _attention_seen[key] = (fingerprint, now)
        return None
    if now - seen[1] < ATTENTION_DELAY:
        return None
    found = {'id': fingerprint, 'text': text, 'reason': reason}
    if reason == 'menu':
        found.update(menu_question(tail) or {})
    return found


def transcript_busy(path, window=262144):
    """True when the last lifecycle event in a Codex rollout file is a started turn."""
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - window))
            tail = stream.read().decode(errors='ignore')
    except (OSError, TypeError):
        return False
    last = None
    for line in tail.splitlines():
        for kind in ('task_started', 'task_complete', 'turn_aborted'):
            if '"type":"' + kind + '"' in line.replace(' ', ''):
                last = kind
    return last == 'task_started'


def reflow_prompt(text):
    """Antigravity breaks its permission request at the pane width; join each paragraph back into one line and drop the
    rule lines and the lone 'Command' heading."""
    paragraphs, current = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or RULE_LINE.match(stripped):
            if current:
                paragraphs.append(' '.join(current))
                current = []
        elif stripped != 'Command':
            current.append(stripped)
    if current:
        paragraphs.append(' '.join(current))
    return '\n'.join(paragraphs)


def live_status(entry, run=subprocess.run):
    if not entry.get('owner_pid') or not entry.get('writable'):
        return {'state': 'Unavailable', 'approval': None}
    screen = capture(entry, run)
    if entry['agent'] == 'antigravity':
        capture_notices(entry, screen)
    else:
        capture_quota_notices(entry, screen)
    pending = approval(screen)
    if pending:
        if entry['agent'] == 'antigravity':
            pending['text'] = reflow_prompt(pending['text'])
        return {'state': 'Approval', 'approval': {k: v for k, v in pending.items() if not k.startswith('_')}}
    working = bool(re.search(r'Working \(|esc to interrupt|(?m:^\s*[✻✶✽✢·*●]\s+[A-Z][a-z]+(?:ing|ed)?…)', screen, re.I))
    if entry['agent'] == 'codex':
        working = working or transcript_busy(entry.get('transcript'))
    if entry['agent'] == 'claude':
        try:
            working = working or json.loads(Path(entry['registry']).read_text()).get('status') == 'busy'
        except (OSError, ValueError):
            pass
    elif entry['agent'] == 'antigravity':
        try:
            metrics = json.loads((AGY_CACHE / (entry['thread'] + '.json')).read_text())
            working = working or metrics.get('agent_state') in ('thinking', 'working', 'tool_use', 'running')
            if metrics.get('tool_confirmation_pending'):
                return {'state': 'Approval', 'approval': {'id': None, 'text': screen.strip(), 'actionable': False}}
        except (OSError, ValueError):
            pass
    needed = attention(entry, screen, working)
    if needed:
        return {'state': 'Input needed', 'approval': None, 'attention': needed}
    status = {'state': 'Working' if working else 'Idle', 'approval': None}
    if entry['agent'] == 'claude' and not working and (reply := suggestion(entry, run)):
        status['suggestion'] = reply
    return status


def verify(entry, run=subprocess.run):
    pid = entry['owner_pid']
    process = Path('/proc') / str(pid)
    valid_comm = {'claude': ('claude',), 'codex': ('codex',), 'antigravity': ('agy', 'exe')}[entry['agent']]
    process_comm = process.joinpath('comm').read_text().strip()
    if process_start(pid) != entry['process_start'] or process_comm not in valid_comm:
        raise RuntimeError('The agent restarted. Reload its tab before sending.')
    result = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                  '#{pane_pid}\t#{pane_current_command}\t#{pane_in_mode}'],
                 check=True, capture_output=True, text=True, timeout=5)
    root, command, mode = result.stdout.strip().split('\t')
    if mode != '0' or command not in {'claude': ('claude',), 'codex': ('node', 'codex'), 'antigravity': ('agy', 'exe')}[entry['agent']]:
        raise RuntimeError('Return to the agent prompt in Termius before sending.')
    ancestor = pid
    while ancestor != int(root) and ancestor > 1:
        ancestor = int((Path('/proc') / str(ancestor) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
    if ancestor != int(root):
        raise RuntimeError('The agent no longer belongs to this tmux pane.')
    if entry['agent'] == 'claude':
        if not entry.get('pending_conversation'):
            registry = json.loads(Path(entry['registry']).read_text())
            if registry.get('sessionId') != entry['thread'] or registry.get('pid') != pid:
                raise RuntimeError('The Claude conversation changed. Reload its tab.')
    elif entry['agent'] == 'antigravity':
        if not entry.get('pending_conversation'):
            lock = AGY_ROOT / 'presence' / (entry['thread'] + '.lock')
            if lock.exists():
                if not any(p.resolve() == lock for p in process.joinpath('fd').iterdir()):
                    raise RuntimeError('The Antigravity conversation changed. Reload its tab.')
            else:
                cache_file = AGY_CACHE / (entry['thread'] + '.json')
                if cache_file.is_file():
                    try:
                        cached = json.loads(cache_file.read_text())
                        if cached.get('conversation_id') != entry['thread']:
                            raise RuntimeError('The Antigravity conversation changed. Reload its tab.')
                    except (OSError, ValueError):
                        pass
    elif entry['agent'] == 'codex':
        if not entry.get('pending_conversation'):  # a new session has no conversation until its first message
            from agents import codex_database, codex_thread
            db = codex_database()
            try:
                row = codex_thread(db, pid, entry['process_start'], entry.get('thread_cwd') or entry['cwd'])
            finally:
                db.close()
            if not row or row[0] != entry['thread']:
                raise RuntimeError('The Codex conversation changed. Reload its tab.')


class DraftError(RuntimeError):
    """The agent input box holds text, so sending would mix it with the new message."""


def prompt_draft(entry, run=subprocess.run):
    screen = capture(entry, run)
    if approval(screen):
        raise RuntimeError('Respond to the pending approval before sending a message.')
    marker = {'claude': '❯', 'codex': '›', 'antigravity': '>'}[entry['agent']]
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.startswith(marker)]
    if not prompts:
        raise RuntimeError('Cannot identify the agent input box. Check Termius before sending.')
    start = prompts[-1]
    draft = [lines[start][1:].strip()]
    for line in lines[start + 1:]:
        if line.startswith('─') or (entry['agent'] == 'codex' and not line.strip()):
            break
        draft.append(line.strip())
    if entry['agent'] == 'codex' and draft == ['Ask Codex to do anything']:
        draft = []
    if entry['agent'] == 'claude' and draft == ['Press up to edit queued messages']:
        draft = []
    # Claude renders placeholders and prompt suggestions ("Try ...", "push it") dimmed; typed text is not dim.
    if any(draft) and entry['agent'] == 'claude':
        if dim_prompt_text(capture(entry, run, styled=True)) == draft[0] and len(draft) == 1:
            draft = []
    if len(draft) == 1 and entry['agent'] == 'antigravity' and AGY_MODE_HINT.search(draft[0]):
        # Antigravity fills the empty box with a mode hint ("Auto mode: ... (shift+tab to cycle)") in its hint colour,
        # and the mode changes it; typed text is never drawn in that colour.
        if hint_prompt_text(capture(entry, run, styled=True), '>') == draft[0]:
            draft = []
    return draft


ANSI_SGR = re.compile(r'\x1b\[[0-9;]*m')
AGY_MODE_HINT = re.compile(r'\bmode\b.*(?:\(shift\+tab to cycle\)|\.\.\.|…)\s*$', re.I)  # a narrow pane cuts the hint short


def hint_prompt_text(styled, marker):
    """Text of the last input line when all of it is in Antigravity's hint colour (256-colour 109), else ''."""
    candidates = [line for line in styled.splitlines() if ANSI_SGR.sub('', line).startswith(marker)]
    if not candidates:
        return ''
    line = candidates[-1]
    hint = False
    start = line.index(marker) + 1
    for part in re.split(r'(\x1b\[[0-9;]*m)', line[start:]):
        if ANSI_SGR.fullmatch(part):
            codes = [int(code or 0) for code in part[2:-1].split(';') if code.isdigit() or code == '']
            if codes[:3] == [38, 5, 109]:
                hint = True
            elif 0 in codes or 39 in codes or codes[:2] == [38, 5] or codes[:1] == [38]:
                hint = False
        elif part.strip() and not hint:
            return ''
    return ANSI_SGR.sub('', line)[1:].strip()


def dim_prompt_text(styled):
    """Text of Claude's input line when every visible character is dimmed (a placeholder or suggested reply)."""
    candidates = [line for line in styled.splitlines() if ANSI_SGR.sub('', line).startswith('❯')]
    if not candidates:
        return ''
    line = candidates[-1]
    text = ANSI_SGR.sub('', line)[1:].strip()
    dim = False
    for part in re.split(r'(\x1b\[[0-9;]*m)', line[line.index('❯') + 1:]):
        if ANSI_SGR.fullmatch(part):
            for code in [int(code or 0) for code in part[2:-1].split(';')]:
                if code in (0, 22):
                    dim = False
                elif code == 2:
                    dim = True
        elif part.strip() and not dim:
            return ''
    return text


def suggestion(entry, run=subprocess.run):
    """A reply Claude has prepared for the user, shown dimmed in the input box."""
    text = dim_prompt_text(capture(entry, run, styled=True))
    return '' if text == 'Press up to edit queued messages' or text.startswith('Try ') else text


AGY_PANEL = re.compile(r'esc Close|↑/↓ Scroll|\(\d+–\d+ of \d+ lines\)|general\s+commands\s+shortcuts', re.I)


def close_agy_panel(entry, run=subprocess.run):
    """Antigravity's /usage, /help ... open full-screen panels that swallow typing: close one (Esc) before sending anything."""
    if entry.get('agent') != 'antigravity':
        return
    for _ in range(3):
        if not AGY_PANEL.search('\n'.join(capture(entry, run).splitlines()[-40:])):
            return
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Escape'], check=True, capture_output=True, timeout=5)
        time.sleep(.5)
    if AGY_PANEL.search('\n'.join(capture(entry, run).splitlines()[-40:])):
        raise RuntimeError('An Antigravity panel is open. Close it in Termius, then send again.')


def empty_prompt(entry, run=subprocess.run):
    close_agy_panel(entry, run)
    if any(prompt_draft(entry, run)):
        raise DraftError('There is an unsent draft in the agent input. Clear it, then send again.')


def type_text(entry, text, enter=False, run=subprocess.run):
    if not isinstance(text, str) or not 0 < len(text) <= 500 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('Enter up to 500 characters of text.')
    verify(entry, run)
    run([paths.TMUX, 'send-keys', '-t', entry['id'], '-l', text], check=True, capture_output=True, timeout=5)
    if enter:
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)


def clear_draft(entry, run=subprocess.run):
    verify(entry, run)
    if not any(prompt_draft(entry, run)):
        return
    keys = lambda *k: run([paths.TMUX, 'send-keys', '-t', entry['id'], *k], check=True, capture_output=True, timeout=5)
    keys('C-e')
    # Ctrl+U clears to the start of the line; Backspace joins the previous line of a multi-line draft.
    for _ in range(40):
        keys('C-u')
        time.sleep(.05)
        if not any(prompt_draft(entry, run)):
            return
        keys('BSpace')
        if not any(prompt_draft(entry, run)):
            return
    raise DraftError('Could not clear the draft. Clear it in Termius.')


def confirm_delivery(entry, sent, run):
    """Antigravity sometimes drops a message right after it starts or refreshes, with an empty box and no error. Its screen
    then never shows the text, so a message missing from the screen after a moment is reported instead of silently lost."""
    if entry.get('agent') != 'antigravity' or sent.startswith('/') or len(sent) < 3:
        return
    for _ in range(8):
        time.sleep(.5)
        screen = capture_screen(entry, 200, run)
        # Taken if the text is on screen (the pane can cut or rewrap it, so only its start is compared), or if the agent is
        # visibly busy: a message sent while it works may be queued out of sight.
        if sent[:15] in ''.join(screen.split()) or re.search(r'esc to cancel|Loading\.\.\.|Working', '\n'.join(screen.splitlines()[-8:])):
            return
    raise RuntimeError('Antigravity did not take the message. Send it again.')


def submit(entry, message, run=subprocess.run):
    if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in message):
        raise RuntimeError('The message contains terminal control characters.')
    verify(entry, run)
    empty_prompt(entry, run)
    buffer = 'tmux-mobile-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=message.encode(), check=True, capture_output=True, timeout=5)
        verify(entry, run)
        empty_prompt(entry, run)
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        verify(entry, run)
        if approval(capture(entry, run)):
            raise RuntimeError('An approval appeared during delivery. Check Termius before retrying.')
        # The agent can swallow an Enter that arrives right behind a bracketed paste, leaving the
        # message sitting in the input box; confirm the box emptied and press Enter again if not.
        sent = ''.join(message.split())
        for _ in range(4):
            time.sleep(.2)
            run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
            time.sleep(.3)
            try:
                draft = prompt_draft(entry, run)
            except RuntimeError as exc:
                if 'approval' in str(exc):
                    raise
                return  # no input box on screen any more: a menu or picker took the Enter
            if not any(draft):
                confirm_delivery(entry, sent, run)
                return
            # Agents mark the highlighted row of a picker (/model ...) like the input line. If the box no longer holds the
            # text we sent, a menu opened: pressing Enter again would choose an item for the user.
            if '\n' not in message.strip() and not ''.join(' '.join(draft).split()).startswith(sent[:30]):
                return
        raise DraftError('The message reached the agent input but was not submitted. Clear it, then send again.')
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)


def respond_approval(entry, identifier, action=None, run=subprocess.run, choice=None):
    if choice is None and action not in ('approve', 'reject'):
        raise ValueError('Invalid approval action.')
    verify(entry, run)
    pending = approval(capture(entry, run))
    if not pending or pending['id'] != identifier or not pending['actionable']:
        raise RuntimeError('The approval changed. Reload and review it again.')
    if choice is not None:
        if type(choice) is not int or choice not in pending['_numbers']:
            raise ValueError('Choose one of the displayed approval options.')
        target = pending['_numbers'].index(choice)
    else:
        target = pending['_once'] if action == 'approve' else pending['_reject']
    if target is None:
        raise RuntimeError('That approval choice is unavailable. Review it again.')
    delta = target - pending['_selected']
    expected = pending['_selected']
    for _ in range(abs(delta)):
        verify(entry, run)
        current = approval(capture(entry, run))
        if not current or current['id'] != identifier:
            raise RuntimeError('The approval changed. Review it again.')
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Down' if delta > 0 else 'Up'], check=True, capture_output=True, timeout=5)
        expected += 1 if delta > 0 else -1
        deadline = time.monotonic() + .5
        while True:
            current = approval(capture(entry, run))
            if not current or current['id'] != identifier:
                raise RuntimeError('The approval changed. Review it again.')
            if current['_selected'] == expected:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not confirm the approval selection. Check Termius.')
            time.sleep(.03)
    verify(entry, run)
    current = approval(capture(entry, run))
    if not current or current['id'] != identifier or current['_selected'] != target:
        raise RuntimeError('Could not confirm the selected approval choice. Review it in Termius.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)


def shell_prompt(entry, run=subprocess.run):
    if entry.get('agent') != 'shell':
        raise RuntimeError('An agent is already running in this pane.')
    if process_start(entry['pane_pid']) != entry['pane_start']:
        raise RuntimeError('This tmux pane changed. Reload its tab.')
    process = Path('/proc') / str(entry['pane_pid'])
    fields = process.joinpath('stat').read_text().rsplit(')', 1)[1].split()
    if process.joinpath('comm').read_text().strip() not in ('bash', 'zsh', 'fish') or fields[2] != fields[5]:
        raise RuntimeError('Wait for the foreground program to finish before starting an agent.')
    state = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                 '#{pane_pid}\t#{pane_current_command}\t#{pane_in_mode}\t#{cursor_y}'],
                check=True, capture_output=True, text=True, timeout=5).stdout.strip().split('\t')
    if len(state) != 4 or state[0] != str(entry['pane_pid']) or state[1] not in ('bash', 'zsh', 'fish') or state[2] != '0':
        raise RuntimeError('This pane is not at an idle shell prompt.')
    lines = capture(entry, run).splitlines()
    row = int(state[3])
    if row >= len(lines):
        raise RuntimeError('Cannot identify the shell prompt. Check the terminal.')
    return lines[row].rstrip()


def start_agent(entry, agent, run=subprocess.run):
    command = LAUNCHERS.get(agent)
    if not command:
        raise ValueError('Choose a supported agent.')
    prompt = shell_prompt(entry, run)
    if not re.search(r'[$#>%❯➜]$', prompt):
        raise RuntimeError('There is terminal input or an unrecognized shell prompt. Clear it before starting an agent.')
    buffer = 'tmux-mobile-launch-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=command.encode(), check=True, capture_output=True, timeout=5)
        if shell_prompt(entry, run) != prompt:
            raise RuntimeError('The shell input changed. Try again after clearing its draft.')
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        deadline = time.monotonic() + 2
        while True:
            current = shell_prompt(entry, run)
            tail = current[len(prompt):].strip() if current.startswith(prompt) else current.strip()
            if current == prompt + ' ' + command or current == prompt + command or tail == command or current.rstrip().endswith(command):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not confirm the launch command. Check the terminal before retrying.')
            time.sleep(.05)
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)


def shell_snapshot(entry, run=subprocess.run):
    text = run([paths.TMUX, 'capture-pane', '-p', '-S', '-1000', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout.rstrip()
    return {'messages': [{'id': 'terminal', 'role': 'assistant', 'kind': 'terminal', 'text': text}],
            'thread': None, 'agent': 'shell', 'writable': True, 'session': entry['id'], 'usage': {}, 'busy': False}


def shell_submit(entry, message, run=subprocess.run):
    if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in message):
        raise RuntimeError('The command contains terminal control characters.')
    prompt = shell_prompt(entry, run)
    if not re.search(r'[$#>%❯➜]$', prompt):
        raise RuntimeError('Clear the existing terminal draft before sending a command.')
    buffer = 'tmux-mobile-shell-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=message.encode(), check=True, capture_output=True, timeout=5)
        if shell_prompt(entry, run) != prompt:
            raise RuntimeError('The shell input changed. Try again after clearing its draft.')
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        time.sleep(.1)
        shell_prompt(entry, run)  # Recheck the idle foreground shell before Enter.
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)

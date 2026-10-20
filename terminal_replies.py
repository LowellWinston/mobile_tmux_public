"""Codex answers its slash commands (/cd, /pwd, /status ...) on the terminal only; nothing reaches its history file.
After such a command is sent, read what the terminal added and keep it as a chat notice."""
import difflib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
import time
import uuid
import paths

CACHE = paths.CACHE / 'replies'
LOCK = threading.Lock()
KEEP = 50
MAX_TEXT = 2000
BOX = re.compile(r'^[\s─━│┃┌┐└┘├┤┬┴┼╭╮╰╯═║\-_|+]*$')


def key(entry):
    return '%s-%s' % (entry.get('owner_pid'), entry.get('process_start'))


def is_noise(line):
    text = line.strip()
    return (not text or BOX.match(text) is not None or text.startswith('›')
            or ('Context' in text and ('% left' in text or '% used' in text)) or text.startswith('Tip:') or FOOTER.match(text) is not None or re.match(r'.*·\s*Context\b', text) is not None)


PICKER = re.compile(r'enter (?:to )?(?:select|confirm|continue)|esc (?:back|to cancel|to go back)|↑/?↓|to navigate|press enter', re.I)
FOOTER = re.compile(r'^\s*\S+\s+(?:minimal|low|medium|high|xhigh|extra high)\s+·\s+Context', re.I)
ITEM = re.compile(r'^\s*(?:[-*•◦]|\d+[.)])\s')


def unwrap(lines):
    """Join the lines a narrow terminal broke in the middle of a sentence; bullets, numbered items and blank lines stay apart."""
    result = []
    for line in lines:
        text = line.strip()
        if not text:
            result.append('')
        elif result and result[-1] and not ITEM.match(line):
            result[-1] += ('' if text.startswith('/') and '://' in result[-1] else ' ') + text
        else:
            result.append(text)
    return '\n'.join(result).strip()


def added_lines(before, after):
    """Lines the terminal gained between two screen captures, minus the prompt box and footer."""
    old, new = [line.rstrip() for line in before.splitlines()], [line.rstrip() for line in after.splitlines()]
    result = []
    for tag, _, _, start, end in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ('insert', 'replace'):
            result += [line for line in new[start:end] if not is_noise(line)]
    return [re.sub(r'^\s*[•◦]\s?', '', line).rstrip() for line in result]


LOGO = re.compile(r'[▀▄█▌▐░▒▓]+')
CLAUDE_LOGO = re.compile(r'[▀▄█▌▐▛▜▝▘▙▟░▒▓]+')


def banner_text(agent, screen):
    """What an agent prints when it starts (version, folder, model, tips, notices), as plain text; '' when none is on screen."""
    lines = screen.splitlines()
    if agent == 'codex':
        begin = next((i for i, line in enumerate(lines) if 'OpenAI Codex' in line), None)
        if begin is None:
            return ''
        block = []
        for line in lines[begin:]:
            if line.strip().startswith('›') or ('Context' in line and '·' in line):
                break
            block.append(line.strip().lstrip('>_').strip() if not block else line.strip())
    elif agent == 'antigravity':
        begin = next((i for i, line in enumerate(lines) if 'Antigravity CLI' in line), None)
        if begin is None:
            return ''
        block, previous_boxed = [], False
        for line in lines[begin:]:
            if re.match(r'^\s*─{3,}', line):
                break
            boxed = line.strip().startswith('┃')
            text = re.sub(r'^\s*┃\s?', '', LOGO.sub(' ', line)).strip()
            if boxed and block and block[-1] and not previous_boxed:
                block.append('')
            previous_boxed = boxed
            if text and '@' not in text and text != 'esc to dismiss':
                block.append(text)
    elif agent == 'claude':
        begin = next((i for i, line in enumerate(lines) if re.search(r'Claude Code v\d', line)), None)
        if begin is None:
            return ''
        block = []
        for line in lines[begin:begin + 12]:
            if re.match(r'^\s*(?:─{3,}|[❯>!]|⏺|●)', line):
                break
            block.append(re.sub(r'^\s*▎\s?', '', CLAUDE_LOGO.sub(' ', line)).strip())
    else:
        return ''
    paragraphs, current = [], []
    for line in block + ['']:
        if line:
            current.append(line)
        elif current:
            wrapped = current[0].startswith('Tip') or len(current) > 3  # narrow panes wrap prose; short blocks are header lines
            paragraphs.append(''.join((' ' if index and not part.startswith('/') else '') + part for index, part in enumerate(current))
                              if wrapped else '\n'.join(current))
            current = []
    return '\n'.join(paragraphs)


def started_iso(entry):
    from agents import started_ms
    try:
        return datetime.fromtimestamp(started_ms(entry['process_start']) / 1000, timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def capture_banner(entry, read):
    """Keep the start-up banner as the first chat notice of this agent process, once; it is on the terminal only."""
    if entry.get('agent') not in ('codex', 'antigravity', 'claude') or not entry.get('process_start') or any(n['id'] == 'terminal:banner' for n in saved(entry)):
        return
    try:
        text = banner_text(entry['agent'], read(entry))
    except Exception:
        return
    if text:
        notice = {'id': 'terminal:banner', 'role': 'assistant', 'kind': 'banner', 'text': text[:MAX_TEXT], 'time': started_iso(entry)}
        with LOCK:
            notices = [n for n in saved(entry) if n['id'] != 'terminal:banner'] + [notice]
            CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = CACHE / (key(entry) + '.json')
            temporary = CACHE / (key(entry) + '.' + str(os.getpid()) + '.tmp')
            temporary.write_text(json.dumps(notices[-KEEP:]))
            temporary.chmod(0o600)
            temporary.replace(target)


def saved(entry):
    try:
        value = json.loads((CACHE / (key(entry) + '.json')).read_text())
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def add(entry, text):
    notice = {'id': 'terminal:' + uuid.uuid4().hex[:12], 'role': 'assistant', 'kind': 'notice', 'text': text[:MAX_TEXT],
              'time': datetime.now(timezone.utc).isoformat()}
    with LOCK:
        notices = (saved(entry) + [notice])[-KEEP:]
        CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = CACHE / (key(entry) + '.json')
        temporary = CACHE / (key(entry) + '.' + str(os.getpid()) + '.tmp')
        temporary.write_text(json.dumps(notices))
        temporary.chmod(0o600)
        temporary.replace(target)
    return notice


def record(entry, before, read, sleep=time.sleep, polls=8):
    """Wait for the terminal to settle after a slash command, then keep whatever it printed."""
    previous, after = None, before
    for poll in range(polls):
        sleep(0.4)
        try:
            after = read(entry)
        except Exception:
            return None
        if after == previous and poll >= 1:
            break
        previous = after
    if PICKER.search(after):  # a menu waiting for a choice is not an answer; it is gone once chosen
        return None
    lines = added_lines(before, after)
    text = unwrap(lines)
    return add(entry, text) if text else None


def merge(entry, messages):
    """Put saved terminal replies among the chat messages by the time they were captured."""
    notices = saved(entry)
    if not notices:
        return messages

    def stamp(value):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (AttributeError, TypeError, ValueError):
            return 0
    result = list(messages)
    for notice in notices:
        if notice['id'] == 'terminal:banner':
            notice = dict(notice, kind='banner')  # saved before banners had their own, quieter style
        later = next((index for index, message in enumerate(result) if stamp(message.get('time')) > stamp(notice['time'])), len(result))
        result.insert(later, notice)
    return result

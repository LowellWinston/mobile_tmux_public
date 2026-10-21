"""Preserve Claude/Codex quota errors that their chat transcripts may omit."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import paths
from activity import insert_by_time

CACHE = paths.CACHE / 'notices'
LOCK = threading.Lock()
LIMIT_TEXT = re.compile(
    r"(?:you(?:'ve| have) (?:hit|reached) (?:your )?(?:(?:session|weekly|usage|rate|five.hour|5.hour|seven.day|7.day) )*limit"
    r"|you(?:'re| are) out of (?:extra )?usage|(?:usage|rate|weekly|session) limit (?:reached|exceeded)"
    r"|(?:exceeded|reached) (?:your )?(?:current )?quota|quota (?:exceeded|reached)|too many requests)", re.I)
LIMIT_CODE = re.compile(r'usage.?limit|rate.?limit|quota.?exceeded|insufficient.?quota', re.I)


def clean(text):
    return re.sub(r'^\s*(?:[⎿⚠■!]+\s*)?(?:(?:API )?Error:\s*)?', '', text).strip()


def is_quota_text(text):
    return bool(isinstance(text, str) and LIMIT_TEXT.search(text.replace('’', "'")))


def identity(text):
    normalized = ' '.join(clean(text).replace('’', "'").lower().split())
    limit = LIMIT_TEXT.search(normalized)
    reset = re.search(r'(?:resets?\b|try again\b).*$', normalized)
    if limit:
        normalized = limit[0] + (' ' + reset[0] if reset else '')
    return hashlib.sha256(normalized.encode()).hexdigest()


def error_message(payload):
    text = payload.get('message') or payload.get('text')
    code = json.dumps([payload.get('error'), payload.get('code'), payload.get('codex_error_info')])
    if is_quota_text(text) or LIMIT_CODE.search(code):
        return clean(text) if isinstance(text, str) and text.strip() else 'Usage limit reached.'
    return None


def saved(entry):
    try:
        value = json.loads((CACHE / entry['agent'] / (entry['thread'] + '.json')).read_text())
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def capture(entry, screen):
    if entry.get('agent') not in ('claude', 'codex') or not entry.get('thread'):
        return
    found = []
    lines = screen.splitlines()
    for index, line in enumerate(lines):
        text = clean(line)
        # Prompt text and command/tool examples must not become quota warnings.
        if line.lstrip().startswith(('❯', '›', '>', '$', '#', '●', '○')):
            continue
        if not re.match(r"^(?:you(?:'ve|'re| have| are)|usage limit|rate limit|weekly limit|session limit|quota|too many requests)", text.replace('’', "'"), re.I):
            continue
        parts = [text]
        for following in lines[index + 1:index + 10]:
            if not following.strip() or following.lstrip().startswith(('❯', '›', '>', '─', '●', '○', '✻', '✢', '■', '⎿')):
                break
            parts.append(following.strip())
        text = '\n'.join(parts)
        if not is_quota_text(' '.join(parts)):
            continue
        found.append({'id': 'quota:' + identity(text), 'role': 'assistant', 'kind': 'notice',
                      'text': text, 'time': datetime.now(timezone.utc).isoformat()})
    if not found:
        return
    with LOCK:
        notices = {notice['id']: notice for notice in saved(entry)}
        changed = False
        for notice in found:
            if notice['id'] not in notices:
                notices[notice['id']] = notice
                changed = True
        if changed:
            folder = CACHE / entry['agent']
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = folder / (entry['thread'] + '.json')
            temporary = folder / (entry['thread'] + '.' + str(os.getpid()) + '.tmp')
            temporary.write_text(json.dumps(list(notices.values())))
            temporary.chmod(0o600)
            temporary.replace(target)


def merge(entry, messages):
    """Prefer the transcript's warning when the same text was seen in tmux."""
    seen = {identity(message['text']) for message in messages if message.get('kind') == 'notice'}
    fresh = []
    for notice in saved(entry):
        if identity(notice['text']) in seen:
            continue
        fresh.append(notice)  # captured time orders terminal-only notices among persisted chat messages
        seen.add(identity(notice['text']))
    return insert_by_time(messages, fresh)

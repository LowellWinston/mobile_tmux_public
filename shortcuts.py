"""Typed shortcuts for the chat box ("cnp" -> "commit and push"), kept on the server so every device shares them."""
import json
import os
import re
from pathlib import Path
import paths

FILE = paths.CACHE / 'shortcuts.json'
NAME = re.compile(r'^[A-Za-z0-9_.-]{1,24}$')
MAX_SHORTCUTS = 50
MAX_TEXT = 4000


def load():
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get('name'), str) and isinstance(item.get('text'), str)] if isinstance(data, list) else []


def clean(items):
    """Validate a submitted list and return it in storage form; raises ValueError with a message the page can show."""
    if not isinstance(items, list):
        raise ValueError('Invalid shortcuts.')
    if len(items) > MAX_SHORTCUTS:
        raise ValueError('At most %d shortcuts.' % MAX_SHORTCUTS)
    result, seen = [], set()
    for item in items:
        name = item.get('name', '').strip() if isinstance(item, dict) and isinstance(item.get('name'), str) else ''
        text = item.get('text', '').strip() if isinstance(item, dict) and isinstance(item.get('text'), str) else ''
        if not NAME.match(name):
            raise ValueError('A shortcut name is 1-24 letters, numbers, dots, dashes or underscores, with no spaces.')
        if not text or len(text) > MAX_TEXT:
            raise ValueError('The text for "%s" must be 1-%d characters.' % (name, MAX_TEXT))
        if name.lower() in seen:
            raise ValueError('Two shortcuts are named "%s".' % name)
        seen.add(name.lower())
        result.append({'name': name, 'text': text})
    return result


def save(items):
    items = clean(items)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(items, indent=1))
    os.replace(temporary, FILE)
    return items

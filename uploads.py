"""Files and screenshots sent from the phone.

A file is saved under the cache folder and the page puts its path into the message, so every agent sees an ordinary
path it can read. All files share one folder so an agent's "allow reading from this folder" prompt, answered once,
covers later attachments too. Nothing here is ever served back, and old files are deleted automatically."""
import re
import secrets
import time
from pathlib import Path
import paths

LIMIT = 20 * 1024 * 1024
KEEP_SECONDS = 7 * 24 * 3600
ROOT = paths.CACHE / 'uploads'
NAME_LENGTH = 80


def safe_name(name):
    """A plain file name: no folders, no odd characters, and the extension kept (agents recognise images by it)."""
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(str(name or '').replace('\\', '/')).name).strip('._')
    stem, dot, extension = name.rpartition('.')
    if not dot:
        stem, extension = name, ''
    extension = extension[:10]
    stem = stem.strip('._')[:NAME_LENGTH - len(extension) - 1] or 'file'
    return stem + ('.' + extension if extension else '')


def prune(now=None, root=None):
    """Remove uploads older than a week."""
    root = root or ROOT
    cutoff = (now if now is not None else time.time()) - KEEP_SECONDS
    if not root.is_dir():
        return
    for file in root.iterdir():
        try:
            if file.is_file() and file.stat().st_mtime < cutoff:
                file.unlink()
        except OSError:
            continue


def save(name, data, root=None):
    """Store one upload and return its path. A random prefix keeps names from colliding."""
    if not data:
        raise ValueError('That file is empty.')
    if len(data) > LIMIT:
        raise ValueError('That file is too large (the limit is %d MB).' % (LIMIT // 1024 // 1024))
    root = root or ROOT
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prune(root=root)
    target = root / (secrets.token_hex(4) + '-' + safe_name(name))
    target.write_bytes(data)
    target.chmod(0o600)
    return target

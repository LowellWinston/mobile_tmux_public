"""Incremental reading of append-only JSONL history files.

A chat history only ever grows at the end, so each file is parsed once; later calls read just the bytes
appended since, instead of re-parsing hundreds of megabytes on every update.
"""
import json
import os
import threading

MAX_FILES = 12
# The chat view never asks for more than the latest 2,000 messages (see window_limit in server.py), so only a little more than
# that is kept per conversation; trimming happens in batches, and `dropped` keeps the true total.
KEEP_MESSAGES = 2500
TRIM_SLACK = 500
_states = {}
_locks = {}
_registry_lock = threading.Lock()


def _lock_for(path):
    with _registry_lock:
        return _locks.setdefault(path, threading.Lock())


def keep_tail(state):
    """Trim the oldest messages of a `{'messages': [...], 'dropped': n}` state once it is well over the cap."""
    if not isinstance(state, dict) or 'messages' not in state:
        return  # only message-list states are capped
    excess = len(state['messages']) - KEEP_MESSAGES
    if excess > TRIM_SLACK:
        del state['messages'][:excess]
        state['dropped'] += excess


def read(path, fresh, consume, trim=None):
    """Bring the cached state for `path` up to date and return it.

    `fresh()` builds an empty state; `consume(state, index, record)` folds one decoded record in, where `index`
    is the record's zero-based line number; `trim(state)` bounds its size after each read. The returned state is
    shared: callers must copy before changing it.
    """
    path = str(path)
    trim = keep_tail if trim is None else trim  # looked up now, so it can be replaced; pass False to disable
    try:
        info = os.stat(path)
    except (OSError, TypeError):
        return fresh()
    with _lock_for(path):
        cached = _states.get(path)
        if cached and (cached['inode'] != info.st_ino or info.st_size < cached['offset']):
            cached = None  # replaced or truncated: start over
        if cached is None:
            cached = {'inode': info.st_ino, 'offset': 0, 'index': 0, 'state': fresh()}
        if info.st_size > cached['offset']:
            with open(path, 'rb') as stream:
                stream.seek(cached['offset'])
                if cached.pop('unterminated', False) and stream.read(1) == b'\n':
                    cached['offset'] += 1  # the newline that finished a record already consumed
                stream.seek(cached['offset'])
                since_trim = 0
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    complete = line.endswith(b'\n')
                    try:
                        record = json.loads(line)
                    except ValueError:
                        if not complete:
                            break  # a record still being written is picked up on the next call
                        record = None
                    cached['offset'] += len(line)
                    index, cached['index'] = cached['index'], cached['index'] + 1
                    cached['unterminated'] = not complete
                    if record is not None:
                        consume(cached['state'], index, record)
                        since_trim += 1
                        if trim and since_trim >= 2000:  # keep the first load of a huge file bounded too
                            trim(cached['state'])
                            since_trim = 0
            if trim:
                trim(cached['state'])
        with _registry_lock:
            _states.pop(path, None)
            _states[path] = cached  # most recently used goes last
            while len(_states) > MAX_FILES:
                oldest = next(iter(_states))
                _states.pop(oldest)
        return cached['state']


def forget():
    with _registry_lock:
        _states.clear()

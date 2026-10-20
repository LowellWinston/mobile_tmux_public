"""Capture Claude's official status-line data for the mobile rings."""
import json
import os
from pathlib import Path
import re
import sys
import time


def capture(data):
    session = data.get('session_id', '')
    if not re.fullmatch(r'[a-zA-Z0-9-]+', session):
        return
    folder = Path(os.environ.get('MOBILE_TMUX_CACHE') or Path.home() / '.cache/mobile-tmux') / 'usage'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (session + '.json')
    temporary = folder / (session + '.' + str(os.getpid()) + '.tmp')
    payload = {key: data[key] for key in ('context_window', 'rate_limits', 'model') if key in data}
    payload['reported_at'] = time.time()
    temporary.write_text(json.dumps(payload))
    temporary.chmod(0o600)
    temporary.replace(target)


if __name__ == '__main__':
    try:
        capture(json.load(sys.stdin))
    except (OSError, ValueError, TypeError):
        pass

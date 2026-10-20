"""Where Mobile Tmux finds its own files and each agent's files. Every location can be overridden with an environment variable."""
import os
from pathlib import Path
import shutil

HOME = Path(os.environ.get('MOBILE_TMUX_HOME') or Path.home())
CACHE = Path(os.environ.get('MOBILE_TMUX_CACHE') or HOME / '.cache/mobile-tmux')
CONFIG = Path(os.environ.get('MOBILE_TMUX_CONFIG') or HOME / '.config/mobile-tmux')
TMUX = os.environ.get('MOBILE_TMUX_TMUX') or shutil.which('tmux') or '/usr/bin/tmux'

CLAUDE = HOME / '.claude'
CODEX = HOME / '.codex'
ANTIGRAVITY = HOME / '.gemini/antigravity-cli'


def private_file(name):
    """A file under the config folder (owner-only data such as the access token and push keys)."""
    return CONFIG / name

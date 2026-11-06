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
OPENCODE = Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local/share') / 'opencode'
OPENCODE_MODELS = Path(os.environ.get('XDG_CACHE_HOME') or HOME / '.cache') / 'opencode/models.json'
ANTIGRAVITY = HOME / '.gemini/antigravity-cli'


def private_file(name):
    """A file under the config folder (owner-only data such as the access token and push keys)."""
    return CONFIG / name

#!/usr/bin/env python3
"""Install Mobile Tmux for the current user: systemd user services, plus (optionally) the agents' status-line hooks.

    python3 deploy/install.py            # services only
    python3 deploy/install.py --hooks    # also add the usage-ring hooks to Claude Code and Antigravity (only where none is set)

Needs no root. Safe to run again.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
UNITS = HOME / '.config/systemd/user'
PYTHON = shutil.which('python3') or '/usr/bin/python3'
PORT = os.environ.get('MOBILE_TMUX_PORT', '7683')
TTYD_PORT = os.environ.get('MOBILE_TMUX_TTYD_PORT', '7682')

SERVER_UNIT = f"""[Unit]
Description=Mobile Tmux: chat view for tmux-hosted coding agents
After=network.target

[Service]
ExecStart={PYTHON} {ROOT / 'server.py'}
Environment=PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}
Environment=MOBILE_TMUX_PORT={PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""

TERMINAL_UNIT = """[Unit]
Description=Mobile Tmux: live terminal (ttyd), reached only through the Mobile Tmux server

[Service]
ExecStart={ttyd} -W -i 127.0.0.1 -p {port} -O -a -b /terminal/live -t titleFixed=Terminal -t fontSize=20 -t scrollback=10000 {python} {attach}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def systemctl(*arguments):
    subprocess.run(['systemctl', '--user', *arguments], check=True)


def add_hook(settings_file, key, command, extra=None):
    """Point the agent's status line at our recorder, unless the user already has a status line of their own."""
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(settings_file.read_text())
    except FileNotFoundError:
        data = {}
    except ValueError:
        return f'  {settings_file}: not valid JSON, left alone. Add the status line yourself (see the README).'
    if key in data:
        return f'  {settings_file}: already has a "{key}"; left alone. See the README to combine it with ours.'
    shutil.copy2(settings_file, settings_file.with_name(settings_file.name + '.before-mobile-tmux')) if settings_file.exists() else None
    data[key] = dict({'type': 'command', 'command': command}, **(extra or {}))
    settings_file.write_text(json.dumps(data, indent=2) + '\n')
    return f'  {settings_file}: usage hook added (backup: {settings_file.name}.before-mobile-tmux). Restart the agent to load it.'


def main():
    if shutil.which('tmux') is None:
        raise SystemExit('tmux is not installed.')
    UNITS.mkdir(parents=True, exist_ok=True)
    (UNITS / 'mobile-tmux.service').write_text(SERVER_UNIT)
    ttyd = shutil.which('ttyd')
    if ttyd:
        (UNITS / 'mobile-tmux-terminal.service').write_text(TERMINAL_UNIT.format(ttyd=ttyd, port=TTYD_PORT, python=PYTHON, attach=ROOT / 'attach.py'))
    systemctl('daemon-reload')
    systemctl('enable', '--now', 'mobile-tmux.service')
    systemctl('restart', 'mobile-tmux.service')
    if ttyd:
        systemctl('enable', '--now', 'mobile-tmux-terminal.service')
    sys.path.insert(0, str(ROOT))
    import auth
    token = auth.token()
    print(f'\nMobile Tmux is running on http://127.0.0.1:{PORT}/mobile/')
    print(f'Access token (also in {auth.TOKEN_FILE}):\n  {token}')
    if not ttyd:
        print('\nOptional: install ttyd and run this again to enable the full interactive terminal ("Interact").')
    if '--hooks' in sys.argv:
        print('\nUsage-ring hooks:')
        if (HOME / '.claude').is_dir():
            print(add_hook(HOME / '.claude/settings.json', 'statusLine', f'{PYTHON} {ROOT / "claude_usage.py"}'))
        if (HOME / '.gemini/antigravity-cli').is_dir():
            print(add_hook(HOME / '.gemini/antigravity-cli/settings.json', 'statusLine', f'{PYTHON} {ROOT / "antigravity_status.py"}',
                           {'enabled': True, 'stack_with_default': True}))
    else:
        print('\nRun with --hooks to also install the usage-ring hooks for Claude Code and Antigravity.')
    print('\nTo keep it running after you log out:  loginctl enable-linger $USER')
    print('To reach it from your phone, see "Reaching it from your phone" in the README.')


if __name__ == '__main__':
    main()

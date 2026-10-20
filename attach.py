"""Attach ttyd clients to an existing tmux session ID, without arbitrary arguments."""
import os
import re
import subprocess
import sys
import paths


def target(arguments):
    if len(arguments) != 1 or not re.fullmatch(r'\$[0-9]+', arguments[0]):
        raise ValueError('Select a session from the terminal tabs.')
    return arguments[0]


if __name__ == '__main__':
    try:
        session = target(sys.argv[1:])
    except ValueError as error:
        raise SystemExit(str(error))
    result = subprocess.run([paths.TMUX, 'has-session', '-t', session])
    if result.returncode:
        raise SystemExit('That tmux session has ended. Refresh the session tabs.')
    os.execv(paths.TMUX, ['tmux', 'attach-session', '-t', session])

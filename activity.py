"""Helpers shared by the Claude, Codex and Antigravity parsers."""
import re

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def notice(identifier, time, text):
    return {'id': str(identifier), 'role': 'assistant', 'kind': 'notice', 'text': text, 'time': time}


def merge(messages):
    """Drop a notice that repeats the one right before it."""
    merged = []
    for message in messages:
        previous = merged[-1] if merged else None
        if message.get('kind') == 'notice' and previous and previous.get('kind') == 'notice' and previous['text'] == message['text']:
            continue
        merged.append(message)
    return merged


MAX_LINE = 160
MAX_ACTIONS = 200


def action(identifier, time, text):
    """One thing the agent did (ran a command, edited a file), as a single short line."""
    text = ' '.join(str(text).split())
    return {'id': str(identifier), 'role': 'assistant', 'kind': 'action',
            'text': text if len(text) <= MAX_LINE else text[:MAX_LINE - 1] + '…', 'time': time}


def group_actions(messages):
    """Fold each run of consecutive actions into one 'actions' message, so a long job is a single collapsed line in the chat."""
    grouped = []
    for message in messages:
        if message.get('kind') != 'action':
            grouped.append(message)
            continue
        previous = grouped[-1] if grouped else None
        if previous and previous.get('kind') == 'actions':
            if previous['count'] < MAX_ACTIONS:
                previous['text'] += '\n' + message['text']
            previous['count'] += 1
        else:
            grouped.append({'id': message['id'], 'role': 'assistant', 'kind': 'actions', 'text': message['text'],
                            'count': 1, 'time': message.get('time')})
    return grouped


def claude_tool_line(name, data):
    """A short description of one Claude tool call."""
    data = data if isinstance(data, dict) else {}
    first = lambda *keys: next((str(data[key]) for key in keys if data.get(key)), '')
    base = lambda path: path.rsplit('/', 1)[-1] if path else ''
    if name == 'Bash':
        command = first('command').split('\n')[0]
        return 'Ran ' + (first('description') + ': ' + command if first('description') else command)
    if name in ('Read', 'Write', 'Edit', 'NotebookEdit'):
        return {'Read': 'Read ', 'Write': 'Wrote ', 'Edit': 'Edited ', 'NotebookEdit': 'Edited '}[name] + base(first('file_path', 'notebook_path'))
    if name in ('Grep', 'Glob'):
        return 'Searched ' + first('pattern')
    if name in ('WebFetch', 'WebSearch'):
        return name + ' ' + first('url', 'query')
    if name in ('Agent', 'Task'):
        return 'Agent: ' + first('description', 'prompt')
    detail = first('description', 'command', 'path', 'query')
    return name + (' ' + detail if detail else '')

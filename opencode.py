"""OpenCode: conversation identity and chat history, read from its sqlite database."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from activity import action, claude_tool_line, group_actions, merge
import paths

DB = paths.OPENCODE / 'opencode.db'
SESSION = re.compile(r'^ses_[A-Za-z0-9]+$')


def connect():
    """Read-only; an empty stand-in when OpenCode has never run on this machine."""
    try:
        return sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True)
    except sqlite3.Error:
        connection = sqlite3.connect(':memory:')
        connection.executescript('CREATE TABLE session (id, directory, parent_id, time_created, time_updated, time_archived, model);'
                                 'CREATE TABLE message (id, session_id, time_created, data);'
                                 'CREATE TABLE part (id, message_id, session_id, time_created, data);')
        return connection


def session_for(connection, pid, since_ms, cwd):
    """The conversation this process runs: the one named by `-s <id>`, else the newest top-level one in its directory
    touched since it started. None until the first message creates one."""
    try:
        arguments = (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode('utf-8', 'ignore').split('\0')
    except OSError:
        arguments = []
    for flag, value in zip(arguments, arguments[1:]):
        if flag in ('-s', '--session') and SESSION.match(value):
            return value
    row = connection.execute(
        'SELECT id FROM session WHERE directory=? AND parent_id IS NULL AND time_archived IS NULL AND time_updated>=? '
        'ORDER BY time_updated DESC LIMIT 1', (cwd, since_ms - 60_000)).fetchone()
    return row[0] if row else None


# OpenCode tool name -> the Claude name activity.claude_tool_line describes (same one-line wording in every chat)
TOOLS = {'bash': 'Bash', 'read': 'Read', 'write': 'Write', 'edit': 'Edit', 'grep': 'Grep', 'glob': 'Glob',
         'webfetch': 'WebFetch', 'websearch': 'WebSearch', 'task': 'Task'}


def tool_line(part):
    arguments = dict((part.get('state') or {}).get('input') or {})
    arguments['file_path'] = arguments.get('filePath')
    return claude_tool_line(TOOLS.get(part.get('tool'), str(part.get('tool', 'tool'))), arguments)


def snapshot(entry, clean):
    connection = connect()
    try:
        rows = connection.execute(
            'SELECT m.id, m.time_created, m.data, p.id, p.data FROM message m LEFT JOIN part p ON p.message_id=m.id '
            'WHERE m.session_id=? ORDER BY m.time_created, m.id, p.time_created, p.id', (entry['thread'],)).fetchall()
        row = connection.execute('SELECT model, cost FROM session WHERE id=?', (entry['thread'],)).fetchone()
    finally:
        connection.close()
    messages, busy, tokens, model, limit = [], False, None, {}, None
    try:
        model = json.loads(row[0] or '{}') if row else {}
        limit = json.loads(paths.OPENCODE_MODELS.read_text())[model['providerID']]['models'][model['id']]['limit']['context']
    except (OSError, ValueError, KeyError, TypeError):
        pass  # the context size is unknown for a model OpenCode has not catalogued
    last = None
    for mid, created, data, pid, part in rows:
        info = json.loads(data)
        if mid != last:
            last = mid
            if info.get('role') == 'assistant':
                busy = not (info.get('time') or {}).get('completed')
                tokens = (info.get('tokens') or {}).get('total') or tokens
        if not part:
            continue
        part = json.loads(part)
        when = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
        if part.get('type') == 'text' and not part.get('synthetic') and clean(part.get('text', '')).strip():
            previous = messages[-1] if messages else None
            if previous and previous.get('mid') == mid and previous['role'] == info['role']:
                previous['text'] += '\n\n' + clean(part['text'])
            else:
                messages.append({'id': pid, 'mid': mid, 'role': info['role'], 'text': clean(part['text']), 'time': when})
        elif part.get('type') == 'tool':
            messages.append(action(pid, when, tool_line(part)))
    return {'messages': group_actions(merge(messages)), 'dropped': 0, 'busy': busy,
            'usage': {'context': {'tokens': tokens, 'limit': limit, 'percent': tokens * 100 / limit if tokens and limit else None},
                      'account': {}, 'five_hour': {}, 'weekly': {}, 'windows': [], 'reported_at': None,
                      'model': model.get('id', ''), 'cost': row[1] if row else None}}

"""Discover live tmux chats and use each agent's own transcript and message route."""
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from antigravity import live_entry as antigravity_entry, process_start
from quota_notices import error_message, merge as merge_quota_notices
import activity
import history
import paths

HOME = paths.HOME
METRICS = paths.CACHE / 'usage'


def records(path):
    if not Path(path).is_file():
        return  # A newly launched agent may not have written its first message yet.
    with Path(path).open() as stream:
        for index, line in enumerate(stream):
            try:
                yield index, json.loads(line)
            except json.JSONDecodeError:
                continue


def descendants(pid, processes):
    found = {pid}
    while True:
        children = {child for child, (parent, _) in processes.items() if parent in found}
        if children <= found:
            return found
        found |= children


UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


FOOTER_PATH = re.compile(r'·\s+(~(?:/[^\s·]*)?|/[^\s·]+)\s+·')


def footer_cwd(run, pane):
    """The working directory Codex shows in its footer line (it changes with /cd; the tmux pane's path does not)."""
    try:
        screen = run([paths.TMUX, 'capture-pane', '-p', '-t', pane, '-S', '-6'], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in reversed(screen.splitlines()):
        match = FOOTER_PATH.search(line)
        if match:
            path = match[1]
            return str(HOME) + path[1:] if path.startswith('~') else path
    return None


def started_ms(ticks):
    """Wall-clock start of a process, in milliseconds, from the clock ticks since boot that /proc reports."""
    boot = time.time() - float(Path('/proc/uptime').read_text().split()[0])
    return int((boot + int(ticks) / os.sysconf('SC_CLK_TCK')) * 1000)


def codex_thread(connection, pid, ticks, cwd):
    """The conversation this Codex process is running: the one named by `codex resume <id>`, else the newest one in
    its directory that was created or touched since the process started. None while it has not begun a conversation."""
    try:
        arguments = (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode('utf-8', 'ignore').split('\0')
    except OSError:
        arguments = []
    for index, argument in enumerate(arguments[:-1]):
        if argument == 'resume' and UUID.match(arguments[index + 1]):
            row = connection.execute('SELECT id,rollout_path FROM threads WHERE id=?', (arguments[index + 1],)).fetchone()
            if row:
                return row
    since = started_ms(ticks) - 60_000
    return connection.execute(
        "SELECT id,rollout_path FROM threads WHERE cwd=? AND archived=0 AND source IN ('cli','vscode') "
        "AND (created_at_ms>=? OR updated_at_ms>=?) ORDER BY updated_at_ms DESC LIMIT 1", (cwd, since, since)).fetchone()


def match_unresolved_codex(connection, entries):
    """A Codex pane whose folder cannot be read (a narrow pane cuts its footer off) is matched by time instead: it owns the
    conversations begun after it started and before the next Codex process did."""
    codex = sorted((e for e in entries if e['agent'] == 'codex'), key=lambda e: int(e.get('process_start') or 0))
    for index, entry in enumerate(codex):
        if not entry.get('pending_conversation'):
            continue
        since = started_ms(entry['process_start']) - 60_000
        later = codex[index + 1]['process_start'] if index + 1 < len(codex) else None
        until = started_ms(later) - 60_000 if later else None
        row = connection.execute(
            "SELECT id,rollout_path,cwd FROM threads WHERE archived=0 AND source IN ('cli','vscode') AND created_at_ms>=? "
            "AND (? IS NULL OR created_at_ms<?) ORDER BY updated_at_ms DESC LIMIT 1", (since, until, until)).fetchone()
        if row:
            entry.pop('pending_conversation', None)
            entry.update(thread=row[0], transcript=row[1], thread_cwd=row[2])


def codex_database():
    """Codex's thread index (newest state_N.sqlite), read-only; an empty stand-in when Codex has never run on this machine."""
    found = sorted((paths.CODEX).glob('state_*.sqlite'), key=lambda p: [int(n) for n in re.findall(r'\d+', p.name)])
    if found:
        try:
            return sqlite3.connect('file:' + str(found[-1]) + '?mode=ro', uri=True)
        except sqlite3.Error:
            pass
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INT, source TEXT, created_at_ms INT, updated_at_ms INT)')
    return connection


def discover(config, run):
    result = run([paths.TMUX, 'list-panes', '-a', '-F',
                  '#{pane_id}\t#{session_id}\t#{session_name}\t#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_current_path}'],
                 capture_output=True, text=True, timeout=5)
    if result.returncode:
        return ([{'id': 'saved', 'label': 'Last chat', 'agent': 'codex',
                  'thread': config['thread'], 'transcript': config['transcript'], 'writable': False}] if config.get('thread') else [])
    ps = run(['ps', '-eo', 'pid=,ppid=,comm='], capture_output=True, text=True, timeout=5)
    processes = {}
    for line in ps.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3:
            processes[int(fields[0])] = (int(fields[1]), fields[2])
    claude = []
    for path in (HOME / '.claude/sessions').glob('*.json'):
        try:
            registry = json.loads(path.read_text())
            if registry.get('kind') == 'interactive' and registry.get('pid') in processes:
                registry['registry'] = str(path)
                claude.append(registry)
        except (OSError, ValueError):
            continue
    connection = codex_database()
    entries = []
    try:
        for line in result.stdout.splitlines():
            pane, tmux_session, label, position, pid, cwd = line.split('\t', 5)
            tree = descendants(int(pid), processes)
            names = {processes[p][1] for p in tree if p in processes}
            entry = {'id': pane, 'tmux_session': tmux_session, 'label': label, 'position': position,
                     'agent': 'shell', 'writable': False,
                     'pane_pid': int(pid), 'pane_start': process_start(int(pid)), 'cwd': cwd}
            live = next((r for r in claude if r['pid'] in tree and r.get('tmux', '').endswith('.' + pane)), None)
            if live:
                project = re.sub(r'[^a-zA-Z0-9]', '-', live['cwd'])
                transcript = HOME / '.claude/projects' / project / (live['sessionId'] + '.jsonl')
                entry.update(agent='claude', thread=live['sessionId'], transcript=str(transcript),
                             registry=live['registry'], writable=True, owner_pid=live['pid'],
                             process_start=process_start(live['pid']),
                             busy=live.get('status') == 'busy')
            elif 'claude' in names:
                owner = next(p for p in tree if p in processes and processes[p][1] == 'claude')
                started = process_start(owner)
                pending = 'pending-' + str(owner) + '-' + started
                transcript = HOME / '.claude/projects' / 'pending' / (pending + '.jsonl')
                entry.update(agent='claude', thread=pending, transcript=str(transcript),
                             writable=True, owner_pid=owner, process_start=started,
                             pending_conversation=True)
            elif 'codex' in names:
                owner = next(p for p in tree if p in processes and processes[p][1] == 'codex')
                started = process_start(owner)
                row = codex_thread(connection, owner, started, cwd)
                thread_cwd = cwd
                if not row:  # after /cd, Codex records its conversation under the new folder, not the pane's
                    shown = footer_cwd(run, pane)
                    if shown and shown != cwd:
                        row, thread_cwd = codex_thread(connection, owner, started, shown), shown
                if row:
                    entry.update(agent='codex', thread=row[0], transcript=row[1], writable=True, cwd=cwd, thread_cwd=thread_cwd,
                                 owner_pid=owner, process_start=started)
                else:
                    # A brand-new Codex session has no conversation yet: show an empty chat, never another session's history.
                    pending = 'pending-' + str(owner) + '-' + started
                    entry.update(agent='codex', thread=pending, transcript=str(HOME / '.codex/sessions/pending' / (pending + '.jsonl')),
                                 writable=True, cwd=cwd, owner_pid=owner, process_start=started, pending_conversation=True)
            elif 'agy' in names or any(processes[p][1] in ('agy', 'exe') for p in tree if p in processes):
                entry['agent'] = 'antigravity'
                entry.update(antigravity_entry([p for p in tree if p in processes and processes[p][1] in ('agy', 'exe')], cwd=cwd))
            entries.append(entry)
        match_unresolved_codex(connection, entries)
    finally:
        connection.close()
    counts = {e['label']: sum(other['label'] == e['label'] for other in entries) for e in entries}
    for entry in entries:
        if counts[entry['label']] > 1:
            entry['label'] += ':' + entry['position']
    # Two Codex processes can never be one conversation: the older process keeps it, the newer one starts empty until it has its own.
    claimed = set()
    for entry in sorted((e for e in entries if e['agent'] == 'codex' and not e.get('pending_conversation')), key=lambda e: int(e.get('process_start') or 0)):
        if entry['thread'] in claimed:
            pending = 'pending-' + str(entry['owner_pid']) + '-' + str(entry['process_start'])
            entry.update(thread=pending, transcript=str(HOME / '.codex/sessions/pending' / (pending + '.jsonl')), pending_conversation=True)
        claimed.add(entry['thread'])
    return entries


def usage(context=None, rates=None, reported=None):
    context = context or {}
    rates = rates or []
    percent = context.get('percent')
    if percent is not None:
        context['percent'] = max(0, min(100, float(percent)))
    eligible = [r for r in rates if r.get('percent') is not None and r.get('active', True)
                and (not r.get('resets_at') or r['resets_at'] > time.time())]
    account = max(eligible, key=lambda r: r['percent']) if eligible else {}
    five_hour = next((r for r in eligible if r.get('period') == 'five_hour' or r.get('window_minutes') == 300 or r.get('label') in ('5h', 'five hour')), {})
    weekly = next((r for r in eligible if r.get('period') == 'weekly' or r.get('window_minutes') == 10080 or r.get('label') in ('168h', 'seven day', 'weekly')), {})
    return {'context': context, 'account': account, 'five_hour': five_hour, 'weekly': weekly,
            'windows': rates, 'reported_at': reported}


COMMAND = re.compile(r'<command-name>\s*(.*?)\s*</command-name>(?:.*?<command-args>(.*?)</command-args>)?', re.DOTALL)
COMMAND_OUTPUT = re.compile(r'<local-command-stdout>(.*?)</local-command-stdout>', re.DOTALL)


def claude_consume(messages, index, record, clean):
    """Fold one transcript record into the list of chat messages."""
    if record.get('isSidechain') or record.get('isMeta'):
        return
    if record.get('type') == 'attachment':
        # A message sent while Claude is busy is delivered as an attachment, not as a user record.
        attachment = record.get('attachment') or {}
        prompt = attachment.get('prompt') if attachment.get('type') == 'queued_command' else None
        if isinstance(prompt, str) and clean(prompt).strip():
            messages.append({'id': str(index), 'role': 'user', 'text': clean(prompt), 'time': record.get('timestamp'),
                             'delivery': 'queued'})
        return
    message = record.get('message', {})
    role = message.get('role')
    # Debug records such as api-request-blob also carry a user/assistant message; they are copies, not turns.
    if record.get('type') not in ('user', 'assistant') or role not in ('user', 'assistant'):
        if record.get('type') == 'system' and record.get('subtype') in ('error', 'api_error'):
            text = error_message(record)
            if text:
                messages.append({'id': str(index), 'role': 'assistant', 'kind': 'notice',
                                 'text': text, 'time': record.get('timestamp')})
        return
    content = message.get('content', '')
    if role == 'user' and isinstance(content, str):
        command, output = COMMAND.search(content), COMMAND_OUTPUT.search(content)
        if command:
            messages.append({'id': str(index), 'role': 'user', 'text': (command[1] + ' ' + (command[2] or '').strip()).strip(),
                             'time': record.get('timestamp')})
            return
        if output:
            result = activity.ANSI.sub('', output[1]).strip()
            if result:
                messages.append({'id': str(index), 'role': 'assistant', 'kind': 'terminal', 'text': result,
                                 'time': record.get('timestamp')})
            return
    text = content if isinstance(content, str) else '\n'.join(
        item.get('text', '') for item in content if item.get('type') == 'text')
    text = clean(text)
    if text.strip() and not text.startswith(('<system-reminder>', '<local-command', '<command-name>')):
        item = {'id': str(index), 'role': role, 'text': text, 'time': record.get('timestamp')}
        if role == 'assistant' and record.get('isApiErrorMessage') and error_message(dict(record, message=text)):
            item['kind'] = 'notice'
        messages.append(item)
    if role == 'assistant' and isinstance(content, list):
        for position, part in enumerate(content):
            if part.get('type') == 'tool_use':
                messages.append(activity.action(f'{index}.{position}', record.get('timestamp'),
                                                activity.claude_tool_line(part.get('name', 'Tool'), part.get('input'))))


def claude_snapshot(entry, clean):
    state = history.read(entry['transcript'], lambda: {'messages': [], 'dropped': 0},
                         lambda state, index, record: claude_consume(state['messages'], index, record, clean))
    messages = activity.group_actions(activity.merge(list(state['messages'])))
    metrics = {}
    try:
        metrics = json.loads((METRICS / (entry['thread'] + '.json')).read_text())
    except (OSError, ValueError):
        pass
    context = metrics.get('context_window', {})
    rate_metrics = metrics
    inherited = False
    if not metrics.get('rate_limits'):
        candidates = sorted(METRICS.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                candidate = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if any(isinstance(value, dict) and isinstance(value.get('resets_at'), (int, float))
                   and value['resets_at'] > time.time() and value.get('used_percentage') is not None
                   for value in (candidate.get('rate_limits') or {}).values()):
                rate_metrics = candidate
                inherited = True
                break
    rates = [{'label': name.replace('_', ' '), 'percent': value.get('used_percentage'),
              'resets_at': value.get('resets_at')}
             for name, value in (rate_metrics.get('rate_limits') or {}).items() if isinstance(value, dict)]
    report = usage({'percent': context.get('used_percentage'),
                    'tokens': context.get('total_input_tokens'),
                    'limit': context.get('context_window_size')}, rates, metrics.get('reported_at'))
    report.update(allowance_inherited=inherited, allowance_reported_at=rate_metrics.get('reported_at'),
                  model=(metrics.get('model') or {}).get('display_name') or (metrics.get('model') or {}).get('id') or '')
    return {'messages': merge_quota_notices(dict(entry, agent='claude'), messages), 'dropped': state['dropped'],
            'busy': entry.get('busy', False), 'usage': report}

"""Checks for session creation, shell input, and pre-conversation trust dialogs."""
import os
import subprocess
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

import antigravity
import server
import terminal_input as terminal


class SessionControlTests(unittest.TestCase):
    def test_shell_envelope_hides_empty_success_and_preserves_output(self):
        def envelope(command, result):
            return '<user_shell_command><command>' + command + '</command><result>' + result + '</result></user_shell_command>'
        text = server.display_text(envelope('cd server_portal', 'Exit code: 0\nDuration: 0.0206 seconds\nOutput:\n'))
        self.assertIn('cd server_portal', text)
        for hidden in ('user_shell_command', '<command>', 'Exit code', 'Duration', 'Output:'):
            self.assertNotIn(hidden, text)
        output = server.display_text(envelope('pwd', 'Exit code: 0\nOutput:\n/home/user\n'))
        self.assertIn('/home/user', output)
        failed = server.display_text(envelope('false', 'Exit code: 1\nOutput:\n'))
        self.assertIn('Exit code: 1', failed)

    def test_unnumbered_folder_trust_choices_are_explicit_and_bound(self):
        cursor = [0]
        keys = []
        def screen():
            return ('Accessing workspace:\n/tmp/project\n\nDo you trust the contents of this project?\n\n'
                    'Antigravity CLI requires permission to read, edit, and execute files here.\n\n' +
                    ('> ' if cursor[0] == 0 else '  ') + 'Yes, I trust this folder\n' +
                    ('> ' if cursor[0] == 1 else '  ') + 'No, exit\n\n↑/↓ Navigate · enter Confirm\n')
        def run(argv, **kwargs):
            if 'capture-pane' in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=screen())
            if 'send-keys' in argv:
                keys.append(argv[-1])
                if argv[-1] == 'Down':
                    cursor[0] += 1
            return subprocess.CompletedProcess(argv, 0, stdout='')
        request = terminal.approval(screen())
        self.assertTrue(request['actionable'])
        self.assertEqual([item['text'] for item in request['options']], ['Yes, I trust this folder', 'No, exit'])
        self.assertEqual(keys, [])
        with patch.object(terminal, 'verify'):
            terminal.respond_approval({'id': 'test'}, request['id'], choice=2, run=run)
        self.assertEqual(keys, ['Down', 'Enter'])

    def test_antigravity_bootstrap_does_not_require_a_transcript_or_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder) / 'proc'
            (proc / '123/fd').mkdir(parents=True)
            def path(value):
                return proc if str(value) == '/proc' else Path(value)
            with patch.object(antigravity, 'Path', side_effect=path), patch.object(antigravity, 'process_start', return_value='42'):
                entry = antigravity.live_entry([123])
            self.assertTrue(entry['writable'])
            self.assertTrue(entry['pending_conversation'])
            self.assertEqual(entry['thread'], 'pending-123-42')

    def test_antigravity_bootstrap_handles_multiple_pids_in_process_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder) / 'proc'
            (proc / '123/fd').mkdir(parents=True)
            (proc / '456/fd').mkdir(parents=True)
            (proc / '123/stat').write_text('123 (agy) S 1 123')
            (proc / '456/stat').write_text('456 (agy) S 123 123')
            def path(value):
                return proc if str(value) == '/proc' else Path(value)
            with patch.object(antigravity, 'Path', side_effect=path), patch.object(antigravity, 'process_start', return_value='42'):
                entry = antigravity.live_entry([123, 456])
            self.assertTrue(entry['writable'])
            self.assertTrue(entry['pending_conversation'])
            self.assertEqual(entry['owner_pid'], 123)
            self.assertEqual(entry['thread'], 'pending-123-42')

    def test_antigravity_active_conversation_discovered_from_cache(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache'
            cache.mkdir()
            (cache / 'test-conv.json').write_text(json.dumps({
                'conversation_id': 'test-conv-uuid',
                'cwd': '/tmp/my-project',
                'transcript_path': '/tmp/transcript.jsonl',
                'reported_at': time.time(),
            }))
            with patch.object(antigravity, 'CACHE', cache), patch.object(antigravity, 'process_start', return_value='42'):
                entry = antigravity.live_entry([123], cwd='/tmp/my-project')
            self.assertTrue(entry['writable'])
            self.assertEqual(entry['thread'], 'test-conv-uuid')
            self.assertEqual(entry['transcript'], '/tmp/transcript.jsonl')
            self.assertFalse(entry.get('pending_conversation', False))

    def test_antigravity_ignores_a_conversation_reported_before_the_process_started(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache'
            cache.mkdir()
            (cache / 'old.json').write_text(json.dumps({'conversation_id': 'old-uuid', 'cwd': '/tmp/p', 'reported_at': 1000.0}))
            with patch.object(antigravity, 'CACHE', cache), patch.object(antigravity, 'process_start', return_value='42'):
                entry = antigravity.live_entry([123], cwd='/tmp/p')
            self.assertTrue(entry['pending_conversation'])

    def test_start_agent_uses_direct_shortcuts_without_paths(self):
        self.assertEqual(terminal.LAUNCHERS, {'codex': 'codex', 'claude': 'claude', 'antigravity': 'agy'})
        for agent, cmd in (('codex', 'codex'), ('claude', 'claude'), ('antigravity', 'agy')):
            calls = []
            def run(argv, **kwargs):
                if 'load-buffer' in argv:
                    calls.append(('load-buffer', kwargs.get('input')))
                elif 'paste-buffer' in argv:
                    calls.append(('paste-buffer', argv))
                elif 'send-keys' in argv:
                    calls.append(('send-keys', argv[-1]))
                return subprocess.CompletedProcess(argv, 0, stdout='')
            entry = {'id': '%1', 'agent': 'shell'}
            with patch.object(terminal, 'shell_prompt', side_effect=['user@host:~$', 'user@host:~$', 'user@host:~$ ' + cmd]), \
                 patch.object(terminal.time, 'sleep'):
                terminal.start_agent(entry, agent, run=run)
            self.assertEqual(calls[0], ('load-buffer', cmd.encode()))
            self.assertEqual(calls[-1], ('send-keys', 'Enter'))

    def test_start_agent_handles_delayed_echo_and_whitespace_tolerances(self):
        entry = {'id': '%1', 'agent': 'shell'}
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout='')
        # Delayed echo: initial, pre-paste check, poll 1 (not arrived yet), poll 2 (arrived with extra spacing)
        with patch.object(terminal, 'shell_prompt', side_effect=['user@host:~$', 'user@host:~$', 'user@host:~$', 'user@host:~$  agy  ']), \
             patch.object(terminal.time, 'sleep'):
            terminal.start_agent(entry, 'antigravity', run=run)

        # Timeout failure if command never appears
        with patch.object(terminal, 'shell_prompt', return_value='user@host:~$'), \
             patch.object(terminal.time, 'monotonic', side_effect=[0, 0, 10]), \
             patch.object(terminal.time, 'sleep'):
            with self.assertRaises(RuntimeError) as ctx:
                terminal.start_agent(entry, 'antigravity', run=run)
            self.assertIn('Could not confirm the launch command', str(ctx.exception))

    def test_invalid_session_names_never_invoke_tmux(self):
        for name in ('', '../other', 'x; echo nope', '-option', 'x' * 65):
            with patch.object(terminal.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    terminal.create_session(name, '/tmp', run=run)
                run.assert_not_called()

    def test_real_tmux_session_creation_and_no_agent_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            socket = str(folder / 'tmux.sock')
            (folder / 'child').mkdir()
            subprocess.run(['tmux', '-S', socket, 'new-session', '-d', '-s', 'seed', 'bash', '--noprofile', '--norc'], check=True)
            def run(argv, **kwargs):
                return subprocess.run(['tmux', '-S', socket, *argv[1:]], **kwargs)
            try:
                pane = terminal.create_session('work', str(folder), run=run)
                time.sleep(.3)
                pid = int(run(['/usr/bin/tmux', 'display-message', '-p', '-t', pane, '#{pane_pid}'], capture_output=True, text=True).stdout)
                entry = {'id': pane, 'agent': 'shell', 'pane_pid': pid, 'pane_start': terminal.process_start(pid)}
                with self.assertRaises(RuntimeError):
                    terminal.create_session('work', str(folder), run=run)
                terminal.shell_submit(entry, 'cd child', run=run)
                time.sleep(.15)
                cwd = run(['/usr/bin/tmux', 'display-message', '-p', '-t', pane, '#{pane_current_path}'], capture_output=True, text=True).stdout.strip()
                self.assertEqual(cwd, str(folder / 'child'))
                terminal.shell_submit(entry, 'printf shell-command-ok > result.txt', run=run)
                time.sleep(.15)
                self.assertEqual((folder / 'child/result.txt').read_text(), 'shell-command-ok')
                run(['/usr/bin/tmux', 'send-keys', '-t', pane, '-l', 'keep this draft'], check=True)
                time.sleep(.1)
                with self.assertRaises(RuntimeError):
                    terminal.shell_submit(entry, 'replacement', run=run)
            finally:
                subprocess.run(['tmux', '-S', socket, 'kill-server'], check=False)


class FakeClaudePane:
    """Stateful stand-in for tmux: tracks the text in Claude's input box."""
    def __init__(self, text='', swallow_enters=0, ghost=False):
        self.ghost = ghost
        self.text, self.swallow, self.enters, self.pending = text, swallow_enters, 0, None

    def __call__(self, argv, **kwargs):
        from types import SimpleNamespace
        cmd = argv[1]
        if cmd == 'capture-pane':
            if '-e' in argv:
                body = '\x1b[2m' + self.text.replace(' ', '\x1b[0m \x1b[2m') + '\x1b[0m' if self.ghost else self.text
                return SimpleNamespace(stdout='─' * 10 + '\n\x1b[39m❯\xa0' + body + '\n', returncode=0)
            return SimpleNamespace(stdout='done\n' + '\u2500' * 10 + '\n\u276f ' + self.text + '\n' + '\u2500' * 10 + '\n', returncode=0)
        if cmd == 'load-buffer':
            self.pending = kwargs['input'].decode()
        elif cmd == 'paste-buffer':
            if self.ghost:  # real input replaces the dimmed suggestion
                self.text, self.ghost = '', False
            self.text += self.pending
        elif cmd == 'send-keys':
            for key in argv[4:]:
                if key == 'Enter':
                    self.enters += 1
                    if self.swallow:
                        self.swallow -= 1
                    else:
                        self.text = ''
                elif key in ('C-u', 'BSpace'):
                    self.text = ''
        return SimpleNamespace(stdout='', returncode=0)


class DraftHandlingTests(unittest.TestCase):
    entry = {'id': '%1', 'agent': 'claude'}

    def setUp(self):
        for target in (patch.object(terminal, 'verify'), patch.object(terminal.time, 'sleep')):
            target.start()
            self.addCleanup(target.stop)

    def test_existing_draft_blocks_send_with_draft_error(self):
        pane = FakeClaudePane('push it')
        with self.assertRaises(terminal.DraftError):
            terminal.submit(self.entry, 'hello', run=pane)
        self.assertEqual(pane.enters, 0)

    def test_dim_prompt_suggestion_is_not_a_draft(self):
        pane = FakeClaudePane('push it', ghost=True)
        terminal.submit(self.entry, 'hello', run=pane)
        self.assertEqual(pane.enters, 1)

    def test_clear_draft_empties_box_then_send_works(self):
        pane = FakeClaudePane('push it')
        terminal.clear_draft(self.entry, run=pane)
        self.assertEqual(pane.text, '')
        terminal.submit(self.entry, 'hello', run=pane)
        self.assertEqual(pane.enters, 1)

    def test_clear_draft_is_noop_when_empty(self):
        pane = FakeClaudePane('')
        terminal.clear_draft(self.entry, run=pane)
        self.assertEqual(pane.enters, 0)

    def test_swallowed_enter_is_retried(self):
        pane = FakeClaudePane(swallow_enters=2)
        terminal.submit(self.entry, 'hello', run=pane)
        self.assertEqual(pane.enters, 3)
        self.assertEqual(pane.text, '')

    def test_enter_never_registering_raises_draft_error(self):
        pane = FakeClaudePane(swallow_enters=99)
        with self.assertRaises(terminal.DraftError):
            terminal.submit(self.entry, 'hello', run=pane)
        self.assertEqual(pane.enters, 4)


class TranscriptParsingTests(unittest.TestCase):
    def write(self, rows):
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(folder, ignore_errors=True))
        path = Path(folder) / 't.jsonl'
        path.write_text('\n'.join(__import__('json').dumps(row) for row in rows))
        return str(path)

    def claude(self, rows):
        import agents
        entry = {'transcript': self.write(rows), 'thread': 'none', 'id': 'x'}
        return agents.claude_snapshot(entry, server.display_text)['messages']

    def test_claude_debug_copies_do_not_duplicate_messages(self):
        text = [{'type': 'text', 'text': 'hello there'}]
        rows = [{'type': 'user', 'message': {'role': 'user', 'content': 'hello there'}},
                {'type': 'api-request-blob', 'message': {'role': 'user', 'content': text}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hi'}]}},
                {'type': 'api-request-blob', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hi'}]}}]
        self.assertEqual([m['text'] for m in self.claude(rows)], ['hello there', 'hi'])

    def test_claude_queued_message_commands_and_tool_calls(self):
        rows = [{'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': 'sent mid-turn'}},
                {'type': 'user', 'message': {'role': 'user', 'content': '<command-name>/status</command-name>\n<command-message>status</command-message>\n<command-args></command-args>'}},
                {'type': 'user', 'message': {'role': 'user', 'content': '<local-command-stdout>\x1b[1mAll good\x1b[0m</local-command-stdout>'}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'git status'}}]}}]
        messages = self.claude(rows)
        self.assertEqual([(m['role'], m.get('kind'), m['text']) for m in messages], [
            ('user', None, 'sent mid-turn'), ('user', None, '/status'), ('assistant', 'terminal', 'All good'),
            ('assistant', 'actions', 'Ran git status')])

    def test_codex_compaction_abort_and_commands(self):
        row = lambda kind, payload: {'type': kind, 'payload': payload, 'timestamp': '2026-01-01T00:00:00Z'}
        rows = [row('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'input': 'text(await tools.exec_command({cmd:"git status --short"}))'}),
                row('response_item', {'type': 'function_call', 'name': 'wait', 'arguments': '{"cell_id":"11"}'}),
                row('compacted', {'message': ''}),
                row('event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'})]
        messages = server.snapshot({'agent': 'codex', 'transcript': self.write(rows), 'thread': 'none', 'id': 'x', 'writable': True})['messages']
        self.assertEqual([(m.get('kind'), m['text']) for m in messages], [
            ('actions', 'Ran git status --short'), ('notice', 'Context compacted.'), ('notice', 'Turn interrupted.')])

    def test_antigravity_checkpoint_error_and_tool_calls(self):
        import agents, antigravity
        rows = [{'step_index': 0, 'type': 'USER_INPUT', 'content': '<USER_REQUEST>go</USER_REQUEST>'},
                {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'content': '', 'tool_calls': [{'name': 'view_file', 'args': {'AbsolutePath': '/x.md'}}]},
                {'step_index': 2, 'type': 'CHECKPOINT', 'content': '# Resuming from a compaction'},
                {'step_index': 3, 'type': 'ERROR_MESSAGE', 'content': 'Error: The stream was interrupted.'}]
        entry = {'transcript': self.write(rows), 'thread': 'none-thread', 'id': 'x', 'writable': True}
        messages = antigravity.snapshot(entry, server.display_text, agents.records, agents.usage)['messages']
        self.assertEqual([(m.get('kind'), m['text']) for m in messages], [
            (None, 'go'), ('actions', 'view_file'), ('notice', 'Context compacted.'), ('notice', 'Error: The stream was interrupted.')])


class ActionGroupTests(unittest.TestCase):
    def test_consecutive_actions_fold_into_one_message(self):
        import activity
        rows = [{'id': '1', 'role': 'user', 'text': 'go'}, activity.action('2.0', 't', 'Ran ls'), activity.action('2.1', 't', 'Edited a.py'),
                {'id': '3', 'role': 'assistant', 'text': 'done'}, activity.action('4.0', 't', 'Ran pytest')]
        grouped = activity.group_actions(rows)
        self.assertEqual([(m.get('kind'), m['text'], m.get('count')) for m in grouped],
                         [(None, 'go', None), ('actions', 'Ran ls\nEdited a.py', 2), (None, 'done', None), ('actions', 'Ran pytest', 1)])

    def test_claude_tool_lines(self):
        import activity
        self.assertEqual(activity.claude_tool_line('Bash', {'command': 'git status\nls', 'description': 'Check tree'}), 'Ran Check tree: git status')
        self.assertEqual(activity.claude_tool_line('Edit', {'file_path': '/a/b/app.js'}), 'Edited app.js')
        self.assertEqual(activity.claude_tool_line('Grep', {'pattern': 'foo'}), 'Searched foo')


class CodexApprovalWordingTests(unittest.TestCase):
    PROMPTS = {
        'edits': ('Would you like to make the following edits?', ['Yes, proceed (y)', "Yes, and don't ask again for these files (a)", 'No, and tell Codex what to do differently (esc)']),
        'permissions': ('Would you like to grant these permissions?', ['Yes, grant these permissions for this turn (y)', 'Yes, grant these permissions for this session (a)', 'No, continue without permissions (esc)']),
        'terminal': ('Would you like to send input to the existing terminal?', ['Yes, proceed (y)', 'No, and tell Codex what to do differently (esc)']),
        'network': ('Do you want to approve network access to "api.example.com"?', ['Yes, just this once (y)', 'Yes, and allow this host for this conversation (a)', 'No, and block this host in the future (esc)']),
        'command': ('Would you like to run the following command?', ['Yes, proceed (y)', 'No, and tell Codex what to do differently (esc)']),
    }

    def test_every_codex_question_becomes_clickable(self):
        for name, (question, options) in self.PROMPTS.items():
            lines = ['  ' + question, '', '  $ some request', ''] + [('› ' if index == 0 else '  ') + '%d. %s' % (index + 1, text) for index, text in enumerate(options)]
            request = terminal.approval('\n'.join(lines))
            self.assertIsNotNone(request, name)
            self.assertEqual(len(request['options']), len(options), name)

    def test_a_plain_numbered_menu_without_a_refusal_is_not_an_approval(self):
        screen = 'Would you like to pick a color?\n› 1. Red\n  2. Blue\n'
        self.assertIsNone(terminal.approval(screen))


class CodexTimeMatchTests(unittest.TestCase):
    def test_each_process_owns_the_conversations_begun_after_it_started(self):
        import sqlite3, agents
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE threads (id, rollout_path, cwd, created_at_ms, updated_at_ms, source, archived)')
        db.execute("INSERT INTO threads VALUES ('old','/o','/a',1000000,9000000,'vscode',0)")
        db.execute("INSERT INTO threads VALUES ('new','/n','/b',5000000,6000000,'vscode',0)")
        entries = [{'agent': 'codex', 'process_start': '1', 'pending_conversation': True},
                   {'agent': 'codex', 'process_start': '2', 'pending_conversation': True}]
        with patch.object(agents, 'started_ms', side_effect=lambda ticks: {'1': 1000000, '2': 5000000}[ticks]):
            agents.match_unresolved_codex(db, entries)
        self.assertEqual([e['thread'] for e in entries], ['old', 'new'])
        self.assertFalse(any(e.get('pending_conversation') for e in entries))


class ShellLogTests(unittest.TestCase):
    def setUp(self):
        import shell_log, tempfile
        self.shell_log = shell_log
        self.directory = tempfile.mkdtemp()
        self.patch = patch.object(shell_log, 'CACHE', Path(self.directory) / 'cache')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_cd_sticks_and_output_is_recorded(self):
        run = self.shell_log.run
        run('%1', 'cd /tmp', '/')
        message = run('%1', 'pwd', '/')
        self.assertIn('/tmp $ pwd\n/tmp', message['text'])
        self.assertEqual(self.shell_log.current('%1', '/'), '/tmp')

    def test_failure_shows_exit_code_and_merges_by_time(self):
        message = self.shell_log.run('%2', 'ls /definitely/not/here', '/tmp')
        self.assertIn('[exit ', message['text'])
        merged = self.shell_log.merge('%2', [{'id': '1', 'role': 'user', 'text': 'x', 'time': '2000-01-01T00:00:00Z'}])
        self.assertEqual([item['id'] for item in merged], ['1', message['id']])

    def test_empty_command_is_refused(self):
        with self.assertRaises(ValueError):
            self.shell_log.run('%3', '   ', '/tmp')


class AttentionTests(unittest.TestCase):
    CLAUDE = {'id': '%a', 'agent': 'claude'}
    MENU = ('Which look should every plate match?\n\n\u276f 1. Liber I\n  2. New style\n  3. Type something.\n'
            '\nEnter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel\n')
    IDLE = 'done\n' + '\u2500' * 10 + '\n\u276f \n' + '\u2500' * 10 + '\n  auto mode on\n'

    def setUp(self):
        terminal._attention_seen.clear()

    def test_unrecognized_menu_is_reported_only_after_the_delay(self):
        self.assertIsNone(terminal.attention(self.CLAUDE, self.MENU, False, now=0))
        self.assertIsNone(terminal.attention(self.CLAUDE, self.MENU, False, now=1))
        found = terminal.attention(self.CLAUDE, self.MENU, False, now=4)
        self.assertEqual(found['reason'], 'menu')
        self.assertIn('Type something.', found['text'])

    def test_menu_is_split_into_question_and_options(self):
        terminal.attention(self.CLAUDE, self.MENU, False, now=0)
        found = terminal.attention(self.CLAUDE, self.MENU, False, now=4)
        self.assertEqual(found['question'], 'Which look should every plate match?')
        self.assertEqual([option['id'] for option in found['options']], [1, 2, 3])
        self.assertEqual(found['options'][1]['text'], 'New style')

    def test_moving_the_cursor_does_not_restart_the_delay(self):
        terminal.attention(self.CLAUDE, self.MENU, False, now=0)
        moved = self.MENU.replace('\u276f 1.', '  1.').replace('  2. New', '\u276f 2. New')
        self.assertIsNotNone(terminal.attention(self.CLAUDE, moved, False, now=5))

    def test_normal_idle_and_working_screens_are_not_flagged(self):
        for now in (0, 10):
            self.assertIsNone(terminal.attention(self.CLAUDE, self.IDLE, False, now=now))
            self.assertIsNone(terminal.attention(self.CLAUDE, 'Crafting\u2026\nno input box here', True, now=now))

    def test_text_and_yes_no_prompts_and_missing_input_box(self):
        for screen, reason in (('Install now? (y/n)\n', 'prompt'), ('Open this URL to log in\nhttps://x\n', 'no-input-box')):
            terminal._attention_seen.clear()
            terminal.attention(self.CLAUDE, screen, False, now=0)
            self.assertEqual(terminal.attention(self.CLAUDE, screen, False, now=5)['reason'], reason)

    def test_live_status_reports_input_needed(self):
        entry = dict(self.CLAUDE, owner_pid=1, writable=True, registry='/nonexistent')
        run = lambda argv, **kwargs: __import__('types').SimpleNamespace(stdout=self.MENU, returncode=0)
        with patch.object(terminal, 'capture_quota_notices'), patch.object(terminal.time, 'monotonic', side_effect=[0, 5]):
            self.assertEqual(terminal.live_status(entry, run)['state'], 'Idle')
            status = terminal.live_status(entry, run)
        self.assertEqual(status['state'], 'Input needed')
        self.assertEqual(status['attention']['reason'], 'menu')

    def test_type_text_validates_and_sends_literal_text(self):
        sent = []
        run = lambda argv, **kwargs: sent.append(argv[3:])
        with patch.object(terminal, 'verify'):
            terminal.type_text(self.CLAUDE, 'my answer', enter=True, run=run)
            for bad in ('', 'x' * 501, 'a\x1bb', None):
                with self.assertRaises(ValueError):
                    terminal.type_text(self.CLAUDE, bad, run=run)
        self.assertEqual(sent, [['%a', '-l', 'my answer'], ['%a', 'Enter']])


class SuggestionTests(unittest.TestCase):
    CLAUDE = {'id': '%s', 'agent': 'claude'}

    def styled(self, body):
        return '\u2500' * 8 + '\n\x1b[39m\u276f\xa0' + body + '\n' + '\u2500' * 8 + '\n'

    def test_dim_text_is_a_suggestion_and_typed_text_is_not(self):
        self.assertEqual(terminal.dim_prompt_text(self.styled('\x1b[2mpush\x1b[0m \x1b[2mit\x1b[0m')), 'push it')
        self.assertEqual(terminal.dim_prompt_text(self.styled('typed by me')), '')
        self.assertEqual(terminal.dim_prompt_text(self.styled('\x1b[2mdim\x1b[0m but \x1b[2mmixed\x1b[0m')), '')

    def test_placeholders_are_not_suggestions(self):
        for hint in ('Press up to edit queued messages', 'Try "fix the tests"'):
            run = lambda argv, hint=hint, **kwargs: __import__('types').SimpleNamespace(stdout=self.styled('\x1b[2m' + hint + '\x1b[0m'), returncode=0)
            self.assertEqual(terminal.suggestion(self.CLAUDE, run), '')

    def test_live_status_exposes_suggestion_only_when_idle(self):
        entry = dict(self.CLAUDE, owner_pid=1, writable=True, registry='/nonexistent')
        screen = 'done\n' + '\u2500' * 8 + '\n\u276f\xa0push it\n' + '\u2500' * 8 + '\n  auto mode on\n'
        def run(argv, **kwargs):
            out = self.styled('\x1b[2mpush it\x1b[0m') if '-e' in argv else screen
            return __import__('types').SimpleNamespace(stdout=out, returncode=0)
        with patch.object(terminal, 'capture_quota_notices'):
            self.assertEqual(terminal.live_status(entry, run).get('suggestion'), 'push it')


class StatusTests(unittest.TestCase):
    def rollout(self, *events):
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(folder, ignore_errors=True))
        path = Path(folder) / 'r.jsonl'
        path.write_text('\n'.join(__import__('json').dumps({'type': 'event_msg', 'payload': {'type': kind}}) for kind in events))
        return str(path)

    def test_transcript_busy_follows_the_last_lifecycle_event(self):
        self.assertTrue(terminal.transcript_busy(self.rollout('task_started', 'token_count')))
        self.assertFalse(terminal.transcript_busy(self.rollout('task_started', 'task_complete')))
        self.assertFalse(terminal.transcript_busy(self.rollout('task_started', 'turn_aborted')))
        self.assertTrue(terminal.transcript_busy(self.rollout('task_started', 'task_complete', 'task_started')))
        self.assertFalse(terminal.transcript_busy('/nonexistent'))
        self.assertFalse(terminal.transcript_busy(None))

    def status(self, agent, screen, **extra):
        entry = dict({'id': '%t', 'agent': agent, 'owner_pid': 1, 'writable': True, 'registry': '/nonexistent'}, **extra)
        run = lambda argv, **kwargs: __import__('types').SimpleNamespace(stdout=screen, returncode=0)
        terminal._attention_seen.clear()
        with patch.object(terminal, 'capture_quota_notices'):
            return terminal.live_status(entry, run)['state']

    def test_any_claude_spinner_word_counts_as_working(self):
        idle = '\u2500' * 8 + '\n\u276f\xa0\n' + '\u2500' * 8 + '\n'
        for verb in ('Crafting', 'Pondering', 'Brewing', 'Hashing'):
            self.assertEqual(self.status('claude', '* ' + verb + '\u2026 (12s)\n' + idle), 'Working')
        self.assertEqual(self.status('claude', 'Done.\n' + idle), 'Idle')

    def test_codex_tab_state_uses_its_transcript(self):
        idle = '\u2500' * 8 + '\n\u203a \n'
        self.assertEqual(self.status('codex', idle, transcript=self.rollout('task_started')), 'Working')
        self.assertEqual(self.status('codex', idle, transcript=self.rollout('task_started', 'task_complete')), 'Idle')


class StreamTests(unittest.TestCase):
    def setUp(self):
        proxy = patch.object(server.auth, 'MODE', 'proxy')  # these tests exercise the endpoints, not sign-in (see test_auth.py)
        proxy.start()
        self.addCleanup(proxy.stop)

    def test_changes_are_pushed_and_unchanged_payloads_are_not_repeated(self):
        import http.client, json, threading
        from http.server import ThreadingHTTPServer
        shared = {'n': 0}
        config = {'id': '%x', 'agent': 'claude', 'transcript': None}
        with patch.object(server, 'choose', return_value=config), \
             patch.object(server, 'change_signature', side_effect=lambda c: (shared['n'] // 10,)), \
             patch.object(server, 'chat_payload', side_effect=lambda c, shell=False, limit=150: {'messages': [], 'value': shared['n'] // 10}):
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.shutdown)
            connection = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=5)
            self.addCleanup(connection.close)
            connection.request('GET', '/mobile/api/stream?session=%25x')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type').split(';')[0], 'text/event-stream')
            self.assertEqual(response.getheader('X-Accel-Buffering'), 'no')

            def next_event():
                while True:
                    line = response.fp.readline().decode()
                    if line.startswith('data:'):
                        return json.loads(line[5:])

            self.assertEqual(next_event()['value'], 0)
            shared['n'] = 10
            started = time.monotonic()
            self.assertEqual(next_event()['value'], 1)
            self.assertLess(time.monotonic() - started, 2)


class PushTests(unittest.TestCase):
    ENTRY = {'id': '%5', 'label': 'work', 'agent': 'claude'}

    def notifier(self, watching=False):
        self.sent, self.clock = [], [0]
        return __import__('push').Notifier(lambda payload, urgent: self.sent.append((payload['body'], urgent)),
                                           lambda session: watching, clock=lambda: self.clock[0])

    def step(self, notifier, seconds, state, **extra):
        self.clock[0] += seconds
        notifier.observe(self.ENTRY, dict({'state': state}, **extra))

    def test_finishing_a_real_turn_notifies_once_after_it_settles(self):
        n = self.notifier()
        self.step(n, 0, 'Working'); self.step(n, 10, 'Working')
        self.step(n, 2, 'Idle'); self.assertEqual(self.sent, [])
        self.step(n, 5, 'Idle'); self.step(n, 5, 'Idle')
        self.assertEqual(self.sent, [('Finished', False)])

    def test_trivial_turns_and_idle_polling_never_notify(self):
        n = self.notifier()
        self.step(n, 0, 'Working'); self.step(n, 2, 'Working'); self.step(n, 2, 'Idle')
        for _ in range(5):
            self.step(n, 5, 'Idle')
        self.step(n, 5, 'Idle')
        self.assertEqual(self.sent, [])

    def test_approvals_and_input_requests_notify_urgently_and_new_requests_notify_again(self):
        n = self.notifier()
        self.step(n, 0, 'Approval', approval={'id': 'a'}); self.step(n, 5, 'Approval', approval={'id': 'a'}); self.step(n, 5, 'Approval', approval={'id': 'a'})
        self.step(n, 5, 'Approval', approval={'id': 'b'}); self.step(n, 5, 'Approval', approval={'id': 'b'})
        self.step(n, 5, 'Input needed', attention={'id': 'c'}); self.step(n, 5, 'Input needed', attention={'id': 'c'})
        self.assertEqual(self.sent, [('Needs approval', True), ('Needs approval', True), ('Needs your input', True)])

    def test_nothing_is_sent_while_the_page_is_being_watched(self):
        n = self.notifier(watching=True)
        self.step(n, 0, 'Approval', approval={'id': 'a'}); self.step(n, 5, 'Approval', approval={'id': 'a'}); self.step(n, 5, 'Approval', approval={'id': 'a'})
        self.assertEqual(self.sent, [])

    def test_only_real_push_services_are_accepted_as_endpoints(self):
        import push
        keys = {'p256dh': 'k', 'auth': 'a'}
        for endpoint, ok in (('https://fcm.googleapis.com/fcm/send/x', True), ('https://updates.push.services.mozilla.com/wpush/v2/x', True),
                             ('https://web.push.apple.com/x', True), ('http://fcm.googleapis.com/x', False),
                             ('https://evil.example/fcm.googleapis.com', False), ('https://googleapis.com.evil.example/x', False),
                             ('https://127.0.0.1/x', False)):
            self.assertEqual(push.valid_subscription({'endpoint': endpoint, 'keys': keys}), ok, endpoint)
        self.assertFalse(push.valid_subscription({'endpoint': 'https://fcm.googleapis.com/x', 'keys': {}}))

    def test_subscriptions_round_trip_and_dropped_ones_are_forgotten(self):
        import push
        store = Path(tempfile.mkdtemp()) / 'subs.json'
        self.addCleanup(lambda: __import__('shutil').rmtree(store.parent, ignore_errors=True))
        subscription = {'endpoint': 'https://fcm.googleapis.com/fcm/send/abc', 'keys': {'p256dh': 'k', 'auth': 'a'}}
        push.subscribe(subscription, store)
        self.assertIn(subscription['endpoint'], push.load(store))
        self.assertEqual(oct(store.stat().st_mode & 0o777), '0o600')
        with self.assertRaises(ValueError):
            push.subscribe({'endpoint': 'https://evil.example/x', 'keys': subscription['keys']}, store)

        class Gone(push.WebPushException if push.available() else Exception):
            def __init__(self):
                self.response = __import__('types').SimpleNamespace(status_code=410)
        def sender(**kwargs):
            raise Gone()
        if push.available():
            with patch.object(push, 'vapid_keys', return_value=(self.pem, 'pub')):
                push.send_all({'title': 't'}, store=store, sender=sender)
            self.assertEqual(push.load(store), {})

    @classmethod
    def setUpClass(cls):
        import push
        if push.available():
            vapid = push.Vapid02(); vapid.generate_keys(); cls.pem = vapid.private_pem().decode()

    def test_push_library_can_encrypt_for_a_real_browser_key(self):
        import push
        if not push.available():
            self.skipTest('pywebpush is not installed')
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        client = ec.generate_private_key(ec.SECP256R1())
        public = client.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        subscription = {'endpoint': 'https://fcm.googleapis.com/fcm/send/abc', 'keys': {'p256dh': push.b64url(public), 'auth': push.b64url(os.urandom(16))}}
        from pywebpush import WebPusher
        encrypted = WebPusher(subscription).encode(b'{"title":"hi"}', 'aes128gcm')
        self.assertGreater(len(encrypted['body']), 20)
        self.assertNotIn(b'hi', encrypted['body'])

    def test_vapid_identity_is_created_once_with_private_permissions(self):
        import push
        if not push.available():
            self.skipTest('pywebpush is not installed')
        path = Path(tempfile.mkdtemp()) / 'vapid.json'
        self.addCleanup(lambda: __import__('shutil').rmtree(path.parent, ignore_errors=True))
        first = push.vapid_keys(path)
        self.assertEqual(first, push.vapid_keys(path))
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(len(__import__('base64').urlsafe_b64decode(first[1] + '==')), 65)


class HistoryCacheTests(unittest.TestCase):
    def setUp(self):
        import history
        self.history = history
        history.forget()
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.folder, ignore_errors=True))
        self.path = self.folder / 'h.jsonl'
        self.calls = []

    def read(self):
        def consume(state, index, record):
            self.calls.append(index)
            state.append((index, record['n']))
        return list(self.history.read(self.path, list, consume))

    def write(self, text, mode='w'):
        with open(self.path, mode) as stream:
            stream.write(text)

    def test_only_appended_records_are_parsed_on_later_reads(self):
        self.write('{"n":0}\n{"n":1}\n')
        self.assertEqual(self.read(), [(0, 0), (1, 1)])
        self.write('{"n":2}\n', 'a')
        self.assertEqual(self.read(), [(0, 0), (1, 1), (2, 2)])
        self.assertEqual(self.calls, [0, 1, 2])  # each record consumed exactly once

    def test_a_record_still_being_written_waits_for_its_end(self):
        self.write('{"n":0}\n{"n":')
        self.assertEqual(self.read(), [(0, 0)])
        self.write('1}\n', 'a')
        self.assertEqual(self.read(), [(0, 0), (1, 1)])
        self.assertEqual(self.calls, [0, 1])

    def test_a_final_record_without_a_newline_is_read_once_and_not_duplicated(self):
        self.write('{"n":0}\n{"n":1}')
        self.assertEqual(self.read(), [(0, 0), (1, 1)])
        self.write('\n{"n":2}\n', 'a')
        self.assertEqual(self.read(), [(0, 0), (1, 1), (2, 2)])
        self.assertEqual(self.calls, [0, 1, 2])

    def test_invalid_lines_are_skipped_but_still_numbered(self):
        self.write('{"n":0}\nnot json\n{"n":2}\n')
        self.assertEqual(self.read(), [(0, 0), (2, 2)])

    def test_a_replaced_or_truncated_file_is_read_again_from_the_start(self):
        self.write('{"n":0}\n{"n":1}\n{"n":2}\n')
        self.read()
        self.write('{"n":9}\n')
        self.assertEqual(self.read(), [(0, 9)])

    def test_missing_file_gives_an_empty_state(self):
        self.assertEqual(self.read(), [])


class WindowTests(unittest.TestCase):
    def payload(self, count, limit):
        messages = [{'id': str(i), 'role': 'user', 'text': 'm' + str(i)} for i in range(count)]
        config = {'id': '%w', 'agent': 'claude'}
        with patch.object(server, 'live_status', return_value={'state': 'Idle', 'approval': None}), \
             patch.object(server, 'snapshot', return_value={'messages': messages, 'busy': False}):
            return server.chat_payload(config, False, limit)

    def test_only_the_latest_messages_are_sent_and_the_rest_are_counted(self):
        data = self.payload(500, 150)
        self.assertEqual(len(data['messages']), 150)
        self.assertEqual(data['messages'][0]['id'], '350')
        self.assertEqual(data['messages'][-1]['id'], '499')
        self.assertEqual((data['earlier'], data['limit']), (350, 150))

    def test_short_chats_are_sent_whole(self):
        data = self.payload(40, 150)
        self.assertEqual((len(data['messages']), data['earlier']), (40, 0))

    def test_limit_parameter_is_clamped_and_defaults_safely(self):
        self.assertEqual(server.window_limit({}), 150)
        self.assertEqual(server.window_limit({'limit': ['300']}), 300)
        self.assertEqual(server.window_limit({'limit': ['1']}), 20)
        self.assertEqual(server.window_limit({'limit': ['999999']}), 2000)
        self.assertEqual(server.window_limit({'limit': ['abc']}), 150)


class LiveScreenTests(unittest.TestCase):
    ENTRY = {'id': '%9', 'agent': 'claude'}

    def setUp(self):
        proxy = patch.object(server.auth, 'MODE', 'proxy')  # these tests exercise the endpoints, not sign-in (see test_auth.py)
        proxy.start()
        self.addCleanup(proxy.stop)

    def fake(self, stdout):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return __import__('types').SimpleNamespace(stdout=stdout, returncode=0)
        return run, calls

    def test_capture_requests_scrollback_and_trims_blank_ends_and_trailing_spaces(self):
        run, calls = self.fake('line one   \n\n  indented\n\n\n')
        self.assertEqual(terminal.capture_screen(self.ENTRY, 80, run), 'line one\n\n  indented')
        self.assertEqual(calls[0][:6], ['/usr/bin/tmux', 'capture-pane', '-p', '-t', '%9', '-S'])
        self.assertEqual(calls[0][6], '-80')

    def test_only_the_last_lines_are_kept_and_the_limit_is_clamped(self):
        text = '\n'.join('row %d' % i for i in range(1000))
        run, calls = self.fake(text)
        kept = terminal.capture_screen(self.ENTRY, 30, run).splitlines()
        self.assertEqual((len(kept), kept[-1]), (30, 'row 999'))
        for asked, used in ((1, 20), (99999, 500)):
            run, calls = self.fake(text)
            self.assertEqual(len(terminal.capture_screen(self.ENTRY, asked, run).splitlines()), used)
            self.assertEqual(calls[0][6], '-' + str(used))

    def test_endpoint_returns_the_screen_of_a_discovered_session_only(self):
        import http.client, json, threading
        from http.server import ThreadingHTTPServer
        seen = []
        with patch.object(server, 'choose', side_effect=lambda ident: seen.append(ident) or {'id': '%9', 'agent': 'claude'}), \
             patch.object(server, 'capture_screen', side_effect=lambda config, lines: 'screen of %s (%d)' % (config['id'], lines)):
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.shutdown)
            def get(query):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=5)
                connection.request('GET', '/mobile/api/screen?' + query)
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result
            self.assertEqual(get('session=%259&lines=120'), (200, {'text': 'screen of %9 (120)', 'session': '%9', 'agent': 'claude'}))
            self.assertEqual(get('session=%259&lines=abc')[1]['text'], 'screen of %9 (80)')
            self.assertEqual(seen, ['%9', '%9'])  # the pane always comes from the server's own session list


class CacheCapTests(unittest.TestCase):
    def setUp(self):
        import history
        self.history = history
        history.forget()
        for name, value in (('KEEP_MESSAGES', 50), ('TRIM_SLACK', 10)):
            patcher = patch.object(history, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.folder, ignore_errors=True))

    def transcript(self, count, start=0):
        import json
        path = self.folder / 't.jsonl'
        lines = [json.dumps({'type': 'user' if i % 2 == 0 else 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
                             'message': {'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'message %d' % i}}) for i in range(start, start + count)]
        with open(path, 'a' if start else 'w') as stream:
            stream.write('\n'.join(lines) + '\n')
        return path

    def snapshot(self, path):
        import agents
        return agents.claude_snapshot({'transcript': str(path), 'thread': 'none', 'id': 'x'}, server.display_text)

    def test_memory_stays_bounded_and_the_total_stays_truthful(self):
        path = self.transcript(300)
        data = self.snapshot(path)
        state = self.history._states[str(path)]['state']
        self.assertLessEqual(len(state['messages']), 50 + 10)
        self.assertEqual(len(data['messages']) + data['dropped'], 300)
        self.transcript(200, start=300)
        data = self.snapshot(path)
        self.assertLessEqual(len(self.history._states[str(path)]['state']['messages']), 50 + 10)
        self.assertEqual(len(data['messages']) + data['dropped'], 500)

    def test_the_messages_that_remain_are_exactly_the_latest_ones_with_their_original_ids(self):
        path = self.transcript(300)
        kept = self.snapshot(path)['messages']
        self.history.forget()
        with patch.object(self.history, 'KEEP_MESSAGES', 10 ** 6):
            full = self.snapshot(path)['messages']
        self.assertEqual(kept, full[-len(kept):])
        self.assertEqual(full[-1]['id'], '299')

    def test_the_first_load_of_a_huge_file_is_trimmed_while_reading(self):
        path = self.transcript(6000)
        seen = []
        original = self.history.keep_tail
        def spy(state):
            seen.append(len(state['messages']))
            original(state)
        with patch.object(self.history, 'keep_tail', spy):
            self.snapshot(path)
        self.assertGreater(len(seen), 1)  # trimmed repeatedly, not once at the end
        self.assertLess(max(seen), 2000 + 61)

    def test_payload_counts_dropped_messages_as_earlier_and_reports_the_ceiling(self):
        messages = [{'id': str(i), 'role': 'user', 'text': 'm%d' % i} for i in range(300)]
        config = {'id': '%w', 'agent': 'claude'}
        with patch.object(server, 'live_status', return_value={'state': 'Idle', 'approval': None}), \
             patch.object(server, 'snapshot', return_value={'messages': messages, 'dropped': 5000, 'busy': False}):
            data = server.chat_payload(config, False, 100)
        self.assertEqual((len(data['messages']), data['earlier'], data['max_limit']), (100, 5200, 2000))
        self.assertNotIn('dropped', data)

    def test_codex_history_is_capped_the_same_way(self):
        import json
        path = self.folder / 'rollout.jsonl'
        rows = [{'type': 'response_item', 'timestamp': '2026-01-01T00:00:00Z',
                 'payload': {'type': 'message', 'role': 'user' if i % 2 == 0 else 'assistant', 'content': [{'type': 'input_text', 'text': 'turn %d' % i}]}}
                for i in range(400)]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        data = server.snapshot({'agent': 'codex', 'transcript': str(path), 'thread': 'none', 'id': 'x', 'writable': True})
        self.assertEqual(len(data['messages']) + data['dropped'], 400)
        self.assertLessEqual(len(data['messages']), 60)
        self.assertEqual(data['messages'][-1]['text'], 'turn 399')


if __name__ == '__main__':
    unittest.main()


class CodexThreadTests(unittest.TestCase):
    """A fresh Codex session must not borrow the history of an older conversation in the same directory."""

    def database(self, rows):
        import sqlite3
        connection = sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INT, source TEXT, created_at_ms INT, updated_at_ms INT)')
        connection.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', rows)
        return connection

    def started(self):
        import os
        ticks = os.sysconf('SC_CLK_TCK')
        uptime = float(open('/proc/uptime').read().split()[0])
        return str(int((uptime - 120) * ticks))  # this process "started" two minutes ago

    def test_old_conversation_in_the_same_directory_is_not_adopted(self):
        import agents, os, time
        now = int(time.time() * 1000)
        db = self.database([('old', '/r/old', '/work', 0, 'cli', now - 86_400_000, now - 3_600_000)])
        self.assertIsNone(agents.codex_thread(db, os.getpid(), self.started(), '/work'))

    def test_conversation_begun_after_the_process_started_is_adopted(self):
        import agents, os, time
        now = int(time.time() * 1000)
        db = self.database([('old', '/r/old', '/work', 0, 'cli', now - 86_400_000, now - 3_600_000),
                            ('new', '/r/new', '/work', 0, 'cli', now - 30_000, now - 5_000)])
        self.assertEqual(agents.codex_thread(db, os.getpid(), self.started(), '/work'), ('new', '/r/new'))

    def test_resume_argument_names_the_conversation(self):
        import agents, subprocess, sys
        uuid = '01a13a1a-9be4-7033-b28e-828425bc75e0'
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)', 'resume', uuid])
        try:
            db = self.database([(uuid, '/r/resumed', '/elsewhere', 0, 'cli', 1, 2)])
            self.assertEqual(agents.codex_thread(db, child.pid, self.started(), '/work'), (uuid, '/r/resumed'))
        finally:
            child.kill()


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        import shortcuts, tempfile
        self.shortcuts = shortcuts
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch.object(shortcuts, 'FILE', Path(self.directory.name) / 'shortcuts.json')
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_saved_shortcuts_round_trip_trimmed(self):
        self.shortcuts.save([{'name': ' cnp ', 'text': ' commit and push\n'}])
        self.assertEqual(self.shortcuts.load(), [{'name': 'cnp', 'text': 'commit and push'}])

    def test_missing_or_damaged_file_gives_an_empty_list(self):
        self.assertEqual(self.shortcuts.load(), [])
        self.shortcuts.FILE.write_text('not json')
        self.assertEqual(self.shortcuts.load(), [])

    def test_bad_input_is_rejected_with_a_message(self):
        for items in ([{'name': 'two words', 'text': 'x'}], [{'name': 'ok', 'text': ''}], [{'name': 'a', 'text': 'x'}, {'name': 'A', 'text': 'y'}],
                      [{'name': 'x' * 25, 'text': 'x'}], 'nope', [{'name': 'ok', 'text': 'x' * 4001}], [{'name': 'a', 'text': 'x'}] * 51):
            with self.assertRaises(ValueError, msg=str(items)[:40]):
                self.shortcuts.save(items)
        self.assertEqual(self.shortcuts.load(), [])


class TerminalReplyTests(unittest.TestCase):
    BEFORE = '  >_ OpenAI Codex\n     ~/work\n\n› Ask Codex to do anything\n\n  GPT medium · Context 100% left · ~/work'
    AFTER = '  >_ OpenAI Codex\n     ~/work\n\n• Working directory changed to: /home/user/work\n\n• Current working directory: /home/user/work\n\n› Ask Codex to do anything\n\n  GPT medium · Context 100% left · ~/work'

    def setUp(self):
        import terminal_replies
        self.replies = terminal_replies
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch.object(terminal_replies, 'CACHE', Path(self.directory.name))
        self.patch.start()
        self.entry = {'owner_pid': 42, 'process_start': '7', 'agent': 'codex'}

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_only_what_the_terminal_added_is_kept_without_prompt_or_footer(self):
        self.assertEqual(self.replies.added_lines(self.BEFORE, self.AFTER),
                         ['Working directory changed to: /home/user/work', 'Current working directory: /home/user/work'])

    def test_nothing_new_means_no_notice(self):
        self.assertIsNone(self.replies.record(self.entry, self.BEFORE, lambda entry: self.BEFORE, sleep=lambda seconds: None))
        self.assertEqual(self.replies.saved(self.entry), [])

    def test_reply_is_saved_and_merged_by_time_even_when_text_repeats(self):
        for _ in range(2):
            self.replies.record(self.entry, self.BEFORE, lambda entry: self.AFTER, sleep=lambda seconds: None)
        merged = self.replies.merge(self.entry, [{'id': '1', 'role': 'user', 'text': 'hi', 'time': '2000-01-01T00:00:00Z'}])
        self.assertEqual([message['id'] for message in merged][0], '1')
        self.assertEqual(sum(message.get('kind') == 'notice' for message in merged), 2)
        self.assertIn('Working directory changed', merged[-1]['text'])


class CodexMetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.rates = patch.object(server, 'RATES_FILE', Path(self.directory.name) / 'rates.json')
        self.rates.start()

    def tearDown(self):
        self.rates.stop()
        self.directory.cleanup()

    def test_fresh_session_reads_context_from_the_footer_and_reuses_saved_allowances(self):
        future = time.time() + 3600
        windows = [{'label': '5h', 'percent': 12.0, 'resets_at': future, 'window_minutes': 300}]
        # A session that has reported saves the account allowances...
        server.codex_metrics({'id': '%1'}, ({'percent': 40.0}, windows, '2026-10-16T00:00:00Z'))
        # ...and a brand-new one (no token event yet) shows them, plus its context from the terminal footer.
        with patch.object(server, 'capture', return_value='GPT · Context 97% left · ~/x'):
            report = server.codex_metrics({'id': '%2'}, None)
        self.assertEqual(report['context']['percent'], 3.0)
        self.assertEqual(report['five_hour']['percent'], 12.0)
        self.assertTrue(report['allowance_inherited'])

    def test_a_reporting_session_is_not_marked_inherited(self):
        windows = [{'label': '5h', 'percent': 5.0, 'resets_at': time.time() + 3600, 'window_minutes': 300}]
        report = server.codex_metrics({'id': '%1'}, ({'percent': 10.0}, windows, '2026-10-16T00:00:00Z'))
        self.assertFalse(report.get('allowance_inherited'))


class FooterDirectoryTests(unittest.TestCase):
    def run_with(self, screen):
        import agents
        result = subprocess.CompletedProcess([], 0, stdout=screen)
        return agents.footer_cwd(lambda *args, **kwargs: result, '%1')

    def test_home_relative_and_absolute_footer_paths(self):
        self.assertEqual(self.run_with('x\n  GPT medium · Context 100% left · ~/project_a · ← for agents'), str(__import__('agents').HOME / 'project_a'))
        self.assertEqual(self.run_with('  GPT · Context 90% left · /srv/app · ← for agents'), '/srv/app')
        self.assertEqual(self.run_with('  GPT · Context 90% left · ~ · ← for agents'), str(__import__('agents').HOME))

    def test_no_footer_means_none(self):
        self.assertIsNone(self.run_with('just some output\n› prompt'))

    def test_transcript_cwd_for_claude_and_antigravity(self):
        with tempfile.TemporaryDirectory() as folder:
            claude_file = Path(folder) / 'claude.jsonl'
            claude_file.write_text('{"cwd":"/home/user"}\n{"cwd":"/home/user/project_b"}\n')
            self.assertEqual(server.claude_cwd({'agent': 'claude', 'transcript': str(claude_file)}), '/home/user/project_b')
            agy_file = Path(folder) / 'agy.jsonl'
            agy_file.write_text('{"tool_calls":[{"args":{"Cwd":"\\"/home/user\\""}}]}\n'
                                '{"tool_calls":[{"args":{"Cwd":"\\"/home/user/project_b\\""}}]}\n')
            self.assertEqual(server.claude_cwd({'agent': 'antigravity', 'transcript': str(agy_file)}), '/home/user/project_b')
            self.assertIsNone(server.claude_cwd({'agent': 'antigravity', 'transcript': str(Path(folder) / 'missing.jsonl')}))


class AntigravityPromptTests(unittest.TestCase):
    """Antigravity draws a mode hint in its empty input box; that must not count as an unsent draft."""
    ENTRY = {'id': '%1', 'agent': 'antigravity'}
    HINT = 'Auto mode: edits & safe commands auto-approved (shift+tab to cycle)'
    RULE = '─' * 20

    def draft(self, plain_line, styled_line):
        plain = 'summary\n' + self.RULE + '\n' + plain_line + '\n' + self.RULE + '\n? for shortcuts\n'
        styled = 'summary\n' + self.RULE + '\n' + styled_line + '\n' + self.RULE + '\n? for shortcuts\n'
        run = lambda argv, **kwargs: __import__('types').SimpleNamespace(stdout=styled if '-e' in argv else plain, returncode=0)
        return terminal.prompt_draft(self.ENTRY, run)

    def test_mode_hint_in_hint_colour_is_not_a_draft_whatever_the_mode(self):
        for hint in (self.HINT, 'Accept-edits mode: file edits auto-approved (shift+tab to cycle)', 'Plan mode: read-only (shift+tab to cycle)'):
            self.assertEqual(self.draft('> ' + hint, '\x1b[38;5;111m>\x1b[39m \x1b[38;5;109m' + hint + '\x1b[39m'), [], hint)

    def test_typed_text_is_still_a_draft(self):
        self.assertEqual(self.draft('> hello there', '\x1b[38;5;111m>\x1b[39m hello there'), ['hello there'])

    def test_typed_text_that_looks_like_the_hint_is_still_a_draft_when_not_in_hint_colour(self):
        self.assertEqual(self.draft('> ' + self.HINT, '\x1b[38;5;111m>\x1b[39m ' + self.HINT), [self.HINT])


class PickerEnterTests(unittest.TestCase):
    """A slash command that opens a picker must get exactly one Enter; a second would choose an item for the user."""
    entry = {'id': '%1', 'agent': 'claude'}

    def setUp(self):
        for target in (patch.object(terminal, 'verify'), patch.object(terminal.time, 'sleep')):
            target.start()
            self.addCleanup(target.stop)

    def run_pane(self, after_enter):
        state = {'enters': 0, 'screen': 'history\n' + '─' * 8 + '\n❯\xa0\n' + '─' * 8}
        def run(argv, **kwargs):
            out = ''
            if argv[:2] == ['/usr/bin/tmux', 'capture-pane']:
                out = state['screen']
            elif 'paste-buffer' in argv:
                state['screen'] = 'history\n' + '─' * 8 + '\n❯\xa0/model\n' + '─' * 8
            elif argv[-1] == 'Enter':
                state['enters'] += 1
                state['screen'] = after_enter
            return __import__('types').SimpleNamespace(stdout=out, returncode=0)
        return run, state

    def test_picker_rows_are_not_mistaken_for_an_unsent_draft(self):
        picker = 'Select model\n❯\xa01. Default (recommended)\n  2. Opus\n  3. Haiku\nEnter to confirm · Esc to exit'
        run, state = self.run_pane(picker)
        terminal.submit(self.entry, '/model', run=run)
        self.assertEqual(state['enters'], 1)

    def test_a_picker_with_no_marker_at_all_also_ends_the_wait(self):
        run, state = self.run_pane('Select model\n  1. Default\n  2. Opus')
        terminal.submit(self.entry, '/model', run=run)
        self.assertEqual(state['enters'], 1)


class ModelPickerTests(unittest.TestCase):
    """The parsers read screens captured from the three real agents (test_fixtures/pickers)."""
    FIXTURES = Path(__file__).parent / 'test_fixtures' / 'pickers'

    def screen(self, name):
        return (self.FIXTURES / name).read_text()

    def test_claude_numbered_rows_with_current_and_selected(self):
        import model_picker
        parsed = model_picker.parse_claude(self.screen('claude_models.txt'))
        self.assertEqual([row['id'] for row in parsed['rows']], [str(n) for n in range(1, 11)])
        current = [row['label'] for row in parsed['rows'] if row['current']]
        self.assertEqual(current, ['Sonnet 5.5'])
        self.assertEqual(parsed['effort'], 'medium')

    def test_codex_models_then_reasoning_levels(self):
        import model_picker
        models = model_picker.parse({'agent': 'codex'}, self.screen('codex_models.txt'))
        self.assertEqual(models['kind'], 'models')
        self.assertEqual([row['label'] for row in models['rows']][:3], ['GPT-6.1-Sol', 'GPT-6-Astra', 'GPT-6-Sol'])
        self.assertEqual([row['id'] for row in models['rows'] if row['selected']], ['3'])
        effort = model_picker.parse({'agent': 'codex'}, self.screen('codex_effort.txt'))
        self.assertEqual(effort['kind'], 'effort')
        self.assertEqual([row['label'] for row in effort['rows']], ['Low', 'Medium', 'High', 'Extra high'])  # no "More reasoning…"
        self.assertEqual([row['label'] for row in effort['rows'] if row['current']], ['Medium'])

    def test_antigravity_list_and_slider_level(self):
        import model_picker
        parsed = model_picker.parse({'agent': 'antigravity'}, self.screen('agy_models.txt'))
        self.assertEqual([row['label'] for row in parsed['rows']],
                         ['Gemini 4 Argon', 'Gemini 3.8 Flash', 'Claude Opus 5.5', 'Claude Sonnet 5.5', 'GPT-OSS 120B (Medium)'])
        self.assertEqual([row['label'] for row in parsed['rows'] if row['current']], ['Gemini 4 Argon'])
        self.assertEqual(parsed['effort'], 'medium')

    def test_a_newer_frame_in_the_scrollback_wins_over_a_stale_one(self):
        import model_picker
        old = '  Select model\n    1.  A\n  ❯ 2.  B\n'
        new = '  Select model\n  ❯ 1.  A\n    2.  B\n'
        rows = model_picker.numbered_rows((old + new).splitlines())
        self.assertEqual([(row['id'], row['selected']) for row in rows], [('1', True), ('2', False)])

    def test_open_picker_refuses_while_the_agent_is_busy(self):
        import model_picker
        entry = {'id': '%1', 'agent': 'codex'}
        with patch.object(terminal, 'verify'), patch.object(terminal, 'live_status', return_value={'state': 'Working'}):
            with self.assertRaises(model_picker.PickerError):
                model_picker.open_picker(entry, run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=''), sleep=lambda s: None)

    def test_claude_cursor_wraps_and_takes_the_short_way(self):
        import model_picker
        pressed = []
        cursor = model_picker.Cursor({'id': '%1'}, None, lambda s: None, {}, current=3, total=13)
        cursor.look = lambda: None
        with patch.object(model_picker, 'press', side_effect=lambda entry, *keys, run=None: pressed.append(keys[0])):
            cursor.go(1)      # 3 -> 1 is two Ups
            self.assertEqual((cursor.at, pressed), (1, ['Up', 'Up']))
            pressed.clear()
            cursor.go(13)     # 1 -> 13 wraps with a single Up
            self.assertEqual((cursor.at, pressed), (13, ['Up']))
            pressed.clear()
            cursor.go(2)      # 13 -> 2 wraps with two Downs
            self.assertEqual((cursor.at, pressed), (2, ['Down', 'Down']))


def test_folder_note_is_given_once_per_cd(tmp_path, monkeypatch):
    import shell_log
    monkeypatch.setattr(shell_log, 'CACHE', tmp_path)
    shell_log._save('%1', {'cwd': str(tmp_path), 'moved_at': 5.0})
    assert str(tmp_path) in shell_log.folder_note('%1', '/home/user')
    assert shell_log.folder_note('%1', '/home/user') == ''
    shell_log._save('%2', {'cwd': str(tmp_path), 'moved_at': 5.0})
    assert shell_log.folder_note('%2', str(tmp_path)) == ''

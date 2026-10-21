"""Regression checks for native quota notices and fresh-session allowance reports."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import agents
import antigravity
import quota_notices
import server


class AgentNoticeTests(unittest.TestCase):
    def test_quota_notice_is_preserved_once_in_conversation_order(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(antigravity, 'CACHE', Path(folder)):
            entry = {'id': '%3', 'thread': 'conversation', 'transcript': 'unused', 'writable': True}
            screen = ('⚠ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in\n'
                      '2h47m39s.\nError ID: request-273\n\n> /model\nModel changed\n')
            antigravity.capture_notices(entry, screen)
            antigravity.capture_notices(entry, screen)
            antigravity.capture_notices(entry, screen.replace('request-273', 'request-\n273'))
            antigravity.capture_notices(entry, '> continue\nGenerating…\n')
            notices = antigravity.saved_notices(entry['thread'])
            self.assertEqual(len(notices), 1)
            self.assertIn('2h47m39s.', notices[0]['text'])
            self.assertNotIn('/model', notices[0]['text'])
            cache = Path(folder) / 'notices/conversation.json'
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            records = [(0, {'type': 'USER_INPUT', 'step_index': 270, 'content': 'Original request'}),
                       (1, {'type': 'USER_INPUT', 'step_index': 274, 'content': 'Continue'})]
            snapshot = antigravity.snapshot(entry, lambda text: text, lambda _: records, agents.usage)
            self.assertEqual([message['id'] for message in snapshot['messages']], ['270', 'notice:request-273', '274'])
            self.assertEqual(antigravity.saved_notices('other-conversation'), [])

    def test_quota_countdown_updates_the_same_notice(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(antigravity, 'CACHE', Path(folder)):
            entry = {'thread': 'conversation'}
            antigravity.capture_notices(entry, '⚠ Individual quota reached. Resets in 2h.\nError ID: request-1')
            first = antigravity.saved_notices('conversation')[0]
            antigravity.capture_notices(entry, '⚠ Individual quota reached. Resets in 1h.\nError ID: request-1')
            current = antigravity.saved_notices('conversation')
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]['time'], first['time'])
            self.assertIn('1h.', current[0]['text'])

    def test_unexpired_allowances_can_fall_back_but_context_cannot(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(agents, 'METRICS', Path(folder)):
            folder = Path(folder)
            prior = {'context_window': {'used_percentage': 80}, 'reported_at': time.time() - 60,
                     'rate_limits': {'five_hour': {'used_percentage': 26, 'resets_at': time.time() + 600},
                                     'seven_day': {'used_percentage': 98, 'resets_at': time.time() + 3600}}}
            (folder / 'previous.json').write_text(json.dumps(prior))
            (folder / 'new.json').write_text(json.dumps({'context_window': {'used_percentage': None}}))
            entry = {'thread': 'new', 'transcript': str(folder / 'empty.jsonl')}
            report = agents.claude_snapshot(entry, lambda text: text)['usage']
            self.assertEqual(report['five_hour']['percent'], 26)
            self.assertEqual(report['weekly']['percent'], 98)
            self.assertIsNone(report['context']['percent'])
            self.assertTrue(report['allowance_inherited'])
            self.assertEqual(report['allowance_reported_at'], prior['reported_at'])
            (folder / 'new.json').write_text(json.dumps({'rate_limits': {
                'five_hour': {'used_percentage': 30, 'resets_at': time.time() + 600}}}))
            report = agents.claude_snapshot(entry, lambda text: text)['usage']
            self.assertEqual(report['five_hour']['percent'], 30)
            self.assertFalse(report['allowance_inherited'])

    def test_expired_allowance_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(agents, 'METRICS', Path(folder)):
            folder = Path(folder)
            (folder / 'expired.json').write_text(json.dumps({'rate_limits': {
                'five_hour': {'used_percentage': 99, 'resets_at': time.time() - 1}}}))
            report = agents.claude_snapshot({'thread': 'new', 'transcript': str(folder / 'empty')}, lambda text: text)['usage']
            self.assertEqual(report['five_hour'], {})
            self.assertFalse(report['allowance_inherited'])

    def test_claude_native_error_and_terminal_warning_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            transcript = folder / 'chat.jsonl'
            text = "You've hit your session limit · resets 12:20pm (America/New_York)"
            transcript.write_text(json.dumps({'type': 'assistant', 'error': 'rate_limit',
                'isApiErrorMessage': True, 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}) + '\n')
            entry = {'agent': 'claude', 'thread': 'conversation', 'transcript': str(transcript)}
            with patch.object(quota_notices, 'CACHE', folder / 'notices'), patch.object(agents, 'METRICS', folder):
                quota_notices.capture(entry, '  ⎿ ' + text + '\n\n❯ continue')
                quota_notices.capture(entry, '  ⎿ ' + text + '\n\n❯ continue')
                result = agents.claude_snapshot(entry, lambda text: text)
                self.assertEqual(len(result['messages']), 1)
                self.assertEqual(result['messages'][0]['kind'], 'notice')
                self.assertEqual(result['messages'][0]['text'], text)

    def test_codex_limit_event_is_displayed_but_other_errors_are_not(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            transcript = folder / 'chat.jsonl'
            text = "You've hit your usage limit. Try again at 5pm."
            events = [{'type': 'event_msg', 'payload': {'type': 'error', 'message': 'Connection reset'}},
                      {'type': 'event_msg', 'payload': {'type': 'error', 'message': text,
                       'codex_error_info': 'UsageLimitExceeded'}}]
            transcript.write_text('\n'.join(json.dumps(event) for event in events))
            entry = {'agent': 'codex', 'thread': 'conversation', 'transcript': str(transcript)}
            with patch.object(quota_notices, 'CACHE', folder / 'notices'):
                quota_notices.capture(entry, '■ ' + text + '\n\n› continue')
                result = server.snapshot(entry)
                self.assertEqual(len(result['messages']), 1)
                self.assertEqual(result['messages'][0]['kind'], 'notice')
                self.assertEqual(result['messages'][0]['text'], text)

    def test_terminal_only_weekly_limit_is_kept_after_it_disappears(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(quota_notices, 'CACHE', Path(folder)):
            entry = {'agent': 'codex', 'thread': 'conversation', 'transcript': str(Path(folder) / 'missing')}
            text = "You've hit your weekly usage limit. Try again next Monday."
            quota_notices.capture(entry, '■ ' + text + '\n\n› Ask Codex to do anything')
            quota_notices.capture(entry, '› Why does it say ' + text)
            messages = server.snapshot(entry)['messages']
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]['text'], text)
            self.assertEqual(quota_notices.saved(dict(entry, thread='other')), [])
            self.assertEqual((Path(folder) / 'codex/conversation.json').stat().st_mode & 0o777, 0o600)

    def test_wrapped_claude_limit_and_user_quoted_text(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(quota_notices, 'CACHE', Path(folder)):
            entry = {'agent': 'claude', 'thread': 'conversation'}
            quota_notices.capture(entry, "❯ You've hit your session limit\n")
            self.assertEqual(quota_notices.saved(entry), [])
            quota_notices.capture(entry, "  ⎿ You've hit your session\n    limit · resets 12:20pm\n\n❯ ")
            self.assertEqual(len(quota_notices.saved(entry)), 1)
            self.assertEqual(quota_notices.identity(quota_notices.saved(entry)[0]['text']),
                             quota_notices.identity("You've hit your session limit · resets 12:20pm"))

    def test_notice_monitor_captures_without_an_open_web_page(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(quota_notices, 'CACHE', Path(folder)):
            entry = {'id': '%1', 'agent': 'claude', 'thread': 'conversation', 'owner_pid': 1, 'writable': True}
            def inspect(item):
                quota_notices.capture(item, "⎿ You're out of extra usage · resets tomorrow\n")
            with patch.object(server, 'sessions', return_value=[entry]), patch.object(server, 'live_status', side_effect=inspect), \
                    patch.object(server.time, 'sleep', side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    server.monitor_notices()
            self.assertEqual(len(quota_notices.saved(entry)), 1)



class StartupBannerTests(unittest.TestCase):
    CODEX = ('\n  >_ OpenAI Codex (v0.1)\n     ~/proj\n\n  Tip: Visit the Codex\n  community forum\n  (https://x.com\n/c/37).\n\n'
             '› Ask Codex to do anything\n\n  GPT medium · Context…\n')
    AGY = ('      ▄▀▀▄        Antigravity CLI 1.4.0\n     ▀▀▀▀       me@example.com (Pro)\n'
           '    ▀▀▀▀       Gemini (Medium)\n ┃ Verbosity now defaults to medium\n' + '─' * 20 + '\n> \n')

    def test_codex_banner(self):
        import terminal_replies
        text = terminal_replies.banner_text('codex', self.CODEX)
        self.assertTrue(text.startswith('OpenAI Codex (v0.1)\n~/proj'))
        self.assertIn('Tip: Visit the Codex community forum (https://x.com/c/37).', text)
        self.assertNotIn('Ask Codex', text)

    def test_agy_banner_hides_the_account_email(self):
        import terminal_replies
        text = terminal_replies.banner_text('antigravity', self.AGY)
        self.assertIn('Antigravity CLI 1.4.0', text)
        self.assertIn('Verbosity now defaults to medium', text)
        self.assertNotIn('@', text)

    def test_no_banner_on_other_screens(self):
        import terminal_replies
        self.assertEqual(terminal_replies.banner_text('codex', 'just some output'), '')
        self.assertEqual(terminal_replies.banner_text('claude', self.CODEX), '')

    def test_claude_banner(self):
        import terminal_replies
        screen = (' ▐▛███▛█   Claude Code v2.1\n▝▜██████▀  Sonnet 5.5 · Claude Pro\n'
                  ' ▝▝   ▝▝   /home/user\n\n▎ Using Sonnet 5.5 · /model\n❯ hello\n')
        self.assertEqual(terminal_replies.banner_text('claude', screen),
                         'Claude Code v2.1\nSonnet 5.5 · Claude Pro\n/home/user\nUsing Sonnet 5.5 · /model')


class DeliveryConfirmationTests(unittest.TestCase):
    def fake(self, screen):
        import types
        return lambda *args, **kwargs: types.SimpleNamespace(stdout=screen)

    def test_antigravity_message_missing_from_screen_is_reported(self):
        import terminal_input
        original = terminal_input.time.sleep
        terminal_input.time.sleep = lambda seconds: None
        try:
            entry = {'id': '%1', 'agent': 'antigravity'}
            terminal_input.confirm_delivery(entry, 'helloworld', self.fake('> hello world\n  Working...\n'))
            with self.assertRaises(RuntimeError):
                terminal_input.confirm_delivery(entry, 'helloworld', self.fake('>\n'))
            terminal_input.confirm_delivery({'id': '%1', 'agent': 'claude'}, 'helloworld', self.fake('>\n'))
        finally:
            terminal_input.time.sleep = original


class ReplyCleanupTests(unittest.TestCase):
    def test_wrapped_lines_are_joined_and_footer_dropped(self):
        import terminal_replies as t
        lines = t.added_lines('old\n› ', 'old\n  Model changed to gpt-6\n  medium\n  - one\n  - two\n  GPT medium · Context 9…\n› ')
        self.assertEqual(t.unwrap(lines), 'Model changed to gpt-6 medium\n- one\n- two')

    def test_menus_are_not_kept(self):
        import terminal_replies as t
        screens = iter(['x', 'Where to run?\n 2. Worktree\n enter select · esc back', 'Where to run?\n 2. Worktree\n enter select · esc back'])
        self.assertIsNone(t.record({'id': '%1'}, 'x', lambda entry: next(screens), sleep=lambda s: None))


class AntigravityNarrowPaneTests(unittest.TestCase):
    def test_truncated_mode_hint_is_not_a_draft(self):
        import terminal_input
        self.assertTrue(terminal_input.AGY_MODE_HINT.search('Auto mode: edits & safe c...'))
        self.assertTrue(terminal_input.AGY_MODE_HINT.search('Auto mode: edits & safe commands auto-approved (shift+tab to cycle)'))
        self.assertFalse(terminal_input.AGY_MODE_HINT.search('please review the mode of the build'))


class TabFolderTests(unittest.TestCase):
    def test_codex_folder_follows_the_latest_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            path.write_text('{"type":"turn_context","payload":{"turn_id":"a","cwd":"/home/user/one","model":"m"}}\n'
                            '{"type":"event_msg","payload":{}}\n'
                            '{"type":"turn_context","payload":{"turn_id":"b","cwd":"/home/user/two","model":"m"}}\n')
            self.assertEqual(server.codex_cwd({'transcript': str(path)}), '/home/user/two')
            self.assertIsNone(server.codex_cwd({'transcript': str(Path(folder) / 'missing.jsonl')}))


class StickyCdTests(unittest.TestCase):
    def tab(self, moved, agent_now, process='7'):
        from unittest.mock import patch
        entry = {'id': '%9', 'agent': 'claude', 'process_start': process}
        with patch.object(server, 'agent_folder', return_value=agent_now), patch.object(server.shell_log, 'moved', return_value=moved):
            return server.tab_folder(entry)

    def test_cd_holds_while_the_agent_stays_put(self):
        self.assertEqual(self.tab(('/a/proj', 1.0, '/home/u', '7'), '/home/u'), '/a/proj')

    def test_agent_moving_elsewhere_wins(self):
        self.assertEqual(self.tab(('/a/proj', 1.0, '/home/u', '7'), '/b/other'), '/b/other')

    def test_cd_from_an_earlier_process_is_ignored(self):
        self.assertEqual(self.tab(('/a/proj', 1.0, '/home/u', '6'), '/home/u'), '/home/u')

    def test_any_shell_command_fixes_the_folder(self):
        import shell_log
        with tempfile.TemporaryDirectory() as folder, patch_cache(shell_log, folder):
            shell_log.run('%5', 'pwd', folder, '/home/u', '7')
            moved = shell_log.moved('%5')
            self.assertEqual((moved[0], moved[2], moved[3]), (str(Path(folder).resolve()), '/home/u', '7'))


def patch_cache(module, folder):
    from unittest.mock import patch
    return patch.object(module, 'CACHE', Path(folder) / 'shell-cache')


class NarrowPickerTests(unittest.TestCase):
    SCREEN = """Switch Model
  Search:
          ────────────────────
> Gemini 4 Argon        (curren
  Gemini 3.8 Flash
  Effort  ◂     ●━━━━━━━━◉─────
               low    medium
Keyboard: ↑/↓ Navigate  ←/→ Eff
"""

    def test_agy_picker_is_recognised_and_current_model_read_when_cut_short(self):
        import model_picker
        self.assertTrue(model_picker.picker_open('antigravity', self.SCREEN))
        rows = model_picker.parse_antigravity(self.SCREEN)['rows']
        self.assertEqual([(row['id'], row['current']) for row in rows], [('Gemini 4 Argon', True), ('Gemini 3.8 Flash', False)])


class AntigravityPromptTests(unittest.TestCase):
    def test_permission_request_is_rejoined(self):
        import terminal_input
        text = 'Command\n' + '─' * 20 + '\n\nRequesting permission\nfor:\n   touch\n/tmp/x.txt\n\nRun this command?'
        self.assertEqual(terminal_input.reflow_prompt(text), 'Requesting permission for: touch /tmp/x.txt\nRun this command?')

    def test_question_card_uses_only_the_paragraph_above_the_options(self):
        import terminal_input
        screen = ('  beta\n\n' + '─' * 20 + '\n> earlier message\n\n? Which color do you prefer?\n\nQuestion\n' + '─' * 20 +
                  '\n\nQuestion 1/1: Which color do\nyou prefer?\n\n> 1. Red\n  2. Green\n  4. Write-in...\n\n  ↑/↓ Navigate · enter Select ·\nesc to cancel')
        found = terminal_input.menu_question(screen.splitlines())
        self.assertEqual(found['question'], 'Which color do you prefer?')
        self.assertEqual([(o['id'], o['detail']) for o in found['options']], [(1, ''), (2, ''), (4, '')])


class EffortParsingTests(unittest.TestCase):
    def test_agy_slider_with_its_right_end_cut_off(self):
        import model_picker
        bar = '  Effort  ◂     {}\n               low    medium\n'
        self.assertEqual(model_picker.slider_level((bar.format('◉────────○─────')).splitlines()), 'low')
        self.assertEqual(model_picker.slider_level((bar.format('●━━━━━━━━◉─────')).splitlines()), 'medium')
        self.assertEqual(model_picker.slider_level((bar.format('●━━━━━━━━●━━━━━')).splitlines()), 'high')

    def test_claude_effort_line_wrapped_on_a_narrow_pane(self):
        import model_picker
        self.assertEqual(model_picker.claude_effort('  ◐ Medium effort (default)\n  ←/→ to adjust'), 'medium')

    def test_models_with_a_level_in_the_name_have_no_effort(self):
        import model_picker
        self.assertTrue(model_picker.LEVEL_IN_NAME.search('GPT-OSS 120B (Medium)'))
        self.assertFalse(model_picker.LEVEL_IN_NAME.search('Claude Sonnet 5.5'))


class AntigravityPanelAndModelTests(unittest.TestCase):
    def test_full_screen_panels_are_recognised(self):
        import terminal_input
        self.assertTrue(terminal_input.AGY_PANEL.search('  (1–12 of 42 lines)\n  ↑/↓ Scroll · pgup/pgdown Page\n  ctrl+home Top · esc Close'))
        self.assertTrue(terminal_input.AGY_PANEL.search(' general   commands   shortcuts'))
        self.assertFalse(terminal_input.AGY_PANEL.search('> hello\\n  Working...'))

    def test_model_comes_from_the_latest_settings_change_in_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'transcript.jsonl'
            path.write_text('{"content":"<USER_SETTINGS_CHANGE>\\nThe user changed setting `Model Selection` from Gemini 4 Argon (Medium) to '
                            'Gemini 3.8 Flash (Low). No need to comment on this change."}\\n')
            self.assertEqual(server.antigravity_model({'transcript': str(path)}, {'reported_at': 0}), 'Gemini 3.8 Flash (Low)')
            self.assertIsNone(server.antigravity_model({'transcript': str(path)}, {'reported_at': time.time() + 60}))


class InsertByTimeTests(unittest.TestCase):
    def test_items_land_before_the_first_later_message_and_bad_stamps_sort_first(self):
        import activity
        messages = [{'id': 'a', 'time': '2026-10-01T10:00:00Z'}, {'id': 'b', 'time': '2026-10-01T12:00:00+00:00'}]
        result = activity.insert_by_time(messages, [{'id': 'x', 'time': '2026-10-01T11:00:00Z'}, {'id': 'y', 'time': 'not a time'},
                                                    {'id': 'z', 'time': '2026-10-01T13:00:00Z'}])
        self.assertEqual([item['id'] for item in result], ['y', 'a', 'x', 'b', 'z'])
        self.assertEqual([m['id'] for m in messages], ['a', 'b'])  # the input is not changed


if __name__ == '__main__':
    unittest.main()

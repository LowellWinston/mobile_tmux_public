"""Read and drive each agent's /model picker so the page can offer plain tap-to-choose lists.

The agents draw very different pickers: Claude a long numbered, scrolling list with an effort slider (left/right);
Codex a numbered model list followed by a numbered reasoning-level list; Antigravity a searchable list with an effort
slider. This module opens the picker, reads it, moves the selection with arrow keys (checking the screen after every key),
and always closes it again. Nothing is chosen unless apply() is asked to confirm.
"""
import re
import subprocess
import time
import paths

TMUX = paths.TMUX
SETTLE = 0.18
MAX_STEPS = 40


class PickerError(RuntimeError):
    """The picker could not be read or driven; the message is safe to show the user."""


# ---- screen text -------------------------------------------------------------------------------------------------

def capture(entry, run=subprocess.run, history=80):
    result = run([TMUX, 'capture-pane', '-p', '-S', '-%d' % history, '-t', entry['id']], capture_output=True, text=True, timeout=5)
    return result.stdout


def visible(entry, run=subprocess.run):
    return run([TMUX, 'capture-pane', '-p', '-t', entry['id']], capture_output=True, text=True, timeout=5).stdout


def press(entry, *keys, run=subprocess.run):
    run([TMUX, 'send-keys', '-t', entry['id'], *keys], check=True, capture_output=True, timeout=5)


LEVEL_IN_NAME = re.compile(r'\((?:minimal|low|medium|high|xhigh)\)\s*$', re.I)
HINTS = {'claude': 'Esc to cancel', 'codex': 'esc back', 'antigravity': ('esc Go Back', 'Keyboard:')}  # a narrow pane cuts the end of the hint line


def picker_open(agent, text):
    """True while the picker's key hint is on the visible screen (headers can scroll out of view)."""
    tail = [line for line in text.splitlines() if line.strip()][-14:]
    hints = HINTS[agent] if isinstance(HINTS[agent], tuple) else (HINTS[agent],)
    return any(hint.lower() in line.lower() for hint in hints for line in tail)


def region(text, header):
    """The screen text from the newest picker header onward (older pickers stay in the scrollback)."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if header.lower() in line.lower()]
    return lines[starts[-1]:] if starts else []


# ---- parsers: one per agent --------------------------------------------------------------------------------------

NUMBERED = re.compile(r'^\s*(?P<mark>[❯›>↓↑]?)\s*(?P<number>\d+)[.)]\s+(?P<body>\S.*)$')


def numbered_rows(lines):
    rows = []
    for line in lines:
        match = NUMBERED.match(line)
        if not match:
            continue
        body = match['body'].rstrip()
        label = re.split(r'\s{2,}', body)[0]
        current = '✔' in label or '(current)' in label
        label = re.sub(r'\s*(✔|\(current\))', '', label).strip()
        rows.append({'id': match['number'], 'label': label, 'current': current, 'selected': match['mark'] in ('❯', '›', '>')})
    # A picker taller than the pane leaves older frames in the scrollback: the newest sighting of each row is the truth.
    latest = {}
    for row in rows:
        latest[row['id']] = row
    return sorted(latest.values(), key=lambda row: int(row['id']))


def parse_claude(text, on_screen=None):
    lines = region(text, 'Select model')
    rows = numbered_rows(lines)
    if not rows:
        return None
    if on_screen is not None:
        # The list is taller than the pane: lines that scrolled into history keep their first-drawn look, so only the rows
        # still on screen can say which one is selected (and what the effort slider reads).
        marked = {row['id'] for row in numbered_rows(on_screen.splitlines()) if row['selected']}
        for row in rows:
            row['selected'] = row['id'] in marked
        lines = on_screen.splitlines()
    effort = claude_effort('\n'.join(lines))
    return {'kind': 'models', 'rows': rows, 'effort': effort}


def parse_codex_models(text):
    rows = numbered_rows(region(text, 'Select Model and Effort'))
    return {'kind': 'models', 'rows': rows, 'effort': None} if rows else None


def parse_codex_effort(text):
    rows = numbered_rows(region(text, 'Select Reasoning Level'))
    for row in rows:
        row['label'] = re.sub(r'\s*\(default\)', '', row['label']).strip()
    rows = [row for row in rows if not row['label'].lower().startswith('more reasoning')]
    return {'kind': 'effort', 'rows': rows} if rows else None


def slider_level(lines):
    """Antigravity's effort slider: the level whose label sits under the ◉ marker."""
    for index, line in enumerate(lines):
        if '◉' in line and index + 1 < len(lines):
            column = line.index('◉')
            labels = [(match.start() + len(match[0]) / 2, match[0]) for match in re.finditer(r'[A-Za-z]+', lines[index + 1])]
            if labels:
                return min(labels, key=lambda item: abs(item[0] - column))[1].lower()
    # A narrow pane cuts the slider's right end: the last label ("high") and its marker are off screen. A bar still filled
    # to the edge with no ◉ in view means the selection is the next level after the last one that is visible.
    for index, line in enumerate(lines):
        if 'Effort' in line and '━' in line and '◉' not in line and index + 1 < len(lines):
            seen = [match[0].lower() for match in re.finditer(r'[A-Za-z]+', lines[index + 1])]
            if seen and seen[-1] in ORDER and rank(seen[-1]) + 1 < len(ORDER):
                return ORDER[rank(seen[-1]) + 1]
    return None


def claude_effort(text):
    """The level on Claude's effort line ("Medium effort (default) ←/→ to adjust"); a narrow pane wraps the hint onto the next line."""
    found = None
    for match in re.finditer(r'(?P<level>[A-Za-z]+) effort(?: \(default\))?\s+←/→', text):
        found = match['level'].lower()
    return found


def parse_antigravity(text):
    lines = region(text, 'Switch Model')
    if not lines:
        return None
    rows, active = [], False
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith('Search:'):
            active = True
            continue
        if stripped.startswith(('Effort', 'Keyboard')):
            break
        if not active or not stripped or set(stripped) <= set('─━ '):
            continue
        selected = stripped.startswith('>')
        body = stripped[1:].strip() if selected else stripped
        current = bool(re.search(r'\(curr', body))  # the marker is cut short on a narrow pane
        label = re.sub(r'\s*\(curr\w*\)?\s*$', '', body).strip()
        rows.append({'id': label, 'label': label, 'current': current, 'selected': selected})
    return {'kind': 'models', 'rows': rows, 'effort': slider_level(lines)} if rows else None


PARSERS = {'antigravity': parse_antigravity}


def header_position(text, header):
    positions = [index for index, line in enumerate(text.splitlines()) if header.lower() in line.lower()]
    return positions[-1] if positions else -1


def parse(entry, text, on_screen=None):
    if entry['agent'] == 'codex':  # two steps; whichever header is newest is the one on screen
        step_two = header_position(text, 'Select Reasoning Level') > header_position(text, 'Select Model and Effort')
        return parse_codex_effort(text) if step_two else parse_codex_models(text)
    if entry['agent'] == 'claude':
        return parse_claude(text, on_screen)
    return PARSERS[entry['agent']](text)


# ---- driving -----------------------------------------------------------------------------------------------------

def settle(sleep=time.sleep):
    sleep(SETTLE)


def snapshot(entry, run, sleep):
    """Wait for the redraw, then read the picker: scrollback for the whole list, the live screen for what is selected."""
    settle(sleep)
    return parse(entry, capture(entry, run), visible(entry, run))


def selected_index(rows):
    return next((index for index, row in enumerate(rows) if row['selected']), None)


def move_to(entry, target_id, run=subprocess.run, sleep=time.sleep, fetch=None):
    """Press Up/Down until the row with this id is selected. fetch() returns the parsed screen."""
    for _ in range(MAX_STEPS):
        screen = fetch()
        if not screen:
            raise PickerError('The picker disappeared. Try again.')
        rows = screen['rows']
        here = selected_index(rows)
        ids = [row['id'] for row in rows]
        if here is None:
            raise PickerError('Could not see which item is selected.')
        if rows[here]['id'] == target_id:
            return screen
        if target_id in ids:
            direction = 'Down' if ids.index(target_id) > here else 'Up'
        elif all(row['id'].isdigit() for row in rows) and target_id.isdigit():
            direction = 'Down' if int(target_id) > int(rows[here]['id']) else 'Up'
        else:
            raise PickerError('That choice is not in the list any more.')
        press(entry, direction, run=run)
    raise PickerError('Could not reach that choice.')


ORDER = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'extra high', 'max']


def rank(level):
    return ORDER.index(level) if level in ORDER else None


def set_effort(entry, wanted, current_level, fetch, run=subprocess.run):
    """Left/Right on a slider until its label matches the wanted level."""
    wanted = wanted.lower()
    for _ in range(len(ORDER) + 2):
        level = current_level()
        if level == wanted:
            return
        if rank(level) is None or rank(wanted) is None:
            raise PickerError('Could not set the "%s" effort level.' % wanted)
        press(entry, 'Right' if rank(wanted) > rank(level) else 'Left', run=run)
        fetch()
    raise PickerError('This model does not offer "%s" effort.' % wanted)


def probe_levels(entry, current_level, fetch, run=subprocess.run):
    """The slider's levels in order, low to high: walk to the left end, then to the right end."""
    previous = current_level()
    if previous is None:
        return []
    for _ in range(8):
        press(entry, 'Left', run=run)
        fetch()
        level = current_level()
        if level == previous or level is None:
            break
        previous = level
    levels = [previous]
    for _ in range(8):
        press(entry, 'Right', run=run)
        fetch()
        level = current_level()
        if level is None or level in levels:
            break
        levels.append(level)
    return levels


def close(entry, run=subprocess.run, sleep=time.sleep):
    """Leave the picker whatever happened: Esc until its key hint is gone (never Enter)."""
    for _ in range(5):
        if not picker_open(entry['agent'], visible(entry, run)):
            return
        press(entry, 'Escape', run=run)
        sleep(0.35)


def open_picker(entry, run=subprocess.run, sleep=time.sleep):
    from terminal_input import empty_prompt, live_status, verify
    verify(entry, run)
    close(entry, run, sleep)  # a picker left open by an earlier attempt would look like an unsent draft
    state = live_status(entry, run)['state']
    if state != 'Idle':
        raise PickerError('The agent is %s. Change the model when it is idle.' % ('working' if state == 'Working' else 'waiting for you'))
    empty_prompt(entry, run)
    press(entry, '-l', '/model', run=run)
    sleep(0.7)
    press(entry, 'Enter', run=run)  # exactly one Enter: a second would choose an item
    for _ in range(12):
        sleep(0.3)
        if picker_open(entry['agent'], visible(entry, run)):
            return
    close(entry, run, sleep)
    raise PickerError('The model picker did not open. Check the terminal view.')


# ---- Claude: a list taller than the pane, with wrap-around ---------------------------------------------------------

MORE = re.compile(r'…\s*\+(\d+)\s+models')


def claude_view(entry, run, sleep):
    """Rows (labels from scrollback), the selected id if its row is on screen, the effort slider and the list length."""
    settle(sleep)
    history, on_screen = capture(entry, run), visible(entry, run)
    lines = region(history, 'Select model')
    rows = {row['id']: row for row in numbered_rows(lines)}
    shown = numbered_rows(on_screen.splitlines())
    for row in shown:
        rows[row['id']] = dict(rows.get(row['id'], {}), **row)
    marked = next((int(row['id']) for row in shown if row['selected']), None)
    effort = claude_effort(on_screen)
    more = [int(match[1]) for line in lines if (match := MORE.search(line))]
    return {'rows': rows, 'marked': marked, 'effort': effort, 'hidden': more[-1] if more else 0}


class Cursor:
    """Where the selection is. The row can scroll off screen, so presses are counted and the visible marker corrects them."""

    def __init__(self, entry, run, sleep, rows, current, total):
        self.entry, self.run, self.sleep = entry, run, sleep
        self.rows, self.at, self.total = rows, current, total

    def look(self):
        view = claude_view(self.entry, self.run, self.sleep)
        for identifier, row in view['rows'].items():
            self.rows.setdefault(identifier, {'id': identifier, 'label': row['label'], 'current': row['current']})
        self.total = max(self.total, max((int(i) for i in self.rows), default=0))
        if view['marked'] is not None:
            self.at = view['marked']
        return view

    def step(self, direction):
        press(self.entry, direction, run=self.run)
        self.at = self.at % self.total + 1 if direction == 'Down' else (self.at - 2) % self.total + 1
        return self.look()

    def go(self, target):
        target = int(target)
        if not 1 <= target <= self.total:
            raise PickerError('That choice is not in the list any more.')
        for _ in range(self.total + 2):
            if self.at == target:
                return
            down = (target - self.at) % self.total
            self.step('Down' if down <= self.total - down else 'Up')
        raise PickerError('Could not reach that choice.')


def claude_start(entry, run, sleep):
    view = claude_view(entry, run, sleep)
    rows = {identifier: {'id': identifier, 'label': row['label'], 'current': row['current']} for identifier, row in view['rows'].items()}
    current = next((int(identifier) for identifier, row in rows.items() if row['current']), view['marked'])
    if current is None:
        raise PickerError('Could not tell which model is current.')
    total = max(int(identifier) for identifier in rows) + view['hidden']
    return Cursor(entry, run, sleep, rows, current, total), view


def claude_options(entry, run, sleep):
    cursor, view = claude_start(entry, run, sleep)
    start = cursor.at
    for _ in range(cursor.total):  # one full lap shows every row, hidden ones included, and ends where it began
        cursor.step('Down')
    cursor.go(start)
    models = sorted(cursor.rows.values(), key=lambda row: int(row['id']))
    level = lambda: cursor.look()['effort']
    efforts = probe_levels(entry, level, lambda: None, run)
    return {'agent': 'claude', 'models': models, 'efforts': efforts, 'current_effort': view['effort']}


def claude_apply(entry, model, effort, confirm, run, sleep):
    cursor, view = claude_start(entry, run, sleep)
    cursor.go(model)
    if effort:
        view = cursor.look()
        if view['effort']:
            set_effort(entry, effort, lambda: cursor.look()['effort'], lambda: None, run)
        else:
            raise PickerError('This model has no effort setting.')
    result = {'moved': True, 'selected': cursor.rows[str(cursor.at)]['label'], 'effort': cursor.look()['effort']}
    if confirm:
        press(entry, 's', run=run)  # this session only, like the page promises
    return result


def options(entry, run=subprocess.run, sleep=time.sleep):
    """Open the picker, read the models and effort levels, close it again without choosing anything."""
    open_picker(entry, run, sleep)
    try:
        if entry['agent'] == 'claude':
            return claude_options(entry, run, sleep)
        def fetch():
            return snapshot(entry, run, sleep)
        screen = fetch()
        if not screen or not screen['rows']:
            raise PickerError('Could not read the model list. Use the terminal view.')
        models = [{'id': row['id'], 'label': row['label'], 'current': row['current']} for row in screen['rows']]
        if entry['agent'] == 'antigravity':
            for model in models:
                model['effort'] = not LEVEL_IN_NAME.search(model['label'])  # "GPT-OSS 120B (Medium)" has no effort slider
        efforts, current_effort = [], None
        if entry['agent'] == 'codex':
            current = next((row for row in screen['rows'] if row['current']), None)
            if current:
                move_to(entry, current['id'], run, sleep, fetch)
                press(entry, 'Enter', run=run)
                second = fetch()
                if second and second['kind'] == 'effort':
                    efforts = [row['label'].lower() for row in second['rows']]
                    current_effort = next((row['label'].lower() for row in second['rows'] if row['current']), None)
        else:
            level = lambda: (fetch() or {}).get('effort')
            current_effort = screen['effort']
            efforts = probe_levels(entry, level, fetch, run)
            if not efforts and entry['agent'] == 'antigravity':  # the current model has no slider: read the levels from one that has
                capable = next((model for model in models if model.get('effort')), None)
                if capable:
                    move_to(entry, capable['id'], run, sleep, fetch)
                    efforts = probe_levels(entry, level, fetch, run)
        return {'agent': entry['agent'], 'models': models, 'efforts': efforts, 'current_effort': current_effort}
    finally:
        close(entry, run, sleep)


def codex_apply(entry, model, effort, confirm, run, sleep):
    def fetch():
        return snapshot(entry, run, sleep)
    move_to(entry, model, run, sleep, fetch)
    press(entry, 'Enter', run=run)
    second = fetch()
    if not second or second['kind'] != 'effort':
        raise PickerError('The reasoning-level step did not appear.')
    landed = second
    if effort:
        wanted = next((row for row in second['rows'] if row['label'].lower() == effort.lower()), None)
        if not wanted:
            raise PickerError('This model does not offer "%s" effort.' % effort)
        landed = move_to(entry, wanted['id'], run, sleep, fetch)
    if confirm:
        press(entry, 's', run=run)  # this session only, like the page promises
    return {'moved': True, 'selected': next((row['label'] for row in landed['rows'] if row['selected']), None)}


def antigravity_apply(entry, model, effort, confirm, run, sleep):
    def fetch():
        return snapshot(entry, run, sleep)
    landed = move_to(entry, model, run, sleep, fetch)
    if effort and landed.get('effort'):
        set_effort(entry, effort, lambda: (fetch() or {}).get('effort'), fetch, run)
    landed = fetch() or landed
    if confirm:
        press(entry, 'Enter', run=run)
    return {'moved': True, 'selected': next((row['label'] for row in landed['rows'] if row['selected']), None), 'effort': landed.get('effort')}


CONFIRMATION = re.compile(r'\b(set model|kept model|model (set|changed)|changed model|model to|effort (set|changed))\b', re.I)


APPLY = {'claude': claude_apply, 'codex': codex_apply, 'antigravity': antigravity_apply}


def apply(entry, model, effort=None, confirm=True, run=subprocess.run, sleep=time.sleep):
    """Choose a model (and effort) in the picker. With confirm=False the selection is moved and read, then cancelled."""
    open_picker(entry, run, sleep)
    try:
        result = APPLY[entry['agent']](entry, model, effort, confirm, run, sleep)
        if not confirm:
            return result
        sleep(1.0)
        close(entry, run, sleep)
        tail = [line.strip() for line in visible(entry, run).splitlines() if line.strip()][-12:]
        message = next((line.lstrip('⎿•● ').strip() for line in reversed(tail) if CONFIRMATION.search(line)), 'Done.')
        return {'applied': True, 'message': message}
    finally:
        close(entry, run, sleep)

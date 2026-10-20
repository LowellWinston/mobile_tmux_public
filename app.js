// When the session has expired the server answers 401: go back to the sign-in page.
const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await nativeFetch(...args);
  if (response.status === 401 && String(args[0]).startsWith('/mobile/api/')) location.href = '/mobile/login';
  return response;
};
const chat = document.querySelector('#chat');
const status = document.querySelector('#status');
const tabs = document.querySelector('#tabs');
const latest = document.querySelector('#latest');
const input = document.querySelector('#message');
let shortcutsReady = false;  // the shortcut chips follow the box's text, wherever the text is set from
const send = document.querySelector('#send');
const notice = document.querySelector('#notice');
const rendered = new Map();
const views = new Map();
const agentNames = {codex: 'Codex', claude: 'Claude', antigravity: 'Antigravity', shell: 'Shell'};
const agentIcons = {
  codex: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 3l-4 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  claude: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v20M2 12h20M5 5l14 14M5 19 19 5M8 3l8 18M3 8l18 8M3 16l18-8M8 21 16 3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  antigravity: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 20 9-17 9 17M6 15h12" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  shell: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 6 6 6-6 6m9 0h7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
};
let selected = null, thread = null, agent = 'codex', writable = false;
let generation = 0, initial = true, sending = false, noticeTimer, usageData = {};
let tabSignature = '', loadingTabs = false, lastDraft = '';
let preferredSession = null;
const shellModes = new Set();
try { for (const id of JSON.parse(localStorage.getItem('shell-modes') || '[]')) shellModes.add(id); } catch {}
let pendingApproval = null, approvalChoice = null, refreshing = false;
const pendingMessages = new Map();
const MAX_PENDING = 10, PENDING_LIFETIME = 30 * 60000, SHRINK_AFTER = 20000;
const newestId = () => { let best = -1; for (const id of rendered.keys()) best = Math.max(best, parseFloat(id) || -1); return best; };
const launches = new Map();
let startupTimer;
function scheduleStartupPoll() { clearTimeout(startupTimer); startupTimer = setTimeout(refresh, 250); }
function chatTabs() { return [...tabs.querySelectorAll('[role="tab"]')]; }
const working = document.createElement('div');
working.id = 'working'; working.setAttribute('role', 'status'); working.hidden = true;
const workingLabel = document.createElement('span'); workingLabel.textContent = 'Working…';
const stopButton = document.createElement('button'); stopButton.type = 'button'; stopButton.textContent = 'Stop';
working.append(workingLabel, stopButton);
stopButton.addEventListener('click', async () => {
  stopButton.disabled = true;
  try {
    const response = await fetch('/mobile/api/key', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({session: selected, thread, key: 'Escape'})});
    if (!response.ok) throw new Error((await response.json()).error || 'Could not stop.');
    showNotice('Stop sent', true); setTimeout(refresh, 500);
  } catch (error) { showNotice(error.message); }
  finally { stopButton.disabled = false; }
});
const suggestion = document.querySelector('#suggestion');
suggestion.firstElementChild.addEventListener('click', () => {
  input.value = suggestion.firstElementChild.dataset.text; saveDraft(); resizeInput(); suggestion.hidden = true; input.focus({preventScroll: true});
});
function setState(session, state) {
  const button = chatTabs().find(button => button.dataset.session === session);
  if (button) {
    button.querySelector('.tab-state').textContent = state || 'Unknown';
    button.dataset.state = state;
    button.setAttribute('aria-label', button.dataset.label + ' · ' + state);
  }
  updateSwitcher();
}
// The bar shows only the current session; the list of all sessions opens from it, with a count of the others that need you.
const switcher = document.querySelector('#switcher');
function setPanel(open) { tabs.hidden = !open; switcher.setAttribute('aria-expanded', String(open)); }
function updateSwitcher() {
  const buttons = chatTabs(), current = buttons.find(button => button.dataset.session === selected) || null;
  switcher.querySelector('#switcher-label').textContent = current ? current.dataset.label : 'Sessions';
  const folder = switcher.querySelector('#switcher-dir'); folder.textContent = current?.dataset.dir ? '\u200E' + current.dataset.dir : ''; folder.title = current?.dataset.dir || '';
  const icon = switcher.querySelector('.agent-icon'), source = current?.querySelector('.agent-icon');
  icon.className = source ? source.className : 'agent-icon'; icon.innerHTML = source ? source.innerHTML : '';
  if (current) switcher.dataset.state = current.dataset.state; else delete switcher.dataset.state;
  const waiting = buttons.filter(button => button !== current && ['Approval', 'Input needed'].includes(button.dataset.state)).length;
  const badge = switcher.querySelector('#switcher-badge');
  badge.hidden = !waiting; badge.textContent = waiting;
  renderAlerts();
  switcher.setAttribute('aria-label', 'Switch session' + (current ? ' (now ' + current.dataset.label + ', ' + (current.dataset.state || 'Unknown') + ')' : '') + (waiting ? ', ' + waiting + ' other waiting for you' : ''));
}
// Other sessions that need you (or just finished) get a line under the bar, so you never have to open the list to find out.
const alertsBox = document.querySelector('#alerts'), previousStates = new Map(), finished = new Map();
const NEEDS = {Approval: 'needs your approval', 'Input needed': 'needs your input'};
function renderAlerts() {
  const now = Date.now(), rows = [];
  for (const [id, until] of finished) if (until <= now || id === selected) finished.delete(id);
  const buttons = chatTabs();
  for (const button of buttons) {
    if (button.dataset.session === selected) continue;
    if (NEEDS[button.dataset.state]) rows.push({button, text: button.dataset.label + ' ' + NEEDS[button.dataset.state], done: false});
    else if (finished.has(button.dataset.session)) rows.push({button, text: button.dataset.label + ' finished', done: true});
  }
  rows.sort((a, b) => a.done - b.done);
  alertsBox.replaceChildren(...rows.slice(0, 3).map(row => {
    const line = document.createElement('button'); line.type = 'button'; line.className = row.done ? 'done' : '';
    const dot = document.createElement('i'); dot.className = 'alert-dot';
    const text = document.createElement('span'); text.className = 'alert-text'; text.textContent = row.text;
    const go = document.createElement('span'); go.className = 'alert-go'; go.textContent = 'Open ›';
    line.append(dot, text, go);
    line.addEventListener('click', () => { finished.delete(row.button.dataset.session); row.button.click(); renderAlerts(); });
    return line;
  }));
  if (rows.length > 3) {
    const more = document.createElement('button'); more.type = 'button';
    more.textContent = '+' + (rows.length - 3) + ' more'; more.addEventListener('click', () => setPanel(true));
    alertsBox.append(more);
  }
}
switcher.addEventListener('click', () => setPanel(tabs.hidden));
tabs.addEventListener('click', event => { if (event.target.closest('[role="tab"]')) setPanel(false); });
document.addEventListener('click', event => { if (!tabs.hidden && !event.target.closest('#tabs, #switcher')) setPanel(false); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !tabs.hidden) { setPanel(false); switcher.focus(); } });
function updateApproval(value) {
  const changed = pendingApproval?.id !== value?.id;
  pendingApproval = value;
  const panel = document.querySelector('#approval');
  panel.hidden = !value?.actionable;
  document.querySelector('#approval-text').textContent = value?.text || '';
  const options = document.querySelector('#approval-options');
  if (changed || (value && !options.children.length)) {
    approvalChoice = null;
    options.replaceChildren();
    const legend = document.createElement('legend'); legend.textContent = 'Choose an option'; options.append(legend);
    for (const option of value?.options || []) {
      const label = document.createElement('label');
      const radio = document.createElement('input'); radio.type = 'radio'; radio.name = 'approval-choice'; radio.value = option.id;
      const text = document.createElement('span'); text.textContent = option.id + '. ' + option.text;
      radio.addEventListener('change', () => { approvalChoice = option.id; updateApproval(pendingApproval); });
      label.append(radio, text); options.append(label);
    }
  }
  for (const radio of options.querySelectorAll('input')) radio.disabled = !value?.actionable || sending;
  document.querySelector('#approval-submit').disabled = !value?.actionable || approvalChoice === null || sending;
}
document.querySelector('#attention-card .raw-toggle').addEventListener('click', event => {
  const raw = document.querySelector('#attention').classList.toggle('raw');
  event.target.textContent = raw ? 'Hide terminal' : 'Show terminal';
});
function updateAttention(value) {
  const panel = document.querySelector('#attention'), text = document.querySelector('#attention-text');
  const wasHidden = panel.hidden;
  panel.hidden = !value;
  if (!value) { document.querySelector('#attention-error').textContent = ''; return; }
  const atBottom = text.scrollHeight - text.scrollTop - text.clientHeight < 40;
  text.textContent = value.text;
  if (wasHidden || atBottom) text.scrollTop = text.scrollHeight;
  // A recognised multiple-choice question reads as a chat card with tappable options; the raw screen stays one tap away.
  const card = document.querySelector('#attention-card'), choices = value.options?.length > 1;
  panel.classList.toggle('as-card', choices);
  card.hidden = !choices;
  // Show only the keys that mean something for this screen: a card's options replace the arrows and numbers.
  const numbers = new Set([...value.text.matchAll(/^\s*[>❯›]?\s*(\d)[.)]\s/gm)].map(match => match[1]));
  const yesNo = /\(y\/n\)|\[y\/n\]|\(yes\/no\)|\by\/n\b/i.test(value.text);
  for (const button of document.querySelectorAll('#attention-keys button')) {
    const key = button.dataset.key;
    button.hidden = choices ? key !== 'Escape'
      : /^\d$/.test(key) ? !numbers.has(key) : (key === 'y' || key === 'n') ? !yesNo : false;
  }
  if (!choices) return;
  card.querySelector('.q').textContent = value.question || 'Choose an option';
  const list = card.querySelector('.opts');
  const signature = JSON.stringify(value.options.map(option => [option.id, option.text, option.detail]));
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    list.replaceChildren(...value.options.map(option => {
      const button = document.createElement('button'); button.type = 'button';
      const number = document.createElement('b'); number.textContent = option.id;
      const label = document.createElement('span'); label.textContent = option.text;
      if (option.detail) { const detail = document.createElement('small'); detail.textContent = option.detail; label.append(detail); }
      button.append(number, label);
      button.addEventListener('click', () => sendTerminalInput('/mobile/api/key', {key: String(option.id)}));
      return button;
    }));
  }
}
async function sendTerminalInput(path, payload) {
  const error = document.querySelector('#attention-error');
  error.textContent = '';
  try {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify(Object.assign({session: selected, thread}, payload))});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not send input.');
    setTimeout(refresh, 400);
  } catch (failure) { error.textContent = failure.message; }
}
for (const button of document.querySelectorAll('#attention-keys button'))
  button.addEventListener('click', () => sendTerminalInput('/mobile/api/key', {key: button.dataset.key}));
document.querySelector('#attention-type').addEventListener('submit', event => {
  event.preventDefault();
  const field = document.querySelector('#attention-input');
  if (!field.value) return;
  sendTerminalInput('/mobile/api/type', {text: field.value, enter: true}); field.value = '';
});
const sameText = (a, b) => a.replace(/\s+/g, ' ').trim() === b.replace(/\s+/g, ' ').trim();
function storageKey() { return 'chat-draft:' + selected + ':' + thread; }
function saveDraft() { try { if (input.value) localStorage.setItem(storageKey(), input.value); else localStorage.removeItem(storageKey()); } catch {} }
function resizeInput() {
  if (shortcutsReady) showShortcutChips();
  input.style.height = 'auto';
  input.style.height = Math.min(108, Math.max(44, input.scrollHeight + 2)) + 'px';
}
const clearDraftButton = document.querySelector('#clear-draft');
function showNotice(text, transient = false) {
  clearTimeout(noticeTimer); notice.textContent = text; clearDraftButton.hidden = true;
  if (transient) noticeTimer = setTimeout(() => { notice.textContent = ''; }, 3000);
}
const nearBottom = () => document.documentElement.scrollHeight - (scrollY + innerHeight) < 220;
function bottom() { scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'}); latest.hidden = true; }
function format(container, text, foldCode = false) {
  const block = document.createElement('div');
  block.className = 'markdown';
  if (window.marked && window.DOMPurify) {
    const html = marked.parse(text, {gfm: true, breaks: false, async: false});
    block.innerHTML = DOMPurify.sanitize(html, {USE_PROFILES: {html: true},
      FORBID_TAGS: ['img', 'style', 'form', 'iframe', 'object', 'embed', 'video', 'audio']});
    // Prose must stay visible even when the original Markdown contains details.
    for (const details of [...block.querySelectorAll('details')].reverse()) {
      const summary = details.querySelector(':scope > summary');
      if (summary) {
        const label = document.createElement('p'); label.append(...summary.childNodes);
        summary.replaceWith(label);
      }
      details.replaceWith(...details.childNodes);
    }
    if (foldCode) {
      for (const pre of block.querySelectorAll('pre')) {
        const details = document.createElement('details'); details.className = 'code-block';
        const summary = document.createElement('summary'); summary.textContent = 'Show code';
        pre.replaceWith(details); details.append(summary, pre);
      }
    }
    for (const link of block.querySelectorAll('a[href]')) {
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    }
    for (const table of block.querySelectorAll('table')) {
      const wrapper = document.createElement('div');
      wrapper.className = 'table-scroll'; wrapper.tabIndex = 0;
      wrapper.setAttribute('role', 'region'); wrapper.setAttribute('aria-label', 'Scrollable table');
      table.replaceWith(wrapper); wrapper.append(table);
    }
  } else { block.classList.add('plain-text'); block.textContent = text; }
  container.append(block);
}
function ring(button, value, label) {
  const percent = value?.percent;
  const available = typeof percent === 'number' && Number.isFinite(percent);
  button.style.setProperty('--used', available ? Math.max(0, Math.min(100, percent)) + '%' : '0%');
  button.classList.toggle('unavailable', !available);
  button.setAttribute('aria-label', label + (available ? ': ' + Math.round(percent) + '% used' : ': unavailable'));
  button.title = button.getAttribute('aria-label');
}
function updateUsage(data) {
  usageData = data || {};
  ring(document.querySelector('#context-ring'), usageData.context, 'Context window');
  const previous = usageData.allowance_inherited ? ' (last known report)' : '';
  ring(document.querySelector('#five-hour-ring'), usageData.five_hour, '5-hour allowance' + previous);
  ring(document.querySelector('#weekly-ring'), usageData.weekly, 'Weekly allowance' + previous);
  drawCombo();
  if (!document.querySelector('#usage-dialog').hidden) usageDetails();
}
// The three meters every agent reports, in one shape: 5-hour allowance, weekly allowance, context window.
function meters() {
  const now = Date.now() / 1000, windows = usageData.windows || [];
  const allowance = (field, periods, labels) => {
    const found = usageData[field];
    if (typeof found?.percent === 'number') return {percent: found.percent, resets: found.resets_at};
    // A window whose reset time has passed is back to zero until the agent reports again.
    const old = windows.find(item => item.active !== false && item.resets_at && item.resets_at <= now && (periods.includes(item.period) || labels.includes(item.label)));
    return old ? {percent: 0, resets: null, renewed: true} : null;
  };
  const context = usageData.context || {};
  return [
    {key: '5h', name: '5-hour', ...allowance('five_hour', ['five_hour'], ['5h', 'five hour'])},
    {key: 'wk', name: 'Weekly', ...allowance('weekly', ['weekly'], ['168h', 'seven day', 'weekly'])},
    {key: 'C', name: 'Context', percent: typeof context.percent === 'number' ? context.percent : undefined,
     detail: context.tokens != null && context.limit ? compact(context.tokens) + ' / ' + compact(context.limit) : ''},
  ].map(item => ({...item, percent: typeof item.percent === 'number' && Number.isFinite(item.percent) ? Math.max(0, Math.min(100, item.percent)) : null}));
}
const compact = number => number >= 1e6 ? +(number / 1e6).toFixed(1) + 'M' : number >= 1e3 ? Math.round(number / 1e3) + 'k' : String(number);
const level = percent => percent >= 85 ? '#f87171' : percent >= 60 ? '#f6c86f' : '#6ee7a0';
function until(seconds) {
  const gap = seconds - Date.now() / 1000;
  if (!(gap > 0)) return '';
  const minutes = Math.round(gap / 60), hours = Math.floor(minutes / 60), days = Math.floor(hours / 24);
  return days >= 2 ? days + 'd' : days === 1 ? '1d ' + (hours - 24) + 'h' : hours ? hours + 'h ' + (minutes % 60) + 'm' : minutes + 'm';
}
function drawCombo() {
  const rows = [], parts = [];
  for (const item of meters()) {
    const row = document.createElement('span'); row.className = 'um-row' + (item.percent == null ? ' um-off' : '');
    const label = document.createElement('span'); label.className = 'um-name'; label.textContent = item.key;
    const bar = document.createElement('span'); bar.className = 'um-bar';
    const fill = document.createElement('i'); fill.style.width = (item.percent || 0) + '%'; fill.style.background = level(item.percent);
    bar.append(fill); row.append(label, bar); rows.push(row);
    parts.push(item.key + ' ' + (item.percent == null ? 'n/a' : Math.round(item.percent) + '%'));
  }
  const combo = document.querySelector('#usage-combo');
  combo.replaceChildren(...rows); combo.title = parts.join(' · ');
  combo.setAttribute('aria-label', 'Usage: ' + parts.join(', '));
}
function usageDetails() {
  const details = document.querySelector('#usage-details'); details.replaceChildren();
  const row = (name, percent, text, extra) => {
    const line = document.createElement('p'); line.className = 'legend-row';
    const title = document.createElement('b'); title.textContent = name;
    const bar = document.createElement('span'); bar.className = 'legend-bar';
    const fill = document.createElement('i'); fill.style.width = (percent || 0) + '%'; fill.style.background = level(percent);
    bar.append(fill);
    const label = document.createElement('span'); label.textContent = text;
    line.append(title, bar, label); details.append(line);
  };
  const heading = document.createElement('p'); heading.textContent = (agentNames[agent] || agent) + (usageData.model ? ' · ' + usageData.model : ''); heading.className = 'usage-title'; details.append(heading);
  for (const item of meters()) {
    const percent = item.percent == null ? 'n/a' : Math.round(item.percent) + '%';
    const extra = item.detail || (item.renewed ? 'renewed' : item.resets ? 'resets in ' + until(item.resets) : '');
    row(item.name, item.percent, percent + (extra ? ' · ' + extra : ''));
  }
  // Antigravity keeps a separate allowance per model family; the one in use is the row above.
  const others = (usageData.windows || []).filter(item => item.active === false && item.period && typeof item.percent === 'number');
  if (others.length) {
    const groups = new Map();
    for (const item of others) {
      const group = item.label.split('-')[0];
      groups.set(group, (groups.get(group) || []).concat((item.period === 'weekly' ? 'wk ' : '5h ') + Math.round(item.percent) + '%'));
    }
    const note = document.createElement('p'); note.className = 'usage-note';
    note.textContent = 'Other models: ' + [...groups].map(([group, values]) => group + ' ' + values.join(' · ')).join('  |  ') + (groups.has('3p') ? '  (3p = third-party models, e.g. Claude)' : '');
    details.append(note);
  }
  const stamp = usageData.allowance_reported_at || usageData.reported_at;
  const when = typeof stamp === 'number' ? stamp : Date.parse(stamp) / 1000;
  if (when) {
    const note = document.createElement('p'); note.className = 'usage-note';
    const ago = Math.max(0, Math.round((Date.now() / 1000 - when) / 60));
    note.textContent = 'Updated ' + (ago < 1 ? 'just now' : ago < 90 ? ago + 'm ago' : Math.round(ago / 60) + 'h ago') + (usageData.allowance_inherited ? ' · allowances from last known report' : '');
    details.append(note);
  }
}
function select(entry) {
  if (sending || (entry.id === selected && entry.thread === thread && entry.agent === agent)) return;
  if (selected) {
    saveDraft();
    views.set(selected + ':' + thread, {scroll: scrollY, follow: nearBottom()});
  }
  selected = entry.id; thread = entry.thread; agent = entry.agent; writable = entry.writable; windowLimit = 150; loadingEarlier = null;
  generation++; initial = true; rendered.clear(); chat.replaceChildren(); latest.hidden = true;
  updateUsage({}); showNotice('');
  updateApproval(null);
  document.querySelector('#launch-agent').hidden = entry.agent !== 'shell';
  try { localStorage.setItem('chat-session', selected); input.value = localStorage.getItem(storageKey()) || ''; } catch { input.value = ''; }
  resizeInput(); input.disabled = !writable; send.disabled = !writable; renderAttachments();
  input.placeholder = writable ? 'Message ' + (agentNames[agent] || agent) + '…' : agent === 'shell' ? 'Start an agent above to send a msg' : 'Messaging unavailable for this session';
  for (const button of chatTabs()) button.setAttribute('aria-selected', String(button.dataset.session === selected));
  updateSwitcher();
  refresh();
}
async function refreshTabs() {
  if (loadingTabs || document.hidden) return;
  loadingTabs = true;
  try {
    const response = await fetch('/mobile/api/sessions', {cache: 'no-store'});
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Refresh or sign in again.');
    const data = await response.json();
    const signature = JSON.stringify(data.sessions);
    for (const entry of data.sessions) {
      if (entry.id !== selected && previousStates.get(entry.id) === 'Working' && entry.state === 'Idle') { finished.set(entry.id, Date.now() + 8000); setTimeout(renderAlerts, 8100); }
      previousStates.set(entry.id, entry.state);
    }
    if (signature !== tabSignature) {
      tabSignature = signature; tabs.replaceChildren();
      for (const entry of data.sessions) {
        const wrapper = document.createElement('div'); wrapper.className = 'chat-tab';
        const button = document.createElement('button');
        button.type = 'button'; button.setAttribute('role', 'tab');
        const icon = document.createElement('span'); icon.className = 'agent-icon ' + entry.agent;
        icon.innerHTML = agentIcons[entry.agent] || agentIcons.shell;
        icon.setAttribute('aria-hidden', 'true');
        const dot = document.createElement('i'); dot.className = 'tab-dot'; icon.append(dot);
        const label = document.createElement('span'); label.className = 'tab-label'; label.textContent = entry.label;
        const state = document.createElement('small'); state.className = 'tab-state'; state.textContent = entry.agent === 'shell' && shellModes.has(entry.id) ? 'Shell' : entry.state || 'Unknown';
        label.append(state);
        if (entry.dir) { const folder = document.createElement('small'); folder.className = 'tab-dir'; folder.textContent = entry.dir; label.append(folder); }
        button.dataset.dir = entry.dir || '';
        button.append(icon, label); button.dataset.session = entry.id; button.dataset.tmuxSession = entry.tmux_session || ''; button.dataset.label = entry.label;
        button.dataset.state = entry.state; button.setAttribute('aria-label', entry.label + ' · ' + (entry.state || 'Unknown'));
        button.title = entry.label + ' · ' + (agentNames[entry.agent] || entry.agent);
        button.setAttribute('aria-selected', String(entry.id === selected));
        button.addEventListener('click', () => select(entry));
        const close = document.createElement('button'); close.type = 'button'; close.className = 'tab-close';
        close.textContent = '×'; close.setAttribute('aria-label', 'Close ' + entry.label + ' tab');
        close.title = 'Close tab'; close.addEventListener('click', event => { event.stopPropagation(); closeChatTab(entry); });
        wrapper.append(button, close); tabs.append(wrapper);
      }
      updateSwitcher();
    }
    if (!data.sessions.length) {
      selected = null; thread = null;
      status.textContent = 'No tmux sessions yet';
      const help = document.createElement('p'); help.id = 'loading';
      help.textContent = 'Nothing to show yet. Tap + to create a tmux session, or run "tmux new -s work" on your computer, then start Claude Code, Codex or Antigravity in it. This page picks it up within a few seconds.';
      chat.replaceChildren(help);
      return;
    }
    const preferred = data.sessions.find(entry => entry.id === preferredSession);
    if (preferred) { preferredSession = null; select(preferred); }
    else if (!data.sessions.some(entry => entry.id === selected)) {
      let saved; try { saved = localStorage.getItem('chat-session'); } catch {}
      select(data.sessions.find(entry => entry.id === saved) || data.sessions.find(entry => entry.thread === data.default_thread) || data.sessions[0]);
    } else {
      const entry = data.sessions.find(entry => entry.id === selected);
      if (entry.thread !== thread || entry.agent !== agent) select(entry);
    }
  } catch (error) { status.textContent = error.message; }
  finally { loadingTabs = false; }
}
let stream = null, streamKey = '', lastPush = 0, queuedPush = null, windowLimit = 150, loadingEarlier = null;
const earlierButton = document.createElement('button');
earlierButton.id = 'earlier'; earlierButton.type = 'button';
earlierButton.addEventListener('click', () => {
  windowLimit += 150; loadingEarlier = {offset: document.documentElement.scrollHeight - scrollY, limit: windowLimit};
  closeStream(); refresh();
});
function closeStream() { if (stream) stream.close(); stream = null; streamKey = ''; }
function openStream() {
  if (!selected || document.hidden || !window.EventSource) return;
  const mode = (shellModes.has(selected) ? '&mode=shell' : '') + '&limit=' + windowLimit, key = selected + mode;
  if (stream && streamKey === key) return;
  closeStream();
  const session = selected;
  streamKey = key;
  stream = new EventSource('/mobile/api/stream?session=' + encodeURIComponent(session) + mode);
  stream.onmessage = event => {
    let data; try { data = JSON.parse(event.data); } catch { return; }
    if (session !== selected) return;
    lastPush = Date.now(); refresh(data);
  };
}
async function refresh(pushed) {
  if (!selected || document.hidden) return;
  if (refreshing) { if (pushed) queuedPush = pushed; return; }
  refreshing = true;
  const version = generation, session = selected;
  openStream();
  try {
    let data = pushed;
    if (!data) {
      const response = await fetch('/mobile/api/chat?session=' + encodeURIComponent(session) + (shellModes.has(session) ? '&mode=shell' : '') + '&limit=' + windowLimit, {cache: 'no-store'});
      if (version !== generation) return;
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Reconnect or sign in again.');
      data = await response.json();
    }
    if (version !== generation) return;
    if (thread !== data.thread) {
      saveDraft(); thread = data.thread; windowLimit = 150; loadingEarlier = null; rendered.clear(); chat.replaceChildren(); initial = true;
      try { input.value = localStorage.getItem(storageKey()) || ''; } catch { input.value = ''; }
      resizeInput();
    }
    agent = data.agent; writable = data.writable;
    const launch = launches.get(selected);
    if (launch && (agent === launch.agent || Date.now() > launch.expires)) {
      launches.delete(selected); refreshTabs();
    }
    const starting = launches.get(selected);
    document.querySelector('#launch-agent').hidden = agent !== 'shell';
    document.querySelector('#launch-agent p').textContent = starting ? 'Starting ' + agentNames[starting.agent] + '…' : 'Start an agent in this tmux pane';
    for (const button of document.querySelectorAll('#launch-agent button')) button.disabled = !!starting || sending;
    updateApproval(data.approval);
    updateAttention(data.attention);
    suggestion.firstElementChild.dataset.text = data.suggestion || '';
    suggestion.firstElementChild.textContent = data.suggestion || '';
    suggestion.hidden = !data.suggestion || !!input.value || !writable;
    if (!sending) { input.disabled = !writable || !!pendingApproval; send.disabled = !writable || !!pendingApproval; }
    input.placeholder = writable ? 'Message ' + (agentNames[agent] || agent) + '…' : agent === 'shell' ? 'Start an agent above to send a msg' : data.messaging_note ? 'Waiting for Antigravity bridge…' : 'Messaging unavailable for this session';
    if (agent === 'shell' && shellModes.has(selected)) input.placeholder = 'Shell command…';
    if (starting) input.placeholder = 'Starting ' + agentNames[starting.agent] + '…';
    let changed = false, next = null;
    // The page mirrors the window the server sent, so a tab left open for days cannot grow without bound.
    const live = new Set(data.messages.map(message => message.id));
    if (live.size) for (const [id, item] of rendered) if (!live.has(id)) { item.element.remove(); rendered.delete(id); }
    for (let position = data.messages.length - 1; position >= 0; position--) {
      const message = data.messages[position], existing = rendered.get(message.id);
      if (existing?.text === message.text) { next = existing.element; continue; }
      const article = document.createElement('article');
      article.className = 'message ' + message.role;
      if (message.kind === 'notice') article.classList.add('agent-notice');
      if (message.kind === 'banner') article.classList.add('agent-banner');
      const carried = message.role === 'user' && /^This session is being continued from a previous conversation/.test(message.text);
      if (carried) {
        // The summary Claude Code inserts after a context reset: not something you wrote, so it stays folded.
        article.className = 'message agent-actions';
        const details = document.createElement('details'), summary = document.createElement('summary'), body = document.createElement('div');
        summary.textContent = 'Summary of the earlier conversation'; body.className = 'carried-summary';
        details.append(summary, body); article.append(details); format(body, message.text);
        rendered.set(message.id, {text: message.text, element: article, role: message.role});
        if (existing) existing.element.replaceWith(article); else if (next) next.before(article); else chat.append(article);
        changed = true; next = article;
        continue;
      }
      if (message.role === 'user') {
        const label = document.createElement('div');
        label.className = 'label'; label.textContent = '> ';
        article.append(label);
      }
      if (message.kind === 'actions') {
        // What the agent did (commands run, files edited): one folded line, open it to see them all.
        article.classList.add('agent-actions');
        const lines = message.text.split('\n'), details = document.createElement('details');
        const summary = document.createElement('summary');
        const last = lines.at(-1);
        summary.textContent = (message.count || lines.length) + (message.count === 1 ? ' action' : ' actions') + ' · ' + (last.length > 46 ? last.slice(0, 45) + '…' : last);
        const list = document.createElement('ul');
        for (const line of lines) { const item = document.createElement('li'); item.textContent = line; list.append(item); }
        if ((message.count || lines.length) > lines.length) { const item = document.createElement('li'); item.textContent = '… ' + (message.count - lines.length) + ' more'; list.append(item); }
        details.append(summary, list); article.append(details);
      } else if (message.kind === 'terminal') {
        const output = document.createElement('pre'); output.className = 'terminal-output'; output.textContent = message.text; article.append(output);
      } else format(article, message.text, agent === 'codex' && message.role === 'assistant');
      if (existing) existing.element.replaceWith(article);
      else if (next) next.before(article);
      else chat.append(article);
      changed = true; next = article;
      rendered.set(message.id, {text: message.text, element: article, role: message.role});
    }
    if (changed || data.busy || data.state === 'Working') lastActivity = Date.now();
    if (data.earlier > 0) {
      const reachable = data.limit < (data.max_limit || Infinity);   // the server never sends more than max_limit messages
      earlierButton.disabled = !reachable;
      earlierButton.textContent = reachable ? 'Load earlier messages (' + data.earlier + ' more)'
        : 'Showing the latest ' + data.limit + ' messages. Older ones are not shown here.';
      chat.prepend(earlierButton);
    } else earlierButton.remove();
    document.querySelector('#unsupported')?.remove();
    if (data.unsupported) {
      const paragraph = document.createElement('p'); paragraph.id = 'unsupported'; paragraph.textContent = data.unsupported; chat.append(paragraph);
    }
    document.querySelector('#messaging-note')?.remove();
    if (data.messaging_note) {
      const paragraph = document.createElement('p'); paragraph.id = 'messaging-note'; paragraph.textContent = data.messaging_note; chat.append(paragraph);
    }
    status.textContent = data.busy || data.state === 'Working' ? (agentNames[agent] || agent) + ' is working' : writable ? 'Connected' : 'Messaging unavailable';
    setState(selected, data.state || (data.busy ? 'Working' : 'Idle'));
    const pending = pendingMessages.get(selected + ':' + thread) || [];
    // Confirmed = a user message with the same text that arrived after this one was sent (by position in the history, not by clock).
    const confirmed = item => data.messages.some(message => message.role === 'user' && sameText(message.text, item.text) && !(parseFloat(message.id) <= item.after));
    const matched = new Set(pending.filter(confirmed));
    let last = -1, dropped = 0;
    pending.forEach((item, position) => { if (matched.has(item)) last = position; });
    while (pending.length > MAX_PENDING) { pending.shift().element?.remove(); dropped++; last--; }
    for (let position = pending.length - 1; position >= 0; position--) {
      const item = pending[position];
      const gone = matched.has(item) || position <= last || Date.now() - item.time > PENDING_LIFETIME;   // delivery is in order: earlier ones were lost
      if (gone) { item.element?.remove(); pending.splice(position, 1); if (!matched.has(item)) dropped++; }
    }
    if (dropped) showNotice('A message you sent was not confirmed by the agent.', true);
    for (const item of pending) {
      if (!item.element?.isConnected) {
        const article = document.createElement('article'); article.className = 'message user pending-message';
        const label = document.createElement('div'); label.className = 'label'; label.textContent = '> ';
        const delivery = document.createElement('span'); delivery.className = 'delivery-status';
        delivery.textContent = item.delivery === 'queued' ? ' · queued in ' + (agentNames[agent] || 'the agent') : ' · submitted';
        article.append(label); format(article, item.text); article.append(delivery); chat.append(article); item.element = article;
      }
    }
    workingLabel.textContent = (agentNames[agent] || agent) + ' is working…';
    working.hidden = !!data.approval || !!data.attention || (data.state !== 'Working' && !data.busy);
    const lastUser = [...chat.querySelectorAll('.message.user')].at(-1);
    if (lastUser) lastUser.after(working); else chat.prepend(working);
    updateUsage(data.usage);
    if (loadingEarlier && data.limit === loadingEarlier.limit) {
      if (changed) scrollTo(0, document.documentElement.scrollHeight - loadingEarlier.offset);
      loadingEarlier = null;
    } else if (initial) {
      const saved = views.get(selected + ':' + thread);
      if (saved && !saved.follow) scrollTo(0, saved.scroll); else bottom();
    } else if (changed && !loadingEarlier) bottom();
    initial = false;
  } catch (error) { if (version === generation) { status.textContent = error.message; setState(selected, 'Offline'); } }
  finally {
    refreshing = false;
    if (queuedPush) { const next = queuedPush; queuedPush = null; if (next.session === selected) refresh(next); }
    clearTimeout(startupTimer);
    const launch = launches.get(selected);
    if (launch && Date.now() > launch.expires) {
      launches.delete(selected);
      for (const button of document.querySelectorAll('#launch-agent button')) button.disabled = false;
    } else if (launch && !document.hidden) scheduleStartupPoll();
  }
}
latest.addEventListener('click', bottom);
// Live screen: a read-only view of the tmux pane itself, for anything the chat view does not show.
const screenDialog = document.querySelector('#screen-dialog'), screenText = document.querySelector('#screen-text'), screenMeta = document.querySelector('#screen-meta');
let screenTimer = null, screenLines = 80, screenBusy = false, screenLast = '';
async function refreshScreen() {
  if (screenBusy || !screenDialog.open || document.hidden || !selected) return;
  screenBusy = true;
  try {
    const response = await fetch('/mobile/api/screen?session=' + encodeURIComponent(selected) + '&lines=' + screenLines, {cache: 'no-store'});
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Could not read the screen.');
    const data = await response.json();
    const selection = getSelection();
    const selecting = selection && !selection.isCollapsed && screenText.contains(selection.anchorNode);
    if (data.text !== screenLast && !selecting) {   // never replace text while someone is selecting it to copy
      const follow = screenText.scrollHeight - screenText.scrollTop - screenText.clientHeight < 60;
      screenText.textContent = data.text; screenLast = data.text;
      if (follow) screenText.scrollTop = screenText.scrollHeight;
    }
    screenMeta.textContent = 'updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  } catch (error) { screenMeta.textContent = error.message; }
  finally { screenBusy = false; }
}
function openScreen() {
  if (!selected) return;
  screenLast = ''; screenText.textContent = ''; screenMeta.textContent = 'loading…';
  screenDialog.showModal(); refreshScreen();
  clearInterval(screenTimer); screenTimer = setInterval(refreshScreen, 1000);
  setTimeout(() => { screenText.scrollTop = screenText.scrollHeight; }, 300);
}
document.querySelector('#open-screen').addEventListener('click', openScreen);

// The + button opens a small menu: attach a photo or file, dictate, or open the terminal.
// Dictation: the browser's speech recognition turns speech into text in the message box; nothing is sent until Send.
const toolsButton = document.querySelector('#tools');
const attachMenu = document.querySelector('#attach-menu');
const attachFile = document.querySelector('#attach-file');
let toggleDictation = () => showNotice('This browser cannot dictate here. Tap the microphone on your keyboard instead.');
(() => {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return;
  let recognition = null;
  let base = '';
  toggleDictation = () => {
    if (recognition) { recognition.stop(); return; }
    recognition = new Recognition();
    recognition.lang = navigator.language || 'en-US';
    recognition.interimResults = true;
    recognition.continuous = true;
    base = input.value && !/\s$/.test(input.value) ? input.value + ' ' : input.value;
    recognition.onresult = (event) => {
      let spoken = '';
      for (const result of event.results) spoken += result[0].transcript;
      input.value = base + spoken.trim();
      resizeInput(); saveDraft();
    };
    recognition.onerror = (event) => {
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') showNotice('Allow microphone access for this page to dictate.');
    };
    const setListening = (on) => { toolsButton.classList.toggle('listening', on); toolsButton.textContent = on ? '🎤' : '+'; };   // the + shows a mic while listening; tap it to stop
    recognition.onend = () => { recognition = null; setListening(false); input.focus(); };
    setListening(true);
    try { recognition.start(); } catch (error) { recognition = null; setListening(false); }
  };
})();

// Attachments: a photo or file is uploaded to the server and its path goes into the message, so any agent can read it.
const attachable = () => !!selected && writable && agent !== 'shell' && !input.disabled;
function closeAttachMenu() { attachMenu.hidden = true; toolsButton.setAttribute('aria-expanded', 'false'); }
toolsButton.addEventListener('pointerdown', (event) => event.preventDefault());
toolsButton.addEventListener('click', () => {
  if (toolsButton.classList.contains('listening')) { toggleDictation(); return; }   // a tap while listening just stops
  if (!attachMenu.hidden) { closeAttachMenu(); return; }
  document.querySelector('#menu-attach').disabled = !attachable();
  attachMenu.hidden = false; toolsButton.setAttribute('aria-expanded', 'true');
});
attachMenu.addEventListener('click', closeAttachMenu);   // any item closes the menu (each item's own handler still runs)
document.querySelector('#menu-dictate').addEventListener('click', toggleDictation);
document.querySelector('#menu-attach').addEventListener('click', () => attachFile.click());
document.addEventListener('click', (event) => { if (!attachMenu.hidden && !attachMenu.contains(event.target) && event.target !== toolsButton) closeAttachMenu(); });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !attachMenu.hidden) { closeAttachMenu(); toolsButton.focus(); } });

const attachments = new Map();   // 'session:thread' -> [{name, path}], kept until the message is sent
const chipBar = document.querySelector('#attachments');
const attachmentKey = () => selected + ':' + thread;
function renderAttachments() {
  const files = attachments.get(attachmentKey()) || [];
  chipBar.hidden = !files.length;
  chipBar.replaceChildren(...files.map((file, index) => {
    const chip = document.createElement('span'); chip.className = 'chip';
    const label = document.createElement('span'); label.textContent = '📎 ' + file.name;
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '✕'; remove.setAttribute('aria-label', 'Remove ' + file.name);
    remove.addEventListener('click', () => { files.splice(index, 1); renderAttachments(); input.focus(); });
    chip.append(label, remove); return chip;
  }));
}
const MAX_SIDE = 2400, MAX_PLAIN_BYTES = 4 * 1024 * 1024;
// Phone photos carry location data and are huge: re-draw JPEG/HEIC photos (dropping that metadata) and shrink anything oversized.
async function prepareFile(file) {
  const kind = file.type;
  if (!kind.startsWith('image/') || kind === 'image/gif' || kind === 'image/svg+xml') return {blob: file, name: file.name || 'file'};
  const isPng = kind === 'image/png';
  try {
    const bitmap = await createImageBitmap(file, {imageOrientation: 'from-image'});
    const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
    if (isPng && scale === 1 && file.size <= MAX_PLAIN_BYTES) { bitmap.close(); return {blob: file, name: file.name || 'screenshot.png'}; }
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const type = isPng ? 'image/png' : 'image/jpeg';
    const blob = await new Promise(resolve => canvas.toBlob(resolve, type, 0.9));
    if (!blob) throw new Error('encode failed');
    const base = (file.name || 'photo').replace(/\.[^.]*$/, '') || 'photo';
    return {blob, name: base + (isPng ? '.png' : '.jpg')};
  } catch { return {blob: file, name: file.name || 'file'}; }   // an image the browser cannot decode goes up unchanged
}
async function uploadFile(file) {
  const {blob, name} = await prepareFile(file);
  const response = await fetch('/mobile/api/upload', {method: 'POST', body: blob, headers: {
    'X-Codex-Mobile': '1', 'X-Session': selected, 'X-Thread': thread, 'X-Filename': encodeURIComponent(name),
    'Content-Type': blob.type || 'application/octet-stream'}});
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again before attaching.');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Could not attach that file.');
  return data;
}
async function attachFiles(files) {
  files = [...files];
  if (!files.length) return;
  if (!attachable()) { showNotice('Attachments need a running agent in this tab.'); return; }
  const session = selected, conversation = thread;
  const key = session + ':' + conversation;
  const added = [];
  try {
    for (const [index, file] of files.entries()) {
      showNotice('Uploading ' + (files.length > 1 ? (index + 1) + '/' + files.length + ' ' : '') + (file.name || 'file') + '…');
      const saved = await uploadFile(file);
      added.push({name: file.name || saved.name.replace(/^[0-9a-f]{8}-/, ''), path: saved.path});
    }
  } catch (error) { showNotice(error.message); }
  if (!added.length) return;
  attachments.set(key, [...(attachments.get(key) || []), ...added]);   // belongs to the tab it was picked in, even if you switched meanwhile
  if (session === selected && conversation === thread) {
    renderAttachments(); input.focus();
    showNotice((added.length === 1 ? 'Attached. ' : added.length + ' files attached. ') + 'Add your instruction and send.', true);
  }
}
attachFile.addEventListener('change', () => { const files = [...attachFile.files]; attachFile.value = ''; attachFiles(files); });   // copy first: clearing the input empties its live list
input.addEventListener('paste', (event) => {   // a screenshot pasted from the clipboard (desktop browsers)
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) { event.preventDefault(); attachFiles(files); }
});
document.querySelector('#close-screen').addEventListener('click', () => screenDialog.close());
document.querySelector('#screen-interact').addEventListener('click', () => {
  const tmuxSession = chatTabs().find(tab => tab.dataset.session === selected)?.dataset.tmuxSession;
  screenDialog.close(); openModelPicker(tmuxSession, selected, thread);   // the full interactive terminal
});
screenDialog.addEventListener('close', () => { clearInterval(screenTimer); screenTimer = null; if (!input.disabled) input.focus({preventScroll: true}); });
document.querySelector('#screen-more').addEventListener('click', event => {
  screenLines = screenLines === 80 ? 300 : 80; event.target.textContent = screenLines === 80 ? 'More history' : 'Less history';
  screenLast = ''; refreshScreen();
});
document.querySelector('#screen-copy').addEventListener('click', async event => {
  try { await navigator.clipboard.writeText(screenText.textContent); event.target.textContent = 'Copied'; }
  catch { event.target.textContent = 'Select to copy'; }
  setTimeout(() => { event.target.textContent = 'Copy'; }, 1500);
});
const modelDialog = document.querySelector('#model-dialog');
const modelFrame = document.querySelector('#model-frame');
let modelSession = null, modelThread = null;
function openModelPicker(tmuxSession, session, conversation) {
  if (!tmuxSession) { showNotice('Could not find the terminal for this tab. Refresh and try again.'); return; }
  modelSession = session; modelThread = conversation;
  document.querySelector('#model-error').textContent = '';
  modelFrame.src = '/terminal/live/?arg=' + encodeURIComponent(tmuxSession);
  modelDialog.showModal();
}
document.querySelector('#close-model').addEventListener('click', () => modelDialog.close());
modelDialog.addEventListener('close', () => {
  modelFrame.src = 'about:blank'; modelSession = null; modelThread = null;
  refresh();
  if (!input.disabled) input.focus({preventScroll: true});
});
for (const button of document.querySelectorAll('#model-keys button')) button.addEventListener('click', async () => {
  if (!modelSession) return;
  button.disabled = true;
  try {
    const response = await fetch('/mobile/api/key', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({session: modelSession, thread: modelThread, key: button.dataset.key})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not send key.');
    document.querySelector('#model-error').textContent = '';
  } catch (error) { document.querySelector('#model-error').textContent = error.message; }
  finally { button.disabled = false; }
});

// Model sheet: the server reads the agent's own /model picker (each agent draws it differently) and drives it, so the page
// only shows one plain list of models and one row of effort levels.
const modelSheet = document.querySelector('#model-sheet'), msNote = document.querySelector('#ms-note'), msModels = document.querySelector('#ms-models');
const msEffort = document.querySelector('#ms-effort'), msEfforts = document.querySelector('#ms-efforts'), msApply = document.querySelector('#ms-apply');
let modelChoice = null, effortChoice = null, modelBase = null, modelsWithoutEffort = new Set();
const titleCase = text => text.charAt(0).toUpperCase() + text.slice(1);
async function modelRequest(path, payload) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'}, body: JSON.stringify(payload)});
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again, then retry.');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Could not reach the model picker.');
  return data;
}
function paintModelSheet() {
  for (const button of msModels.children) button.setAttribute('aria-checked', String(button.dataset.id === modelChoice));
  for (const button of msEfforts.children) button.setAttribute('aria-checked', String(button.dataset.id === effortChoice));
  msEffort.hidden = !msEfforts.children.length || modelsWithoutEffort.has(modelChoice);  // some models carry their level in the name
  msApply.disabled = !modelBase || (modelChoice === modelBase.model && effortChoice === modelBase.effort);
}
async function openModelSheet() {
  if (!selected || modelSheet.open) return;
  const session = selected, conversation = thread;
  modelBase = null; modelChoice = effortChoice = null;
  msModels.replaceChildren(); msEfforts.replaceChildren(); msEffort.hidden = true; msApply.disabled = true;
  msNote.textContent = 'Reading the model list… this takes a few seconds.';
  document.querySelector('#ms-scope').textContent = '';
  modelSheet.showModal();
  try {
    const data = await modelRequest('/mobile/api/model-options', {session, thread: conversation});
    if (!modelSheet.open) return;
    const current = data.models.find(model => model.current) || data.models[0];
    modelBase = {model: current?.id, effort: data.current_effort || null};
    modelsWithoutEffort = new Set(data.models.filter(model => model.effort === false).map(model => model.id));
    modelChoice = modelBase.model; effortChoice = modelBase.effort;
    for (const model of data.models) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'ms-option'; button.setAttribute('role', 'radio');
      button.dataset.id = model.id; button.append(document.createTextNode(model.label));
      if (model.current) { const tag = document.createElement('small'); tag.textContent = 'current'; button.append(tag); }
      button.addEventListener('click', () => { modelChoice = model.id; paintModelSheet(); });
      msModels.append(button);
    }
    for (const level of data.efforts || []) {
      const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'ms-chip'; chip.setAttribute('role', 'radio');
      chip.dataset.id = level; chip.textContent = titleCase(level);
      chip.addEventListener('click', () => { effortChoice = level; paintModelSheet(); });
      msEfforts.append(chip);
    }
    document.querySelector('#ms-scope').textContent = data.agent === 'antigravity' ? 'Applies as soon as you choose.' : 'Applies to this session only.';
    msNote.textContent = '';
    paintModelSheet();
    msModels.querySelector('[aria-checked="true"]')?.scrollIntoView({block: 'nearest'});
  } catch (error) { msNote.textContent = error.message; }
}
msApply.addEventListener('click', async () => {
  if (!modelBase) return;
  const session = selected, conversation = thread;
  msApply.disabled = true; msNote.textContent = 'Applying… the agent is being switched.';
  try {
    const data = await modelRequest('/mobile/api/model', {session, thread: conversation, model: modelChoice,
      effort: effortChoice && effortChoice !== modelBase.effort && !modelsWithoutEffort.has(modelChoice) ? effortChoice : null});
    modelSheet.close(); showNotice(data.message || 'Model changed.', true); refresh();
  } catch (error) { msNote.textContent = error.message; paintModelSheet(); }
});
document.querySelector('#ms-close').addEventListener('click', () => modelSheet.close());
modelSheet.addEventListener('click', event => {  // a tap on the dimmed area closes it
  const box = modelSheet.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) modelSheet.close();
});
modelSheet.addEventListener('close', () => { if (!input.disabled) input.focus({preventScroll: true}); });
document.querySelector('#compose').addEventListener('submit', async event => {
  event.preventDefault();
  const typed = input.value.trim();
  const alias = savedShortcuts.find(item => item.name.toLowerCase() === typed.toLowerCase());  // a lone shortcut name sends its full text
  const files = agent === 'shell' ? [] : attachments.get(attachmentKey()) || [];
  const message = [...files.map(file => file.path), alias ? alias.text : typed].filter(Boolean).join(' ');   // attached files go first, as plain paths
  if (!message || sending) return;
  if (!files.length && agent !== 'shell' && message.startsWith('!')) { runShell(message.slice(1).trim()); return; }  // run here, in the tab's folder, for every agent
  if (!writable || pendingApproval) return;
  const session = selected, conversation = thread, after = newestId();
  if (!files.length && agent !== 'shell' && message === '/model') {  // pick from a list instead of typing into the agent's own picker
    input.value = ''; saveDraft(); resizeInput(); openModelSheet(); return;
  }
  sending = true; send.disabled = true; input.disabled = true;
  for (const tab of tabs.querySelectorAll('button')) tab.disabled = true;
  showNotice('Sending…');
  try {
    const response = await fetch('/mobile/api/message', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({message, session, thread: conversation, mode: agent === 'shell' ? 'shell' : 'chat'})});
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again before sending. Your draft is saved.');
    const data = await response.json();
    if (!response.ok) { const failure = new Error(data.error || 'Message was not accepted.'); failure.draft = !!data.draft; throw failure; }
    const key = session + ':' + conversation;
    const pending = pendingMessages.get(key) || [];
    if (agent !== 'shell') pending.push({text: message, time: Date.now(), after, delivery: ['claude', 'antigravity'].includes(agent) ? data.delivery : 'submitted'});
    pendingMessages.set(key, pending);
    input.value = ''; saveDraft(); resizeInput();
    attachments.delete(session + ':' + conversation); renderAttachments();
    showNotice(['claude', 'antigravity'].includes(agent) && data.delivery === 'queued' ? 'Queued in ' + (agentNames[agent] || agent) + '; delivered when its current work allows' : 'Submitted to terminal; waiting for agent history', true);
    await refresh(); bottom();
  } catch (error) { showNotice(error.message); clearDraftButton.hidden = !error.draft; }
  finally {
    sending = false; send.disabled = !writable || !!pendingApproval; input.disabled = !writable || !!pendingApproval;
    if (!input.disabled && !modelDialog.open) input.focus({preventScroll: true});
    for (const tab of tabs.querySelectorAll('button')) tab.disabled = false;
  }
});
input.addEventListener('input', () => { resizeInput(); saveDraft(); if (input.value) suggestion.hidden = true; });
clearDraftButton.addEventListener('click', async () => {
  clearDraftButton.disabled = true;
  try {
    const response = await fetch('/mobile/api/clear-draft', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({session: selected, thread})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not clear the draft.');
    showNotice('Agent draft cleared. Send your message again.', true);
  } catch (error) { showNotice(error.message); clearDraftButton.hidden = false; }
  finally { clearDraftButton.disabled = false; }
});
for (const button of document.querySelectorAll('#launch-agent button')) button.addEventListener('click', async () => {
  if (sending || agent !== 'shell' || launches.has(selected)) return;
  if (button.dataset.agent === 'shell') {
    button.disabled = true;
    try {
      const response = await fetch('/mobile/api/message', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
        body: JSON.stringify({message: 'cd ~', session: selected, thread, mode: 'shell'})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not start the shell in your home directory.');
      shellModes.add(selected);
      try { localStorage.setItem('shell-modes', JSON.stringify([...shellModes])); } catch {}
      await refresh(); showNotice('No Agent shell is ready in ~', true);
    } catch (error) { showNotice(error.message); }
    finally { button.disabled = false; }
    return;
  }
  const session = selected;
  launches.set(session, {agent: button.dataset.agent, expires: Date.now() + 20000});
  sending = true;
  input.disabled = true; send.disabled = true;
  for (const control of document.querySelectorAll('#launch-agent button')) control.disabled = true;
  for (const tab of tabs.querySelectorAll('button')) tab.disabled = true;
  showNotice('Starting ' + agentNames[button.dataset.agent] + '…');
  try {
    const response = await fetch('/mobile/api/start', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({session: selected, thread, agent: button.dataset.agent})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not start the agent.');
    showNotice('Launch submitted; waiting for the agent to start', true);
    await refreshTabs(); await refresh();
  } catch (error) { launches.delete(session); showNotice(error.message); }
  finally {
    sending = false;
    input.disabled = send.disabled = !writable || !!pendingApproval;
    for (const control of document.querySelectorAll('#launch-agent button')) control.disabled = launches.has(session);
    for (const tab of tabs.querySelectorAll('button')) tab.disabled = false;
    if (launches.has(session)) scheduleStartupPoll();
  }
});
async function loadTmuxSessionChoices() {
  const list = document.querySelector('#tmux-session-list');
  list.replaceChildren();
  const loading = document.createElement('p'); loading.textContent = 'Loading sessions…'; list.append(loading);
  try {
    const response = await fetch('/mobile/api/tmux-sessions', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load active sessions.');
    list.replaceChildren();
    const visible = new Set(chatTabs().map(button => button.dataset.tmuxSession).filter(Boolean));
    const unique = new Map(data.sessions.map(session => [session.name, session]));
    const choices = [...unique.values()].filter(session => !visible.has(session.id));
    if (!unique.size) {
      const empty = document.createElement('p'); empty.textContent = 'No active tmux sessions.'; list.append(empty);
    } else if (!choices.length) {
      const empty = document.createElement('p'); empty.textContent = 'All active tmux sessions are already on the top tabs.'; list.append(empty);
    }
    for (const session of choices) {
      const button = document.createElement('button'); button.type = 'button'; button.setAttribute('role', 'listitem');
      const name = document.createElement('span'); name.textContent = session.name;
      const windows = document.createElement('small'); windows.textContent = session.windows + (session.windows === 1 ? ' window' : ' windows');
      button.append(name, windows);
      button.addEventListener('click', () => {
        const chatTab = chatTabs().find(tab => tab.dataset.tmuxSession === session.id);
        if (chatTab) chatTab.click();
        else window.location.href = '/mobile/terminal/?session=' + encodeURIComponent(session.id);
      });
      list.append(button);
    }
  } catch (error) {
    list.replaceChildren();
    const message = document.createElement('p'); message.textContent = error.message; list.append(message);
  }
}
document.querySelector('#new-session').addEventListener('click', () => {
  if (sending) return;
  document.querySelector('#session-error').textContent = '';
  document.querySelector('#new-session-dialog').showModal();
  loadTmuxSessionChoices(); document.querySelector('#session-name').focus();
});
document.querySelector('#cancel-session').addEventListener('click', () => document.querySelector('#new-session-dialog').close());
async function closeChatTab(entry) {
  if (sending || !entry.tmux_session) return;
  if (!window.confirm('Close “' + entry.label + '” and stop its tmux pane? Any work running there will stop.')) return;
  for (const control of tabs.querySelectorAll('button')) control.disabled = true;
  try {
    const response = await fetch('/mobile/api/session', {method: 'DELETE', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({pane: entry.id})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not close the tab.');
    if (selected === entry.id) { selected = null; thread = null; }
    await refreshTabs(); showNotice('Closed ' + entry.label, true);
  } catch (error) { showNotice(error.message); }
  finally { for (const control of tabs.querySelectorAll('button')) control.disabled = false; }
}
document.querySelector('#new-session-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.querySelector('#create-session');
  if (button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch('/mobile/api/session', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({name: document.querySelector('#session-name').value.trim(), session: selected})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not create the session.');
    preferredSession = data.session;
    document.querySelector('#new-session-dialog').close(); document.querySelector('#session-name').value = '';
    await refreshTabs();
  } catch (error) { document.querySelector('#session-error').textContent = error.message; }
  finally { button.disabled = false; }
});
input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey &&
      !event.isComposing && event.keyCode !== 229 && matchMedia('(min-width: 768px) and (pointer: fine)').matches) {
    event.preventDefault(); document.querySelector('#compose').requestSubmit();
  }
});
document.querySelector('#approval-submit').addEventListener('click', async () => {
  if (!pendingApproval || approvalChoice === null || sending) return;
  sending = true;
  updateApproval(pendingApproval);
  for (const tab of tabs.querySelectorAll('button')) tab.disabled = true;
  try {
    const response = await fetch('/mobile/api/approval', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({session: selected, thread, approval: pendingApproval.id, choice: approvalChoice})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Approval response failed.');
    showNotice('Approval response submitted', true); await refresh();
  } catch (error) { showNotice(error.message); }
  finally {
    sending = false; for (const tab of tabs.querySelectorAll('button')) tab.disabled = false;
    updateApproval(pendingApproval);
    input.disabled = send.disabled = !writable || !!pendingApproval;
  }
});
tabs.addEventListener('keydown', event => {
  if (sending || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  const buttons = chatTabs(), index = buttons.indexOf(document.activeElement);
  if (index < 0) return;
  event.preventDefault();
  const next = buttons[(index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length];
  next.focus(); next.click();
});
for (const button of document.querySelectorAll('.usage-ring')) button.addEventListener('click', () => {
  const dialog = document.querySelector('#usage-dialog'); dialog.hidden = !dialog.hidden;
  if (!dialog.hidden) { usageDetails(); document.querySelector('#close-usage').focus(); }
});
document.querySelector('#close-usage').addEventListener('click', () => { document.querySelector('#usage-dialog').hidden = true; document.querySelector('#context-ring').focus(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') document.querySelector('#usage-dialog').hidden = true; });
document.addEventListener('click', event => {  // a tap anywhere outside the details (and not on a ring, which toggles them) closes them
  const dialog = document.querySelector('#usage-dialog');
  if (!dialog.hidden && !event.target.closest('#usage-dialog, .usage-ring')) dialog.hidden = true;
});
// Notifications: Web Push through a service worker, so alerts arrive even when this page is closed.
const notifyButton = document.querySelector('#notify-toggle');
const pushHeaders = {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'};
const postPush = (path, payload) => fetch(path, {method: 'POST', headers: pushHeaders, body: JSON.stringify(payload)});
function setNotifyState(state) {
  notifyButton.hidden = false; notifyButton.dataset.state = state;
  notifyButton.title = {on: 'Notifications on (tap to turn off)', off: 'Turn on notifications', unsupported: 'Notifications unavailable here'}[state];
}
function keyBytes(text) {
  const raw = atob((text + '='.repeat((4 - text.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, character => character.charCodeAt(0));
}
async function initNotifications() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) { setNotifyState('unsupported'); return; }
  try {
    const registration = await navigator.serviceWorker.register('/mobile/sw.js', {scope: '/mobile/'});
    navigator.serviceWorker.addEventListener('message', event => {
      if (event.data?.session) { preferredSession = event.data.session; refreshTabs(); }
    });
    const existing = await registration.pushManager.getSubscription();
    setNotifyState(existing && Notification.permission === 'granted' ? 'on' : 'off');
    notifyButton.addEventListener('click', async () => {
      if (notifyButton.dataset.state === 'unsupported') return;
      notifyButton.disabled = true;
      try {
        const current = await registration.pushManager.getSubscription();
        if (current && notifyButton.dataset.state === 'on') {
          await postPush('/mobile/api/push/unsubscribe', {endpoint: current.endpoint}); await current.unsubscribe();
          setNotifyState('off'); showNotice('Notifications turned off', true); return;
        }
        if (await Notification.requestPermission() !== 'granted') { showNotice('Notifications are blocked for this site in your browser settings.'); return; }
        const config = await (await fetch('/mobile/api/push/config', {cache: 'no-store'})).json();
        if (!config.supported) throw new Error('Push is not installed on the server.');
        const subscription = current || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(config.publicKey)});
        const saved = await postPush('/mobile/api/push/subscribe', {subscription: subscription.toJSON()});
        if (!saved.ok) throw new Error((await saved.json()).error || 'Could not save the subscription.');
        setNotifyState('on'); await postPush('/mobile/api/push/test', {});
        showNotice('Notifications on. A test alert is on its way.', true);
      } catch (error) { showNotice(error.message); }
      finally { notifyButton.disabled = false; }
    });
  } catch { setNotifyState('unsupported'); }
}
notifyButton.addEventListener('click', () => {
  if (notifyButton.dataset.state === 'unsupported') showNotice('For notifications on iPhone, add this page to your Home Screen and open it from there.');
});
const hashed = location.hash.match(/session=([^&]+)/);
if (hashed) { preferredSession = decodeURIComponent(hashed[1]); history.replaceState(null, '', location.pathname + location.search); }
initNotifications();

// Shortcuts: short names typed in the chat box offer their full text; kept on the server so every device shares them.
let savedShortcuts = [];
const chipBox = document.querySelector('#shortcut-chips'), scDialog = document.querySelector('#shortcuts-dialog');
const scRows = document.querySelector('#sc-rows'), scNote = document.querySelector('#sc-note');
async function loadShortcuts() {
  try {
    const response = await fetch('/mobile/api/shortcuts', {cache: 'no-store'});
    if (response.ok) savedShortcuts = (await response.json()).shortcuts || [];
  } catch {}
  showShortcutChips();
}
function showShortcutChips() {
  const typed = input.value.trim().toLowerCase();
  const matches = typed && !/\s/.test(typed) ? savedShortcuts.filter(item => item.name.toLowerCase().startsWith(typed)).slice(0, 3) : [];
  chipBox.replaceChildren(...matches.map(item => {
    const chip = document.createElement('button'); chip.type = 'button';
    const name = document.createElement('b'); name.textContent = item.name;
    chip.append(name, document.createTextNode(item.text.replace(/\s+/g, ' ')));
    chip.title = item.text;
    chip.addEventListener('click', () => { input.value = item.text; saveDraft(); resizeInput(); showShortcutChips(); input.focus(); });
    return chip;
  }));
  chipBox.hidden = !matches.length;
}
input.addEventListener('input', showShortcutChips);
// Tapping a chip must not take focus from the message box: the keyboard would close, the page would move, and the tap would miss.
for (const box of [chipBox, document.querySelector('#suggestion')]) box.addEventListener('pointerdown', event => { if (event.target.closest('button')) event.preventDefault(); });
shortcutsReady = true;
function shortcutRow(item = {name: '', text: ''}) {
  const row = document.createElement('div'); row.className = 'sc-row';
  const name = document.createElement('input'); name.placeholder = 'Name, e.g. cnp'; name.value = item.name; name.maxLength = 24; name.autocapitalize = 'off'; name.autocomplete = 'off'; name.spellcheck = false;
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'sc-del'; remove.textContent = 'Delete';
  remove.addEventListener('click', () => row.remove());
  const text = document.createElement('textarea'); text.placeholder = 'Full text, e.g. commit and push'; text.value = item.text; text.maxLength = 4000;
  row.append(name, remove, text); return row;
}
document.querySelector('#open-shortcuts').addEventListener('click', () => {
  scNote.textContent = ''; scRows.replaceChildren(...savedShortcuts.map(shortcutRow));
  if (!savedShortcuts.length) scRows.append(shortcutRow());
  scDialog.showModal();
});
document.querySelector('#sc-add').addEventListener('click', () => { const row = shortcutRow(); scRows.append(row); row.querySelector('input').focus(); });
for (const id of ['#sc-close', '#sc-close-bottom']) document.querySelector(id).addEventListener('click', () => scDialog.close());
scDialog.addEventListener('click', event => {  // a tap on the dimmed area outside the box closes it
  const box = scDialog.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) scDialog.close();
});
document.querySelector('#shortcuts-form').addEventListener('submit', async event => {
  event.preventDefault();
  const items = [...scRows.querySelectorAll('.sc-row')].map(row => ({name: row.querySelector('input').value.trim(), text: row.querySelector('textarea').value.trim()}))
    .filter(item => item.name || item.text);
  scNote.textContent = 'Saving…';
  try {
    const response = await fetch('/mobile/api/shortcuts', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'}, body: JSON.stringify({shortcuts: items})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save.');
    savedShortcuts = data.shortcuts; showShortcutChips(); scDialog.close();
  } catch (error) { scNote.textContent = error.message; }
});
loadShortcuts();
navigator.serviceWorker?.ready.then(() => navigator.serviceWorker.controller?.postMessage({clear: true})).catch(() => {});
let bottomSince = null;
setInterval(() => {
  // "Load earlier" widens the window; return to the small one once the reader is back at the live end.
  if (windowLimit <= 150 || loadingEarlier || document.hidden || !nearBottom()) { bottomSince = null; return; }
  bottomSince ??= Date.now();
  if (Date.now() - bottomSince >= SHRINK_AFTER) { windowLimit = 150; bottomSince = null; closeStream(); refresh(); }
}, 5000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) navigator.serviceWorker?.controller?.postMessage({clear: true});  // you are looking: clear the badge and alerts
  if (document.hidden) closeStream(); else { refreshTabs(); refresh(); }
});
// The stream pushes changes as they happen; polling only covers a stream that is down or silent.
refreshTabs(); setInterval(refreshTabs, 5000); setInterval(() => { if (Date.now() - lastPush > 12000) refresh(); }, 2000);

// Keep the composer above the on-screen keyboard. Rather than trusting a formula, measure where the composer really is
// against the visible part of the page and lift it by whatever still hangs below; browsers disagree about the keyboard.
if (window.visualViewport) {
  const viewport = window.visualViewport, composer = document.querySelector('footer');
  let lift = 0;
  const fit = () => {
    const visibleBottom = viewport.offsetTop + viewport.height;
    const next = Math.max(0, Math.round(lift + composer.getBoundingClientRect().bottom - visibleBottom));
    if (next === lift) return;
    const wasLive = nearBottom();
    lift = next; document.documentElement.style.setProperty('--keyboard', lift + 'px');
    if (lift && wasLive) bottom();
  };
  const settle = () => { for (const delay of [0, 120, 300, 600, 1000]) setTimeout(fit, delay); };  // the keyboard animates in
  viewport.addEventListener('resize', fit); viewport.addEventListener('scroll', fit);
  input.addEventListener('focus', settle); input.addEventListener('blur', settle);
  window.addEventListener('orientationchange', settle);
}


// The less-used buttons (shortcuts, notifications) sit behind one menu button so the top bar stays uncrowded.
const moreToggle = document.querySelector('#more-toggle'), moreMenu = document.querySelector('#more-menu');
function setMore(open) { moreMenu.hidden = !open; moreToggle.setAttribute('aria-expanded', String(open)); }
moreToggle.addEventListener('click', event => { event.stopPropagation(); setMore(moreMenu.hidden); });
moreMenu.addEventListener('click', () => setMore(false));
document.addEventListener('click', event => { if (!moreMenu.hidden && !moreMenu.contains(event.target)) setMore(false); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') setMore(false); });
drawCombo();

// Renaming changes the name of the tmux session behind the open tab.
document.querySelector('#rename-session').addEventListener('click', async () => {
  const button = chatTabs().find(item => item.dataset.session === selected);
  if (!button?.dataset.tmuxSession) { showNotice('Select a session to rename.'); return; }
  const name = (window.prompt('Rename session', button.dataset.tmuxSession) || '').trim();
  if (!name || name === button.dataset.tmuxSession) return;
  try {
    const response = await fetch('/mobile/api/rename', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({pane: selected, name})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not rename the session.');
    tabSignature = ''; await refreshTabs(); showNotice('Renamed to ' + name, true);
  } catch (error) { showNotice(error.message); }
});

// "!command" runs on the server in this tab's folder (cd sticks); the agent never sees it, so it means the same for every agent.
async function runShell(command) {
  if (!command) { showNotice('Type a command after the !.'); return; }
  const session = selected;
  sending = true; send.disabled = true; showNotice('Running…');
  try {
    const response = await fetch('/mobile/api/shell', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({session, command})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not run the command.');
    input.value = ''; saveDraft(); resizeInput(); showNotice('');
    tabSignature = ''; await refreshTabs();  // the tab's folder follows the command
    await refresh(); bottom();
  } catch (error) { showNotice(error.message); }
  finally { sending = false; send.disabled = !writable || !!pendingApproval; if (!input.disabled) input.focus({preventScroll: true}); }
}

// The open session is not repeated in the list, so closing it lives in the menu.
document.querySelector('#close-session').addEventListener('click', () => {
  const entry = chatTabs().find(item => item.dataset.session === selected);
  if (entry) closeChatTab({id: selected, label: entry.dataset.label, tmux_session: entry.dataset.tmuxSession});
  else showNotice('Select a session to close.');
});

const screen = document.querySelector('#screen');
const status = document.querySelector('#status');
const tabs = document.querySelector('#session-tabs');
const manager = document.querySelector('#manager');
const sessionList = document.querySelector('#session-list');
const error = document.querySelector('#manager-error');
let sessions = [];
let active = null;
let loading = false;
let tabSignature = '';

const requested = new URLSearchParams(location.search).get('session');
if (requested && /^\$\d+$/.test(requested)) active = requested;
else { try { active = localStorage.getItem('mobile-tmux-session'); } catch {} }

async function request(url, options = {}) {
  const response = await fetch(url, {cache: 'no-store', ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}

function select(session) {
  active = session.id;
  try { localStorage.setItem('mobile-tmux-session', active); } catch {}
  renderTabs();
  const frame = document.createElement('iframe');
  frame.title = 'tmux session ' + session.name;
  frame.allow = 'clipboard-read; clipboard-write';
  frame.src = '/terminal/live/?arg=' + encodeURIComponent(session.id);
  screen.replaceChildren(frame);
  status.textContent = 'Connected to ' + session.name + '. Tap the terminal to type.';
  renderSessions();
}

function renderTabs() {
  const signature = JSON.stringify(sessions.map(session => [session.id, session.name]));
  if (signature !== tabSignature) {
    tabSignature = signature;
    tabs.replaceChildren();
    for (const session of sessions) {
      const button = document.createElement('button');
      button.className = 'session-tab'; button.type = 'button'; button.setAttribute('role', 'tab');
      button.textContent = session.name; button.title = session.name; button.dataset.session = session.id;
      button.addEventListener('click', () => select(session));
      tabs.append(button);
    }
  }
  for (const button of tabs.children) button.setAttribute('aria-selected', String(button.dataset.session === active));
}

function renderSessions() {
  sessionList.replaceChildren();
  if (!sessions.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-list'; empty.textContent = 'No active tmux sessions.';
    sessionList.append(empty);
    return;
  }
  for (const session of sessions) {
    const row = document.createElement('div'); row.className = 'session';
    const label = document.createElement('div'); label.className = 'session-name';
    const name = document.createElement('strong'); name.textContent = session.name;
    const meta = document.createElement('div'); meta.className = 'session-meta';
    meta.textContent = session.windows + (session.windows === 1 ? ' window' : ' windows') + (session.id === active ? ' · connected' : '');
    label.append(name, meta);
    const connect = document.createElement('button'); connect.className = 'control'; connect.type = 'button'; connect.textContent = 'Open';
    connect.setAttribute('aria-label', 'Open ' + session.name);
    connect.addEventListener('click', () => { select(session); manager.hidden = true; });
    const close = document.createElement('button'); close.className = 'control danger'; close.type = 'button'; close.textContent = 'Close';
    close.setAttribute('aria-label', 'Close tmux session ' + session.name);
    close.addEventListener('click', () => closeSession(session));
    row.append(label, connect, close); sessionList.append(row);
  }
}

async function refreshSessions() {
  if (loading) return;
  loading = true;
  try {
    const data = await request('/mobile/api/tmux-sessions');
    const byName = new Map();
    for (const session of data.sessions) {
      if (!byName.has(session.name) || session.id === active) byName.set(session.name, session);
    }
    sessions = [...byName.values()];
    renderSessions();
    renderTabs();
    const current = sessions.find(session => session.id === active);
    if (current) {
      if (!screen.querySelector('iframe')) select(current);
    } else if (sessions.length) {
      select(sessions[0]);
    } else {
      active = null; renderTabs();
      screen.replaceChildren();
      const empty = document.createElement('p'); empty.id = 'empty'; empty.textContent = 'No tmux sessions are running. Open Sessions to start one.';
      screen.append(empty); status.textContent = 'No active tmux sessions.';
    }
  } catch (e) { status.textContent = e.message; }
  finally { loading = false; }
}

async function closeSession(session) {
  if (!window.confirm('Close tmux session “' + session.name + '”? Any running work in it will stop.')) return;
  error.textContent = '';
  for (const button of sessionList.querySelectorAll('button')) button.disabled = true;
  try {
    await request('/mobile/api/tmux-sessions', {method: 'DELETE', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({session: session.id})});
    status.textContent = 'Closed ' + session.name + '.';
    await refreshSessions();
  } catch (e) { error.textContent = e.message; }
  finally { renderSessions(); }
}

document.querySelector('#open-sessions').addEventListener('click', () => {
  error.textContent = ''; manager.hidden = false; refreshSessions();
});
document.querySelector('#close-manager').addEventListener('click', () => { manager.hidden = true; });
manager.addEventListener('click', event => { if (event.target === manager) manager.hidden = true; });
document.querySelector('#create-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.querySelector('#create-button');
  if (button.disabled) return;
  button.disabled = true; error.textContent = '';
  try {
    const name = document.querySelector('#new-name').value.trim();
    const data = await request('/mobile/api/tmux-sessions', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'},
      body: JSON.stringify({name})});
    document.querySelector('#new-name').value = '';
    await refreshSessions();
    const created = sessions.find(session => session.id === data.session);
    if (created) select(created);
    manager.hidden = true;
  } catch (e) { error.textContent = e.message; }
  finally { button.disabled = false; }
});

document.addEventListener('keydown', event => { if (event.key === 'Escape' && !manager.hidden) manager.hidden = true; });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshSessions(); });
refreshSessions();
setInterval(refreshSessions, 5000);

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A phone-friendly chat view for coding agents (Claude Code, Codex, Antigravity) running in tmux. The chat is rebuilt from each agent's **own history/transcript files**, not the tmux screen; user input is injected into the pane through tmux (bracketed paste + Enter). See `README.md` for the full behavior spec.

## Commands

- Run tests: `python3 -m unittest test_session_controls test_agent_notices test_auth test_uploads`
- Single test: `python3 -m unittest test_session_controls.SessionControlTests.<test_name>`
- Restart after **any** backend or frontend change: `systemctl --user restart mobile-tmux.service` (or `sudo systemctl restart mobile-tmux.service` on a system-level install) (`server.py` loads `index.html`/`app.js` once at start-up, so UI and API changes only take effect together after a restart).
- Service runs `python3 server.py` on 127.0.0.1:7683 (stdlib `http.server`, no dependencies, no build step). `deploy/install.py` installs user-level systemd units (no root).

## Architecture

- **Auth is in `auth.py`.** Default `token` mode (access token file + session cookie, rate-limited); `MOBILE_TMUX_AUTH=proxy` trusts a reverse proxy and refuses non-loopback binds. Every GET/POST/DELETE goes through `Handler.deny()`; POSTs also need a same-host Origin and the `X-Codex-Mobile` header. `/terminal/live/` is proxied to ttyd (WebSocket included) after sign-in.
- **File locations are in `paths.py`** (env-overridable). Never hardcode `/home/...`, `/usr/bin/tmux` or similar.
- **`server.py`** – the HTTP `Handler` (`do_GET`/`do_POST`/`do_DELETE`), `snapshot()`/`chat_payload()` (what the page polls), and `submit()`. It also runs a background monitor (`monitor_notices`) every 2s that captures terminal-only quota notices even when no page is open.
- **`agents.py`** – discovers tmux panes (every ~5s) and maps each to a live agent conversation, with a per-agent strategy: Codex → newest non-archived CLI/IDE thread in the pane's cwd (sqlite + jsonl; ambiguous if two panes share a directory); Claude → its live process registry and exact tmux pane; Antigravity → the conversation lock held by the live process (`antigravity.py`). Reads transcripts via `records()`.
- **`terminal_input.py` / `terminal_replies.py` / `model_picker.py` / `shortcuts.py`** – safe injection into the pane: verifies process start time, pane ancestry, agent and conversation identity, and an empty recognized input box before sending; parses folder-trust and approval prompts into numbered choices bound to a request fingerprint. Never overwrite an existing user draft.
- **`uploads.py`** – saves phone attachments (flat private folder, random prefix, 7-day prune); `Handler.do_upload` takes the raw body with `X-Session`/`X-Thread`/`X-Filename` headers. The page adds the returned path to the next message; nothing is served back.
- **`history.py`, `activity.py`, `quota_notices.py`, `shell_log.py`** – transcript rendering helpers, Idle/Approval/Working state, deduplicated rate-limit notices (transcript and terminal copies merged), and the "No agent" shell command/output mode.
- **Usage rings** come from native reports only (no estimates): `claude_usage.py` and `antigravity_status.py` are status-line hooks that write to `~/.cache/mobile-tmux/{usage,antigravity}/`; Codex usage comes from saved token counts. `push.py` handles web push.
- **Frontend**: `index.html` + `app.js` (single large file), PWA bits (`sw.js`, `manifest.webmanifest`), vendored `marked`/`DOMPurify` in `vendor/`. `termius.html/js` is a secondary view. `/terminal/` redirects to `/mobile/`.

## Conventions and gotchas

- Per-agent quirks regularly show up as chat display gaps; when fixing one, audit that agent's history record types rather than patching a single symptom, and keep Claude/Codex/Antigravity behavior consistent.
- POSTs must carry the selected pane and conversation; stale conversations are rejected. Keep that binding when adding endpoints.
- Tool output and reasoning are intentionally omitted from the chat; shell-command envelopes render as `! command` plus output.
- Tests rely on `test_fixtures/` and mock tmux/process state via `unittest.mock.patch`; they don't need a live tmux.
- The access token and push keys live in `~/.config/mobile-tmux/` (never commit them).

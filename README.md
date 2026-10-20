# Mobile Tmux

A chat-style phone interface for coding agents running in tmux. Run [Claude Code](https://claude.com/claude-code),
[Codex CLI](https://github.com/openai/codex) or Antigravity (`agy`) in a tmux session on your own machine, and
read and answer them from your phone as a chat, with approvals, usage meters, and model pickers.

- **Reads the agent's own history, not the screen.** The chat is rebuilt from each agent's transcript files, so it is clean text, not a terminal scrape.
  Input goes into the pane through tmux, exactly as if you had typed it.
- **Approvals and questions as buttons.** Folder-trust prompts, permission requests and multiple-choice questions show up with their real options.
- **Usage at a glance.** Rings for the 5-hour allowance, weekly allowance and context use, from each agent's official reports.
- **Model and effort pickers**, `/slash` commands, `!shell` commands in the session's folder, dictation, and push notifications when an agent needs you.
- **Several sessions as tabs**, with the working folder, an Idle/Working/Approval state, and a start button for each agent.
- **No dependencies, no build step.** A single Python standard-library server and one web page.

## Requirements

- Linux (it relies on `/proc` and the agents' file layouts on Linux). macOS is not supported yet.
- Python 3 (developed and tested on 3.14; older versions are untested) and `tmux`.
- At least one agent installed and run **inside tmux**: Claude Code, Codex CLI, or Antigravity.
- Optional: [`ttyd`](https://github.com/tsl0922/ttyd) for the full interactive terminal ("Interact"); `pywebpush` for push notifications (`pip install pywebpush`).

## Quick start

```bash
git clone https://github.com/LowellWinston/mobile_tmux.git
cd mobile_tmux
python3 deploy/install.py --hooks     # systemd user service, plus the usage-ring hooks
```

The installer needs no root. It prints the address and your **access token**. Then, on the same machine:

1. Start an agent in tmux: `tmux new -s work`, then `claude` (or `codex`, or `agy`).
2. Open `http://127.0.0.1:7683/mobile/` and sign in with the token.

To try it without installing a service: `python3 server.py`.

### Reaching it from your phone

By default the server listens on `127.0.0.1` only. Anyone who can reach it can type into your agents and run commands as you, so **do not
expose it to the internet directly**. Good ways to reach it:

- **Tailscale (simplest).** Install Tailscale on the machine and the phone, then `tailscale serve --bg --https=443 http://127.0.0.1:7683`.
  Open `https://<machine>.<tailnet>.ts.net/mobile/`. You get HTTPS (needed for push notifications and installing it as an app), and only your devices can reach it.
- **Your own HTTPS reverse proxy** with its own sign-in. Set `MOBILE_TMUX_AUTH=proxy` (see below) and use
  [`deploy/examples/nginx-auth-proxy.conf`](deploy/examples/nginx-auth-proxy.conf) as a starting point.
- **Plain LAN over HTTP.** Set `MOBILE_TMUX_HOST=0.0.0.0` and sign in with the token. The token and chat then cross your network unencrypted,
  so only do this on a network you trust. Push notifications and "add to home screen" need HTTPS.

On the phone, open the page and use "Add to Home Screen" for an app-like view.

## Signing in and security

| Mode | Set with | Behavior |
| --- | --- | --- |
| `token` (default) | nothing | One access token, created on first run in `~/.config/mobile-tmux/token` (owner-only). Signing in sets an HttpOnly, SameSite=Strict cookie. Failed attempts are rate-limited. |
| `proxy` | `MOBILE_TMUX_AUTH=proxy` | A reverse proxy in front signs people in; the server trusts it. It refuses to start unless it is bound to loopback, so nothing can reach it around the proxy. |

State-changing requests also need a matching `Origin` and a custom header, so another website cannot drive it from your browser.
To rotate the token, delete `~/.config/mobile-tmux/token` and restart the service.

Be clear about what this tool is: a remote control for programs that can edit files and run commands as your user. Treat the access token like an SSH key.
The `!command` feature runs shell commands as the user the server runs as. Report security problems privately (see [SECURITY.md](SECURITY.md)).

## Usage rings (the `--hooks` step)

Claude Code and Antigravity report usage through a "status line" command. `deploy/install.py --hooks` points that at small recorders
(`claude_usage.py`, `antigravity_status.py`) that save the report for the page. It never replaces a status line you already have; in that case
add the command yourself, for example in `~/.claude/settings.json`:

```json
{"statusLine": {"type": "command", "command": "/usr/bin/python3 /path/to/mobile_tmux/claude_usage.py"}}
```

For Antigravity the same block goes in `~/.gemini/antigravity-cli/settings.json` with `antigravity_status.py` and `"enabled": true`.
Restart the agent after changing this. Codex needs no hook. Without the hooks everything else works; the rings just show "n/a".

## Configuration

All optional, set as environment variables (for the systemd service, add `Environment=NAME=value` to the unit and restart).

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOBILE_TMUX_HOST` | `127.0.0.1` | Address to listen on. |
| `MOBILE_TMUX_PORT` | `7683` | Port to listen on. |
| `MOBILE_TMUX_AUTH` | `token` | `token` or `proxy` (see above). |
| `MOBILE_TMUX_TTYD_PORT` | `7682` | Where the optional ttyd runs. |
| `MOBILE_TMUX_PUSH_SUBJECT` | `mailto:mobile-tmux@localhost` | Contact (a `mailto:` or `https:` URL) sent to push services. Set your own email if you use push. |
| `MOBILE_TMUX_HOME` | your home | Where agent folders (`~/.claude`, `~/.codex`, `~/.gemini`) are looked up. |
| `MOBILE_TMUX_CACHE` | `~/.cache/mobile-tmux` | Working data (usage reports, saved notices). |
| `MOBILE_TMUX_CONFIG` | `~/.config/mobile-tmux` | The access token and push keys. |
| `MOBILE_TMUX_TMUX` | found on `PATH` | Path to the `tmux` binary. |

## How it works, and its limits

Details are in [docs/behavior.md](docs/behavior.md). In short: the server finds your tmux panes every few seconds and maps each one to a live agent
conversation, then serves the chat to the page. Before it types anything it checks that the pane still runs the same agent process and
conversation, and that the input box is empty, so it never overwrites something you were typing. Still:

- Use one input surface at a time. If you type in the terminal and on the phone at once, they can race.
- Agents change their output formats. Parsing of approval prompts, pickers and status lines is per-agent and can break after an agent update; please file an issue with the agent and version.
- Two Codex sessions in the same folder cannot be told apart, so the second one is read-only.
- Tool output and the agent's reasoning are deliberately left out of the chat.

## Development

```bash
python3 -m unittest test_session_controls test_agent_notices test_auth    # or: pytest
```

The tests use fixtures and mocks and need no running tmux or agents. See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).

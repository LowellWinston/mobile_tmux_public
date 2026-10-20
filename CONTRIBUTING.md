# Contributing

Issues and pull requests are welcome.

- Run the tests before sending a change: `python3 -m unittest test_session_controls test_agent_notices test_auth`.
- The server has no dependencies and no build step; please keep it that way. The page is `index.html` + `app.js`.
- Agent behavior differs a lot. When fixing something for one agent, check that Claude Code, Codex and Antigravity still behave consistently.
- Bugs that depend on an agent's screen output are easiest to fix with a captured sample: attach the relevant pane text (with `tmux capture-pane -p`)
  and the agent version, and trim anything private. Samples live under `test_fixtures/`.
- After changing `server.py`, `index.html` or `app.js`, restart the server: it loads the page once at start-up.

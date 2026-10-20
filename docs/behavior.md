# Behavior reference

How the chat view works in detail. See the [README](../README.md) for installing and running it.

Mobile Tmux is a phone interface for live
Codex, Claude Code, and Antigravity conversations in tmux. Compact session tabs scroll
horizontally within their own area, with a fixed opaque area reserved for the usage rings,
and show each agent's icon and Idle/Approval state. Working status appears
directly after the latest user message in the chat. Three rings
show 5-hour allowance, weekly allowance, and context use; tap for figures and reset times.
The + control creates a new detached tmux session in the selected pane’s directory.
Duplicate names are rejected. Choose No agent on an idle shell tab for compact command/output
mode: enter commands directly, without a ! prefix. Shell cd commands persist in that pane.
Full-screen interactive programs still require an interactive terminal. User messages render in light blue as plain `> message` prompts without bubble borders or padding. The composer expands while typing; desktop Enter sends,
Shift+Enter inserts a newline, and mobile Enter inserts a newline.

The + button beside the composer opens a three-item menu: Photo or file, Dictate, and Terminal (the live screen, with its Interact option). While dictating, the button shows a pulsing microphone and one tap stops it. Photo or file uploads to `/mobile/api/upload` (raw body, 20 MB limit, bound to the selected session and conversation, agent tabs only) and shows the file as a removable chip above the composer. On Send the saved paths are put in front of the typed text as plain paths, and the chips clear. A chip belongs to the tab it was picked in. JPEG, HEIC and oversized images are redrawn first (longest side 2400 px), which drops camera metadata; PNG screenshots, GIFs and other files go up unchanged. On desktop browsers, pasting an image (Ctrl/Cmd+V) anywhere on the page attaches it, except inside some other text field, and dropping files on the page does too. Files live in `~/.cache/mobile-tmux/uploads/` and are deleted after seven days.

The bridge discovers panes every five seconds. Codex maps to the newest non-archived
CLI/IDE thread in the pane's directory, excluding subagents. Multiple Codex panes with
the same directory are ambiguous and cannot send. Claude maps to its live registry's
process and exact tmux pane. Antigravity maps to the conversation lock held by its live
process. Before Antigravity creates a conversation, its startup view is bound to the exact
process and start time, so folder-trust choices can be answered from the phone. Plain shell
commands are enabled only after choosing No agent. Their tabs offer fixed
Codex, Claude, and Antigravity launch buttons, protected by an idle-shell and empty-draft
check. Starting a different agent in a pane automatically updates its chat mapping.
Launch buttons start a new CLI session in the pane’s current directory. During startup,
the composer shows “Starting …” and readiness checks temporarily run more frequently.

Messages go through bracketed paste and Enter into the selected live agent's tmux input,
including while it is working; the agent controls when it consumes that input. The bridge
checks process start time, pane ancestry, agent identity, conversation identity, and an
empty recognized input box. Claude’s queued-message placeholder is recognized as a
placeholder so subsequent busy messages can also be queued. Styled “Try …” hints are
recognized without treating actual typed text as a placeholder. Slash commands and `!`
commands are forwarded to the agent, which controls their support and scheduling. The UI distinguishes queued
Claude messages from consumed history; Claude sends them when its current work allows.
An existing Termius draft is never intentionally overwritten.
Concurrent terminal activity can still race these checks; use one input surface at a time.
POST requests include the selected pane and conversation; stale conversations are rejected.
Private agent inbox protocols and process-environment credentials are not used.
Submitted messages appear provisionally until matched in saved history. Successful input
submission does not claim that the agent has finished processing the message.

Recognized folder-trust prompts (including unnumbered choices) and yes/no approval choosers appear above the composer with their actual numbered
options, including conversation-scoped or persistent choices when the agent offers them.
Only the current actionable request is shown; numbered options appear once as controls.
No option is preselected; select a radio option and press Submit choice.
Each submission is bound to the visible request fingerprint and live conversation.
Unrecognized prompts require Termius.
Commands truncated by the native terminal are also truncated in the panel; expand them
in Termius before deciding. Permission display depends on each agent's terminal format.

History polls every two seconds while visible; background polling pauses. New messages
and updates to existing replies automatically scroll to the latest content. Unchanged
polls preserve your reading position; scroll positions on tab switches and composer drafts
are kept per conversation. All saved user/assistant text is available,
without an extra retention cap. Responses appear when persisted, not necessarily token by
token. Markdown uses locally vendored marked and DOMPurify; code and tables scroll inside
their blocks. Codex progress updates and prose stay visible, including text inside Markdown
collapsible sections; only Codex code blocks fold behind “Show code”. Tool output and reasoning are omitted;
internal clarification reply envelopes display only the answer. Shell-command envelopes
render as ! command plus output; XML tags, durations, and empty success metadata are omitted.
Antigravity quota warnings omitted from its transcript are captured from its visible
terminal output, deduplicated by Error ID, and kept in a private per-conversation notice
cache. They appear inline with their reset text and Error ID, and remain after scrolling
out of the terminal. Claude synthetic rate-limit errors and Codex limit-error events also appear as notices.
A passive server-side monitor checks live panes every two seconds, even when the web page
is closed, and retains recognized terminal-only quota notices per agent and conversation.
Transcript and terminal copies are deduplicated. Transient notices that disappear between
polls and unrecognized future terminal formats can still be missed.

Usage comes from native reports, without estimates:
- Codex: saved token counts and account-limit windows.
- Claude: `claude_usage.py` captures official status-line context and rate-limit fields,
  configured in `~/.claude/settings.json`; cache: `~/.cache/mobile-tmux/usage/<session>.json`.
  Fresh sessions can use the latest unexpired Claude allowance report, labeled “last known
  report”; context is never copied from another conversation and waits for native reporting.
- Antigravity: `antigravity_status.py` captures official status-line usage, model, and state
  fields in `~/.cache/mobile-tmux/antigravity/<conversation>.json`. Model-specific quotas
  choose the reported Gemini or Claude allowance bucket.
Missing or expired allowance reports show unavailable. Agent reports can lag behind activity.

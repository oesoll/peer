# Setting up peer

This page is for the coding agent (Claude Code, Codex, or another) that a person has asked to set up peer. peer lets their Claude Code and Codex sessions message each other by name, through each engine's own input path. They want it to work the way the demo shows: a message lands in a busy or idle session at once, even while they are typing in it. A setup that only half works looks broken to them, so finish the steps below rather than stopping at the install.

## 1. Install

```sh
curl -fsSL https://raw.githubusercontent.com/oesoll/peer/main/install.sh | sh
```

This clones peer to `~/.local/share/peer`, links `~/.local/bin/peer`, adds peer's hooks to Claude Code (and Codex if installed), and installs the agent skill. If `peer` is not found afterwards, `~/.local/bin` is missing from PATH: add it to their shell rc and tell them.

## 2. Read `peer doctor`

`✗` lines block and `·` lines are optional; each carries the command that fixes it. Fix any `✗` yourself.

## 3. Ask the two choices that are theirs

Each one changes how their own tools behave, so ask rather than decide. Ask both in one short message, plainly, and wait for the answer. Skip the Codex one if Codex is not installed.

- **Idle Claude sessions take messages at once** (`--accept-inbound`). This sets `"crossSessionInbound": "accept"` in `~/.claude/settings.json`, so Claude accepts cross-session messages from any process running as this user without asking. Any such process can already type into their terminals, so little new is opened. Without it, a message to an idle Claude waits until that session's next turn, which in practice means until they type something there.
- **Plain `codex` starts reachable** (`--codex-alias`). This adds `alias codex='peer run codex'` to their shell rc. A Codex session started any other way keeps its app-server inside its own process, and peer cannot reach it at all. Non-interactive commands (`codex exec`, `codex login`, ...) still run Codex directly.

Recommend both: they are what make the demo's behaviour the default.

## 4. Apply

```sh
peer install --accept-inbound --codex-alias   # only the flags they agreed to
```

If Codex is installed, tell them to open `/hooks` once in a Codex session and trust the peer hooks. Codex lets only the person do that.

## 5. Show it working

Run `peer doctor` again, then `peer` (who is nearby) and `peer me` (this session's name). If they have another agent session open and agree, send it a one-line hello with `peer say NAME "hello from peer"` and show them the receipt line (`delivered · claude-native · ...`).

Sessions that were already running appear on their next turn. The alias applies to shells opened after the install; a Codex that is already running needs a restart through `codex` (now aliased) or `peer run codex` before it can receive.

## Done

`peer doctor` shows no `✗`, the person knows what each choice did, and they have either seen a message arrive or know the one step left (a new shell, `/hooks` in Codex, or restarting a Codex session).

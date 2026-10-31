# peer

Let your coding agents talk to each other by name.

```
$ peer
work / main
  0.0 work-lead     claude  working 12s   ~/src/app
  0.1 work-review   claude  idle 3m       ~/src/app
  0.2 work-tests    codex   blocked(permission) 40s   ~/src/app
$ peer say work-review "the auth refactor is in; please review src/auth.ts"
delivered · claude-native · laptop/3f9c…
```

The reply shows up in the sender's own conversation as `[from work-review@laptop] …`.

## Why

When you run several Claude Code and Codex sessions side by side, getting one to talk to another usually means a script that types into the other terminal: read the screen, paste, check it landed, press Enter. Each step goes wrong in its own way. An Enter lands on an approval prompt and answers it for you, a message sticks to a half-typed draft, or a paste never gets submitted and nobody notices.

peer never types into a terminal. It hands the message to each engine's own input path:

- **Claude Code** — its built-in cross-session inbox, the same one its `SendMessage` tool uses between local sessions. It reaches any running session, idle or busy, without restarting it.
- **Codex** — the app-server's `turn/start` and `turn/steer`, for sessions started with `peer run codex`.

You get one command to send, real states instead of screen-reading (`working`, `blocked`, `idle`, `waiting-on(name)`), and an answer to "did it arrive?" in the same line.

## Install

```sh
git clone https://github.com/oesoll/peer && cd peer
./install.sh                    # links ~/.local/bin/peer, adds hooks, installs the agent skill
peer install --accept-inbound   # optional: let idle Claude sessions take messages at once (see below)
peer doctor                     # what works, what is missing, and the command that fixes it
```

You need Python 3.9+, and tmux for `peer spawn` and nearby-first listing. Codex delivery needs `websocket-client` (`pip install --user websocket-client`). After install, Codex asks you once in `/hooks` to trust the new hooks.

Sessions that were already running show up on their next turn.

## Use

Agents learn the commands from the bundled skill. To try it by hand:

```sh
peer                               # who is near you: same tmux session, similar folders, recent contacts
peer say NAME "text"               # delivered / queued (busy: next tool call) / held (waiting on its user)
peer ask NAME "question"           # wait up to 60s for the reply
peer read NAME --last 2            # their last answers, read from engine history, not the screen
peer spawn reviewer --kind codex   # start one in a detached tmux session, return when ready
peer name reviewer                 # rename the current session; the name follows resume
```

Names are made from the tmux session and pane label (`tmux set -p @peer review` gives `work-review`), otherwise from the folder and engine. A message to an agent that is waiting on its user is held and delivered once it is free.

### Other machines

```json
// ~/.config/peer/hosts.json
{ "server": { "ssh": "me@server" } }
```

peer runs `ssh me@server ~/.local/bin/peer _rpc …` for anything that lives there. Set `"peer"` in the entry if it is installed elsewhere. Install peer on that machine too, and add this one to its hosts.json so replies can come back.

### Your own launchers

If you start agents through wrappers, tell `peer spawn` and `peer run` about them:

```json
// ~/.config/peer/launch.json
{ "claude": ["my-claude-wrapper"], "codex": ["codex"] }
```

## `--accept-inbound`, plainly

Claude Code delivers a cross-session message without asking only when it can vouch for the sender, which means the sender is another Claude session running in the same permission mode. The peer worker is not a Claude session. So without this setting, Claude holds the message behind an approval prompt, and peer waits for the hook route instead (the session's next tool call or turn).

`peer install --accept-inbound` sets `"crossSessionInbound": "accept"` in `~/.claude/settings.json`. Claude then takes cross-session messages from any process running as your user. Such a process can already type into your terminals, so this opens little that was not open. Still, decide it on purpose.

## Limits

- Claude's cross-session inbox is not a public API. peer uses the protocol it observed (`peerProtocol: 1`) and falls back to hooks when that changes, but a Claude Code update can break the fast path.
- Codex sessions are reachable only when started with `peer run codex` or `peer spawn --kind codex`. Codex support is newer and less tested than Claude support.
- Between machines, the receiving side trusts the origin name it is given. ssh has already proven you are the same user there.
- A session doing background work (a subagent, a long shell) can show as `idle`.

Not affiliated with Anthropic or OpenAI. MIT licensed.

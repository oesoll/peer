#!/bin/sh
# Link peer onto PATH, hook it into Claude Code and Codex, install the agent skill, then check.
# Extra arguments go to `peer install` (for example --accept-inbound).
set -eu
case "$0" in
    *install.sh) here=$(cd "$(dirname "$0")" && pwd) ;;
    *) here="" ;;   # piped: $0 is the shell, not this file
esac
# Piped from curl there is no checkout next to this script: fetch one, then run its installer.
if [ ! -x "$here/bin/peer" ]; then
    dest="${PEER_HOME:-$HOME/.local/share/peer}"
    if [ -d "$dest/.git" ]; then
        git -C "$dest" pull --ff-only --quiet
    else
        git clone --depth 1 --quiet https://github.com/oesoll/peer "$dest"
    fi
    exec sh "$dest/install.sh" "$@"
fi
mkdir -p "$HOME/.local/bin"
ln -sfn "$here/bin/peer" "$HOME/.local/bin/peer"
"$here/bin/peer" install "$@"
for skills in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    if [ -d "$(dirname "$skills")" ]; then
        mkdir -p "$skills"
        ln -sfn "$here/skills/peer" "$skills/peer"
    fi
done
echo
"$here/bin/peer" doctor || true
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo; echo "Add ~/.local/bin to PATH so agents can run peer." ;;
esac

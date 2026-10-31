#!/bin/sh
# Link peer onto PATH, hook it into Claude Code and Codex, install the agent skill, then check.
# Extra arguments go to `peer install` (for example --accept-inbound).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
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

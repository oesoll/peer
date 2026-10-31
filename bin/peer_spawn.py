"""peer spawn: start a named Claude or Codex in a detached tmux session and return when it is ready."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from peer_core import config, host, launcher, public, read, root, sessions, valid_name
from peer_transport import BIN, remote


def spawn(argv):
    parser = argparse.ArgumentParser(prog='peer spawn')
    parser.add_argument('name')
    parser.add_argument('--kind', choices=('claude', 'codex'), required=True)
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--host', default=host())
    parser.add_argument('--timeout', type=float, default=30)
    # REMAINDER would swallow --kind after name; split native arguments explicitly.
    extra = argv[argv.index('--') + 1:] if '--' in argv else []
    prefix = argv[:argv.index('--')] if '--' in argv else argv
    args = parser.parse_args(prefix)
    valid_name(args.name)
    if args.host != host():
        return remote(args.host, {'op': 'spawn', 'name': args.name, 'kind': args.kind,
                                  'cwd': args.cwd, 'args': extra, 'timeout': min(args.timeout, 5)})
    if any(r['name'] == args.name for r in sessions()):
        raise ValueError('name belongs to an existing session; resume it or choose another name')
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError('working directory does not exist: ' + str(cwd))
    engine = launcher(args.kind) if args.kind == 'claude' else [sys.executable, str(BIN / 'peer-session'), 'codex']
    if not shutil.which(launcher(args.kind)[0]):
        raise ValueError(launcher(args.kind)[0] + ' is not on PATH (see launch.json in peer doctor)')
    env = dict(os.environ, PEER_NAME=args.name, PEER_STATE=str(root()), PEER_CONFIG=str(config()))
    command = ['env', 'PEER_NAME=' + args.name, 'PEER_STATE=' + str(root()), 'PEER_CONFIG=' + str(config())] + engine + extra
    sock = os.environ.get('TMUX', '').split(',')[0]
    tmux = ['tmux'] + (['-S', sock] if sock else [])
    # Start a private detached session; no focus change or implicit mutation of
    # the user's current layout. Grouping/splitting can be added when requested.
    label = 'peer-' + args.name + '-' + secrets.token_hex(2)
    output = subprocess.check_output(tmux + ['new-session', '-d', '-P', '-F', '#{pane_id}', '-s', label,
                                           '-c', str(cwd), shlex.join(command)], env=env, text=True).strip()
    end = time.monotonic() + min(args.timeout, 60)
    while time.monotonic() < end:
        row = next((r for r in sessions() if r['name'] == args.name), None)
        if row:
            state = public(row)
            if state['state'] in ('idle', 'working', 'blocked', 'error'):
                return dict(peer=state, ready=state['state'] in ('idle', 'working'))
        time.sleep(.1)
    return dict(name=args.name, ready=False, target=output, reason='startup is waiting; inspect peer read after the hook registers')

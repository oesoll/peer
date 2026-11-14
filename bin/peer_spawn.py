"""peer spawn: start a named agent and return when it is ready.

With the session layer (sm) installed, sm starts and keeps it, so it survives closed terminals and can be
resumed by name. Without it, it starts in a detached tmux session."""
import argparse
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import time
from peer_core import config, host, launcher, me, public, python, root, sessions, sm, valid_name
from peer_transport import BIN, remote


def spawn(argv):
    parser = argparse.ArgumentParser(prog='peer spawn')
    parser.add_argument('name')
    parser.add_argument('--kind', choices=('claude', 'codex', 'omo'), required=True)
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--host', default=host())
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--env', action='append', default=[], help='K=V for the session')
    # sm only: where the session shows. Without sm there is nothing to place it in.
    parser.add_argument('--pane', help=argparse.SUPPRESS)
    parser.add_argument('--visible', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--hold', action='store_true', help=argparse.SUPPRESS)  # older callers; sm always holds
    parser.add_argument('--parent', help=argparse.SUPPRESS)  # forwarded by the spawning host
    # REMAINDER would swallow --kind after name; split native arguments explicitly.
    extra = argv[argv.index('--') + 1:] if '--' in argv else []
    prefix = argv[:argv.index('--')] if '--' in argv else argv
    args = parser.parse_args(prefix)
    valid_name(args.name)
    for item in args.env:
        if '=' not in item:
            raise ValueError('--env takes K=V: ' + item)
    parent = args.parent
    if not parent:
        mine = me(required=False)
        parent = host() + '/' + mine['id'] if mine else None
    if args.host != host():
        return remote(args.host, {'op': 'spawn', 'name': args.name, 'kind': args.kind,
                                  'cwd': args.cwd, 'args': extra, 'timeout': min(args.timeout, 5),
                                  'pane': args.pane, 'visible': args.visible, 'parent': parent, 'env': args.env})
    layer = sm()
    if layer:
        return layer.spawn(args.name, args.kind, args.cwd, args.pane, args.visible, args.env, extra, parent,
                           timeout=args.timeout)
    if args.pane or args.visible:
        raise ValueError('--pane and --visible place a session in sm, which is not installed here')
    return tmux_spawn(args, extra, parent)


def tmux_spawn(args, extra, parent):
    if any(r['name'] == args.name for r in sessions()):
        raise ValueError('name belongs to an existing session; resume it or choose another name')
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError('working directory does not exist: ' + str(cwd))
    if not shutil.which('tmux'):
        raise ValueError('peer spawn needs tmux (or sm) on this machine')
    if not shutil.which(launcher(args.kind)[0]):
        raise ValueError(launcher(args.kind)[0] + ' is not on PATH (see launch.json in peer doctor)')
    # Claude needs no launcher; Codex and OmO go through peer-session so peer can reach them.
    engine = launcher('claude') if args.kind == 'claude' else [python(), str(BIN / 'peer-session'), args.kind]
    identity = ['PEER_NAME=' + args.name, 'PEER_STATE=' + str(root()), 'PEER_CONFIG=' + str(config())]
    identity += ['PEER_PARENT=' + parent] if parent else []
    command = ['env'] + identity + list(args.env) + engine + extra
    sock = os.environ.get('TMUX', '').split(',')[0]
    tmux = ['tmux'] + (['-S', sock] if sock else [])
    # Start a private detached session; no focus change or implicit mutation of
    # the user's current layout. Grouping/splitting can be added when requested.
    label = 'peer-' + args.name + '-' + secrets.token_hex(2)
    output = subprocess.check_output(tmux + ['new-session', '-d', '-P', '-F', '#{pane_id}', '-s', label,
                                             '-c', str(cwd), shlex.join(command)], text=True).strip()
    end = time.monotonic() + min(args.timeout, 60)
    while time.monotonic() < end:
        row = next((r for r in sessions() if r['name'] == args.name), None)
        if row:
            state = public(row)
            if state['state'] in ('idle', 'working', 'blocked', 'error'):
                return dict(peer=state, ready=state['state'] in ('idle', 'working'))
        time.sleep(.1)
    return dict(name=args.name, ready=False, target=output, reason='startup is waiting; inspect peer read after the hook registers')

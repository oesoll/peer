"""Observe the private app-server owned by one peer-session launcher."""
import os
from pathlib import Path
import time
from peer_core import claim_name, host, pane_label, lock, path, process, read, session_key, target, write
from peer_transport import CodexClient


def watch(server, endpoint, stopped):
    while not stopped.is_set() and server.poll() is None:
        try:
            with CodexClient(endpoint) as client:
                while not stopped.is_set() and server.poll() is None:
                    for sid in client.call('thread/loaded/list', {}).get('data', []):
                        thread = client.call('thread/read', {'threadId': sid})['thread']
                        # The TUI also loads ephemeral helper threads (no rollout file). Naming them
                        # took PEER_NAME-2 and replies left under the wrong name.
                        if thread.get('parentThreadId') or thread.get('ephemeral'):
                            continue
                        ident = session_key('codex', sid)
                        status = thread['status']
                        flags = status.get('activeFlags') or []
                        state = {'idle':'idle','active':'working','systemError':'error','notLoaded':'gone'}.get(status['type'], 'unknown')
                        if flags:
                            state = 'blocked'
                        file = path('sessions', ident)
                        with lock(file, .02):
                            previous = read(file)
                            # PEER_NAME comes from peer spawn; otherwise name it the way the hook would.
                            name = previous.get('name') or claim_name(ident, os.environ.get('PEER_NAME') or pane_label()
                                                                      or (Path(thread['cwd']).name or 'agent')[:45] + '-codex')
                            now = time.time()
                            row = dict(id=ident, session=sid, kind='codex', name=name, host=host(),
                                       cwd=thread['cwd'], title=thread.get('name'), target=target(), process=process(server.pid), transcript=thread.get('path'),
                                       state=state, reason=','.join(flags) or 'app-server',
                                       since=previous.get('since', now) if previous.get('state') == state else now,
                                       observed=now, codex_socket=endpoint, codex_home=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')),
                                       tmux_socket=os.environ.get('TMUX','').split(',')[0])
                            write(file, row)
                    stopped.wait(.3)
        except Exception:
            stopped.wait(.5)

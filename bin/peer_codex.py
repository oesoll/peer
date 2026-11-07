"""Observe the private app-server owned by one peer-session launcher."""
import os
from pathlib import Path
import time
from peer_core import claim_name, host, pane_label, lock, path, process, read, record_turn_failure, session_key, target, write
from peer_transport import CodexClient


def last_turn(client, sid):
    """How the newest turn ended: (failed?, why). A failed turn leaves the thread in systemError or plain idle."""
    turns = client.call('thread/turns/list', {'threadId': sid, 'limit': 1, 'itemsView': 'notLoaded'}).get('data') or []
    if not turns or turns[0].get('status') != 'failed':
        return False, None
    error = turns[0].get('error') or {}
    return True, ' '.join((error.get('message') or error.get('codexErrorInfo') or 'turn-failed').split())[:300]


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
                        before = read(file).get('state')
                        # A turn just ended, or the thread just entered an error: ask how. Polling only at these
                        # moments keeps the 0.3 s loop cheap.
                        failed, why = False, None
                        if before != state and (before == 'working' or state == 'error') and state != 'working':
                            failed, why = last_turn(client, sid)
                        with lock(file, .02):
                            previous = read(file)
                            # PEER_NAME comes from peer spawn; otherwise name it the way the hook would.
                            name = previous.get('name') or claim_name(ident, os.environ.get('PEER_NAME') or pane_label()
                                                                      or (Path(thread['cwd']).name or 'agent')[:45] + '-codex')
                            now = time.time()
                            finished = before == 'working' and state == 'idle' and not failed
                            if failed:
                                state = 'error'
                            row = dict(id=ident, session=sid, kind='codex', name=name, host=host(),
                                       cwd=thread['cwd'], title=thread.get('name'), target=target(), process=process(server.pid), transcript=thread.get('path'),
                                       state=state, turn_ok_at=now if finished else previous.get('turn_ok_at'),
                                       reason=','.join(flags) or why or (previous.get('reason') if state == previous.get('state') == 'error' else None) or 'app-server',
                                       since=previous.get('since', now) if previous.get('state') == state else now,
                                       observed=now, codex_socket=endpoint, codex_home=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')),
                                       tmux_socket=os.environ.get('TMUX','').split(',')[0])
                            write(file, row)
                            if failed:
                                record_turn_failure(row)
                        if failed:
                            from peer_delivery import start_worker
                            start_worker()
                    stopped.wait(.3)
        except Exception:
            stopped.wait(.5)

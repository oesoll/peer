"""Owner-side durable mailbox. Network runs here, never in peer-hook."""
import html
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from peer_core import (alive, envelope, host, inbox, key, lock, mail_path, mark_delivered,
                       path, public, read, receipt, root, session, sessions, update_receipt, write)
from peer_transport import BIN, CodexClient, RPCError


def start_worker():
    # One flock-protected worker per state directory; no service installation.
    # Spawn is outside the hook. The winner stays until its mailboxes are empty.
    subprocess.Popen([sys.executable, str(BIN / 'peer'), '_work'], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     start_new_session=True)


def receive(request, source_host):
    ident = request['id']
    if not re.fullmatch('[a-f0-9]{24}', ident):
        raise ValueError('invalid message id')
    sender = request['sender']
    if sender['host'] != source_host:
        raise ValueError('sender host does not match authenticated transport')
    recipient = session(request['recipient'])
    if not recipient:
        raise ValueError('recipient session not registered; refresh peer list')
    if not isinstance(request['body'], str) or not 0 < len(request['body'].encode()) <= 64 * 1024:
        raise ValueError('message must contain 1–65536 UTF-8 bytes')
    with lock(path('delivery', ident)):
        previous = receipt(ident)
        if previous:
            if previous['recipient'] != recipient['id'] or previous['sender'] != sender:
                raise ValueError('message id collision')
            return previous  # transport retry is idempotent
        now = time.time()
        stage = 'held' if public(recipient)['state'] in ('blocked', 'gone', 'unknown') else 'queued'
        mail = dict(id=ident, recipient=recipient['id'], sender=sender, body=request['body'], created=now,
                    reply_to=request.get('reply_to'), ask=bool(request.get('ask')))
        rec = {k: v for k, v in mail.items() if k != 'body'}
        rec.update(stage=stage, updated=now, route=None)
        write(mail_path(recipient['id'], ident), mail)
        write(path('receipts', ident), rec)
        # Replies to a live ask are consumed by its waiting CLI, never injected
        # into the same agent while that tool invocation is still waiting.
        with lock(path('waiting', recipient['id'])):
            waiting = read(path('waiting', recipient['id']))
            if (waiting.get('until', 0) > now and alive(waiting) and waiting.get('peer_id') == sender['id']
                    and waiting.get('peer_host') == source_host and not read(path('answers', waiting['message']))):
                write(path('answers', waiting['message']), mail)
                mark_delivered(mail, 'ask-return', 'waiting CLI mailbox')
                return receipt(ident)
    start_worker()
    return receipt(ident)


def claude_native(row):
    """Claude Code's own cross-session inbox, the one its SendMessage tool uses between local sessions.

    ~/.claude/sessions/<pid>.json names the socket and <pid>.*.key holds its token (readable by the same
    user only). It reaches any running Claude, idle or busy, without restarting it. The protocol is not
    public: peerProtocol 1 is what was observed, and anything else falls back to the hook route."""
    pid = (row.get('process') or {}).get('pid')
    config_dir = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    # Without "accept" Claude holds the message behind an approval screen, because the peer worker is
    # not itself a Claude session it can vouch for. Better to wait for the hook route than ask the user.
    if read(config_dir / 'settings.json').get('crossSessionInbound') != 'accept':
        return None
    base = config_dir / 'sessions'
    meta = read(base / ('%s.json' % pid))
    if meta.get('sessionId') != row['session'] or meta.get('peerProtocol') != 1 or not meta.get('messagingSocketPath'):
        return None
    for key_file in base.glob('%s.*.key' % pid):
        token = read(key_file).get('peerToken')
        if token:
            return {'socket': meta['messagingSocketPath'], 'token': token}
    return None


def native_send(endpoint, mail):
    """False when the inbox could not be reached (nothing was written); raises once bytes went out."""
    sender = mail['sender']
    mode = sender.get('mode') if sender.get('mode') in ('bypass', 'default') else 'default'
    origin = html.escape('peer:' + sender['name'] + '@' + sender['host'], quote=True)
    content = ('<cross-session-message from="%s" from-name="%s" from-mode="%s">\n%s\n</cross-session-message>'
               % (origin, html.escape(sender['name'], quote=True), mode, envelope(mail)))
    message = {'msgV': 1, 'msg_id': str(uuid.UUID(mail['id'] + '00000000')), 'type': 'user', 'priority': 'next',
               'message': {'role': 'user', 'content': content}, 'from': 'peer:' + sender['name'] + '@' + sender['host']}
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(3)
    try:
        sock.connect(endpoint['socket'])
    except OSError:
        sock.close()
        return False
    with sock:
        sock.sendall(json.dumps({'type': 'auth', 'token': endpoint['token']}).encode() + b'\n'
                     + json.dumps(message, ensure_ascii=False).encode() + b'\n')
        sock.shutdown(socket.SHUT_WR)
    return True


def codex_send(row, mail):
    with CodexClient(row['codex_socket']) as client:
        thread = client.call('thread/read', {'threadId': row['session']})['thread']
        state = thread['status']
        flags = state.get('activeFlags', [])
        if state['type'] == 'notLoaded' or any('waiting' in str(f).lower() for f in flags):
            return 'held', 'app-server awaiting user or session unloaded'
        if thread.get('canAcceptDirectInput') is False:
            return 'held', 'thread does not accept direct input'
        params = {'threadId': row['session'], 'input': [{'type': 'text', 'text': envelope(mail)}],
                  'clientUserMessageId': mail['id']}
        if state['type'] == 'active':
            turns = client.call('thread/turns/list', {'threadId': row['session'], 'limit': 1, 'itemsView': 'notLoaded'})['data']
            active = next((t for t in turns if t['status'] == 'inProgress'), None)
            if not active:
                return 'queued', 'turn changed; retry at next observation'
            params['expectedTurnId'] = active['id']
            update_receipt(mail['id'], stage='sending', route='codex-steer')
            client.call('turn/steer', params)
            mark_delivered(mail, 'codex-steer', 'app-server accepted active turn input')
        elif state['type'] == 'idle':
            update_receipt(mail['id'], stage='sending', route='codex-start')
            client.call('turn/start', params)
            mark_delivered(mail, 'codex-start', 'app-server started turn')
        else:
            return 'held', 'app-server state: ' + state['type']
    return None


# What a held message is waiting for, said so the sender knows whether to wait or act.
HELD = {
    'blocked': 'waiting on its user (an approval or a question); delivered once that is answered',
    'gone': 'that session has ended; delivered if it is resumed',
    'unknown': 'its state is unknown; retried as soon as it reports in',
    'error': 'its engine reported an error; retried once it recovers',
}


def attempt(file):
    mail = read(file)
    if not mail:
        return
    with lock(path('delivery', mail['id']), .005):
        rec = receipt(mail['id'])
        if rec.get('stage') not in ('queued', 'held'):
            return
        row = session(mail['recipient'])
        state = public(row)['state']
        if state in ('blocked', 'gone', 'unknown', 'error'):
            update_receipt(mail['id'], stage='held', reason=HELD.get(state, state))
            return
        try:
            if row['kind'] == 'codex' and row.get('codex_socket'):
                result = codex_send(row, mail)
                if result:
                    update_receipt(mail['id'], stage=result[0], reason=result[1])
                return
            if row['kind'] == 'claude':
                native = claude_native(row)
                if native:
                    update_receipt(mail['id'], stage='sending', route='claude-native')
                    if native_send(native, mail):
                        update_receipt(mail['id'], stage='accepted', reason='Claude inbox accepted; awaiting transcript evidence')
                        return
                    update_receipt(mail['id'], stage='queued', route=None, reason='Claude inbox unreachable')
                # No inbox route: the hook hands it over at the next tool call or turn end.
                if state in ('working', 'waiting-on'):
                    update_receipt(mail['id'], stage='queued', route='claude-hook', reason='lands at its next tool call or turn end')
                else:
                    update_receipt(mail['id'], stage='held', reason='idle Claude reads it on its next turn; to deliver at once: peer install --accept-inbound')
                return
            update_receipt(mail['id'], stage='held', reason='this Codex was not started through peer, so nothing can reach it; '
                       'restart it with `peer run codex` (peer install --codex-alias makes plain `codex` do that)')
        except Exception as error:
            # Retrying an unacknowledged native send could duplicate work. Keep
            # the mailbox for diagnosis; only unsent connection failures retry.
            after = receipt(mail['id'])
            uncertain = after.get('stage') == 'sending'
            update_receipt(mail['id'], stage='uncertain' if uncertain else 'held', reason=type(error).__name__)


def transcript_evidence(row):
    """Read a bounded transcript tail; retain only evidence, never conversation text."""
    transcript = row.get('transcript')
    if not transcript:
        return
    try:
        with open(transcript, 'rb') as stream:
            stream.seek(0, 2)
            length = stream.tell()
            stream.seek(max(0, length - 256 * 1024))
            lines = stream.read().splitlines()
        text = ''
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get('type') == 'user':
                text += json.dumps(item.get('message', {}))
        for ident in set(re.findall(r'\[peer-id:([a-f0-9]{24})\]', text)):
            rec = receipt(ident)
            if rec.get('recipient') != row['id'] or rec.get('stage') not in ('accepted', 'uncertain', 'sending'):
                continue
            mail = read(mail_path(row['id'], ident))
            if mail:
                with lock(path('delivery', ident), .005):
                    mark_delivered(mail, rec.get('route'), 'user message in Claude transcript')
    except (OSError, TimeoutError):
        pass


ACCEPTED_TTL = 300  # an engine took it but left no transcript evidence; stop holding the body


def settle_accepted(file):
    """Close a message the engine accepted long ago without evidence (a transcript line we could
    not match). Its body must not sit on disk, and it must not keep the worker up."""
    mail = read(file)
    rec = receipt(mail.get('id', '')) if mail else {}
    if rec.get('stage') == 'accepted' and time.time() - rec.get('updated', time.time()) > ACCEPTED_TTL:
        update_receipt(mail['id'], reason='handed to the engine; no transcript evidence')
        file.unlink(missing_ok=True)


def code_stamp():
    return [p.stat().st_mtime for p in sorted(Path(__file__).parent.glob('peer*'))]


def work(once=False):
    try:
        with lock(root() / 'worker', .005):
            from peer_core import process
            write(root() / 'worker.json', process(os.getpid()))
            idle = time.monotonic()
            stamp = code_stamp()
            while True:
                if code_stamp() != stamp:
                    return  # deployed anew; the next receive starts a worker on the new code
                for file in (root() / 'mail').glob('*/*.json'):
                    settle_accepted(file)
                files = list((root() / 'mail').glob('*/*.json'))
                for file in files:
                    try:
                        attempt(file)
                    except (OSError, TimeoutError):
                        continue
                for row in sessions():
                    if row['kind'] == 'claude' and alive(row):
                        transcript_evidence(row)
                if once:
                    return
                if files:
                    idle = time.monotonic()
                elif time.monotonic() - idle > 10:
                    return
                time.sleep(.25)
    except TimeoutError:
        pass

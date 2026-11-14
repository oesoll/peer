"""Owner-side durable mailbox. Network runs here, never in peer-hook."""
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time
import uuid
from peer_core import (asleep, python, revive, alive, envelope, host, inbox, key, lock, mail_path, mark_delivered,
                       path, public, read, receipt, root, session, sessions, sm, update_receipt, write)
from peer_transport import BIN, CodexClient, RPCError, remote


def start_worker():
    # One flock-protected worker per state directory; no service installation.
    # Spawn is outside the hook. The winner stays until its mailboxes are empty.
    subprocess.Popen([python(), str(BIN / 'peer'), '_work'], stdin=subprocess.DEVNULL,
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
    recipient = revive(recipient)
    if not isinstance(request['body'], str) or not 0 < len(request['body'].encode()) <= 64 * 1024:
        raise ValueError('message must contain 1–65536 UTF-8 bytes')
    with lock(path('delivery', ident)):
        previous = receipt(ident)
        if previous:
            if previous['recipient'] != recipient['id'] or previous['sender'] != sender:
                raise ValueError('message id collision')
            if previous.get('body_hash') and previous['body_hash'] != hashlib.sha256(request['body'].encode()).hexdigest():
                raise ValueError('message id content conflict; reuse an id only for the same message')
            if previous.get('stage') in ('queued', 'held'):
                start_worker()  # a retry of waiting mail: the worker may have exited since
            return previous  # transport retry is idempotent
        now = time.time()
        stage = 'held' if public(recipient)['state'] in ('blocked', 'gone', 'unknown') else 'queued'
        mail = dict(id=ident, recipient=recipient['id'], sender=sender, body=request['body'], created=now,
                    reply_to=request.get('reply_to'), ask=bool(request.get('ask')))
        rec = {k: v for k, v in mail.items() if k != 'body'}
        rec.update(stage=stage, updated=now, route=None, body_hash=hashlib.sha256(request['body'].encode()).hexdigest())
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


def claude_native_of_process(row):
    """The inbox of the conversation this record's process is in now. A record can outlive its session id while its
    process runs on in another conversation (/clear, /resume). claude_native() refuses the old record, since the inbox
    belongs to another conversation id, so its mail would wait for a hook that never comes. Same pid and same start
    time mean the same running agent, so its live inbox is the right door."""
    process = row.get('process')
    if not process:
        return None
    for other in sessions():
        if other['id'] != row['id'] and other.get('kind') == 'claude' and other.get('process') == process:
            native = claude_native(other)
            if native:
                return native
    return None


def native_send(endpoint, mail):
    """False when the inbox could not be reached (nothing was written); raises once bytes went out."""
    sender = mail['sender']
    mode = sender.get('mode') if sender.get('mode') in ('bypass', 'default') else 'default'
    origin = html.escape('peer:' + sender['name'] + '@' + sender['host'], quote=True)
    content = ('<cross-session-message from="%s" from-name="%s" from-mode="%s">\n%s\n</cross-session-message>'
               % (origin, html.escape(sender['name'] + '@' + sender['host'], quote=True), mode, envelope(mail, labelled=False)))
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


def omo_deliver(row, mail):
    """OmO takes mail through its peer extension (peer-omo.js): one JSON line in, one out. The extension hands the text
    to the session as user input, as a steer while a turn runs (after its tool calls) or as a new turn when idle."""
    sock = row.get('omo_socket')
    if not sock or not Path(sock).exists():
        update_receipt(mail['id'], stage='held', reason='OmO session without the peer extension; start it through peer-session')
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)
        try:
            conn.connect(sock)
        except OSError:
            update_receipt(mail['id'], stage='held', reason='OmO peer socket did not answer')
            return
        update_receipt(mail['id'], stage='sending', route='omo-socket')
        conn.sendall((json.dumps({'id': mail['id'], 'text': envelope(mail)}, ensure_ascii=False) + '\n').encode())
        data = b''
        while not data.endswith(b'\n'):
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data or b'{}')
    if reply.get('ok'):
        mark_delivered(mail, 'omo-' + str(reply.get('mode') or 'start'), 'OmO took it as user input')
    else:
        update_receipt(mail['id'], stage='held', reason='OmO refused: ' + str(reply.get('error') or 'no reply')[:200])


def wake(row):
    """Ask sm to wake a session it put to sleep (`sm-session wake NAME` resumes the same engine session), once;
    the next attempt delivers."""
    layer = sm()
    if not layer:
        return
    mark = root() / 'waking' / (key(row['id']) + '.json')
    if time.time() - read(mark).get('at', 0) < 90:
        return  # already waking
    write(mark, dict(at=time.time(), name=row['name']))
    subprocess.Popen([python(), str(Path(layer.BIN) / 'sm-session'), 'wake', row['name']], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)


# What a held message is waiting for, said so the sender knows whether to wait or act.
HELD = {
    'blocked': 'waiting on its user (an approval or a question); delivered once that is answered',
    'gone': 'that session has ended; delivered if it is resumed',
    'unknown': 'its state is unknown; retried as soon as it reports in',
    'error': 'its engine reported an error; retried once it recovers',
}


MAIL_TTL = 6 * 3600  # undelivered this long: the moment has passed, so close it rather than inject it late


def attempt(file):
    mail = read(file)
    if not mail:
        return
    with lock(path('delivery', mail['id']), .005):
        rec = receipt(mail['id'])
        if rec.get('stage') not in ('queued', 'held'):
            return
        if time.time() - (mail.get('created') or time.time()) > MAIL_TTL:
            # Days-old mail delivered at once would bury the agent in stale asks, and while it waits the worker
            # never goes idle.
            update_receipt(mail['id'], stage='failed', reason='not delivered within %d h' % (MAIL_TTL // 3600))
            Path(file).unlink(missing_ok=True)
            return
        row = session(mail['recipient'])
        if not row:
            update_receipt(mail['id'], stage='failed', reason='recipient session record removed')
            Path(file).unlink(missing_ok=True)
            return
        row = revive(row)  # resumed without a hook yet: still deliverable
        if asleep(row):
            wake(row)
            update_receipt(mail['id'], stage='queued', reason='waking the session')
            return
        state = public(row)['state']
        if state in ('blocked', 'gone', 'unknown', 'error'):
            why = public(row).get('reason') if state == 'error' else None
            update_receipt(mail['id'], stage='held', reason=HELD.get(state, state) + (': ' + why if why else ''))
            return
        try:
            if row['kind'] == 'omo':
                omo_deliver(row, mail)
                return
            if row['kind'] == 'codex' and row.get('codex_socket'):
                result = codex_send(row, mail)
                if result:
                    update_receipt(mail['id'], stage=result[0], reason=result[1])
                return
            if row['kind'] == 'claude':
                native = claude_native(row) or claude_native_of_process(row)
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


NOTICE_WINDOW = 6 * 3600  # same horizon as MAIL_TTL: older requests are not waiting on this turn any more


def notify_turn_failures():
    """An agent's turn failed (rate limit, overload, a dead API call), at its start or halfway. Whoever gave it work
    has no other way to learn that: `delivered` was true, and no reply will ever come. Each sender hears once, with the
    reason."""
    now = time.time()
    for file in (root() / 'turn-failures').glob('*.json'):
        failure = read(file)
        since = max(failure.get('after') or 0, now - NOTICE_WINDOW)
        senders = {}
        for item in (root() / 'receipts').glob('*.json'):
            rec = read(item)
            sender = rec.get('sender') or {}
            if rec.get('recipient') != failure.get('recipient') or rec.get('turn_failed') \
                    or rec.get('stage') not in ('delivered', 'accepted', 'uncertain') \
                    or (rec.get('delivered') or rec.get('updated') or 0) <= since \
                    or str(sender.get('id', '')).startswith('service:'):
                continue
            senders.setdefault((sender['id'], sender['host']), []).append(rec['id'])
        unreached = False
        for (ident, owner), messages in senders.items():
            body = ("%s's turn failed, so what you sent it was probably not handled and no reply is coming: %s"
                    % (failure.get('name'), failure.get('reason')))
            request = dict(op='receive', id=secrets.token_hex(12), recipient=ident, body=body,
                           sender=dict(id='service:peer', name='peer', host=host(), mode='default'))
            try:
                if owner == host():
                    receive(request, host())
                else:
                    remote(owner, request)
            except Exception:  # that machine or session is away; a few more tries, then give up rather than linger
                unreached = True
                continue
            for message in messages:
                update_receipt(message, turn_failed=failure.get('reason'))
        attempts = failure.get('attempts', 0) + 1
        if unreached and attempts < 5:
            write(file, dict(failure, attempts=attempts))
        else:
            Path(file).unlink(missing_ok=True)


EVIDENCE_STAGES = ('accepted', 'uncertain', 'sending')
RETRY_AFTER = 5  # seconds before mail that could not go (held, or waiting for a hook) is tried again


def work(once=False):
    restart = False
    try:
        with lock(root() / 'worker', .005):
            from peer_core import process
            write(root() / 'worker.json', process(os.getpid()))
            idle = time.monotonic()
            stamp = code_stamp()
            due = {}   # mail file -> when to try it again; new mail goes at once, mail that could not go waits
            seen = {}  # session -> transcript (size, mtime) when last searched for evidence
            while True:
                if code_stamp() != stamp:
                    restart = any((root() / 'mail').glob('*/*.json'))
                    return
                notify_turn_failures()
                files = list((root() / 'mail').glob('*/*.json'))
                now = time.monotonic()
                expecting = set()
                for file in files:
                    if due.get(file, 0) > now:
                        continue
                    settle_accepted(file)
                    try:
                        attempt(file)
                    except (OSError, TimeoutError):
                        continue
                    mail = read(file)
                    stage = receipt(mail['id']).get('stage') if mail else None
                    if stage in EVIDENCE_STAGES:
                        expecting.add(mail['recipient'])
                    elif stage in ('queued', 'held'):
                        due[file] = now + RETRY_AFTER
                due = {f: t for f, t in due.items() if f in files}
                # Only a recipient with mail awaiting its transcript line is read, and only when the transcript
                # moved. Reading every live Claude's last 256 KB four times a second was most of the worker's CPU.
                for rid in expecting:
                    row = session(rid)
                    if not row or row['kind'] != 'claude' or not row.get('transcript') or not alive(row):
                        continue
                    try:
                        info = os.stat(row['transcript'])
                    except OSError:
                        continue
                    if seen.get(rid) != (info.st_size, info.st_mtime):
                        seen[rid] = (info.st_size, info.st_mtime)
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
    finally:
        # Release the worker lock before launching its replacement, so waiting mail
        # continues after an update without needing another send to start a worker.
        if restart:
            start_worker()

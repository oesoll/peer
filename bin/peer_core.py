"""Peer identity and local state. Hook-safe: kernel reads and small atomic files only."""
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time

def process(pid):
    """PID + kernel start time prevents stale observations after PID reuse."""
    try:
        if sys.platform == 'darwin':
            buf = ctypes.create_string_buffer(136)
            lib = ctypes.CDLL('/usr/lib/libproc.dylib')
            if lib.proc_pidinfo(int(pid), 3, 0, buf, 136) != 136:
                return None
            data = buf.raw
            return {'pid': int(pid), 'parent': struct.unpack_from('I', data, 16)[0],
                    'start': list(struct.unpack_from('QQ', data, 120)),
                    'name': data[48:64].split(b'\0')[0].decode(errors='replace')}
        data = Path('/proc/%s/stat' % pid).read_text()
        name, rest = data.split('(', 1)[1].rsplit(')', 1)
        fields = rest.split()
        return {'pid': int(pid), 'parent': int(fields[1]), 'start': fields[19], 'name': name}
    except (OSError, ValueError):
        return None


def ancestry():
    rows = []
    pid = os.getppid()
    for _ in range(32):
        row = process(pid)
        if not row or pid <= 1:
            break
        rows.append(row)
        pid = row['parent']
    return rows



def root():
    """Mailboxes, receipts and session records. Never shared between machines."""
    base = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')
    return Path(os.environ.get('PEER_STATE') or Path(base) / 'peer')


def config():
    """hostname (optional) and hosts.json for other machines."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(os.environ.get('PEER_CONFIG') or Path(base) / 'peer')


def launcher(kind):
    """Command that starts an engine, from launch.json: {"claude": ["claude"], "codex": ["codex"]}.

    Point it at your own wrapper if you have one; peer passes your arguments through unchanged."""
    value = read(config() / 'launch.json').get(kind) or [kind]
    if not isinstance(value, list) or not value or not all(isinstance(a, str) for a in value):
        raise ValueError('launch.json: "%s" must be a nonempty list of arguments' % kind)
    return value


def host():
    """This machine's name as other machines' hosts.json call it."""
    try:
        return (config() / 'hostname').read_text().strip()
    except OSError:
        return socket.gethostname().split('.')[0]


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def path(kind, ident):
    return root() / kind / (key(ident) + '.json')


def read(file):
    try:
        return json.loads(file.read_text())
    except FileNotFoundError:
        return {}


def write(file, data):
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=file.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temp, file)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def lock(file, timeout=2):
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file.with_suffix('.lock').open('a') as stream:
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError('peer state busy; try again')
                time.sleep(.002)
        yield


def alive(row):
    recorded = row.get('process') or {}
    actual = process(recorded.get('pid', 0)) if recorded else None
    return bool(actual and actual['start'] == recorded.get('start'))


def target():
    return os.environ.get('TMUX_PANE', '')


def session_key(kind, sid):
    if kind not in ('claude', 'codex') or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', sid):
        raise ValueError('invalid engine/session identity')
    return kind + ':' + sid


def valid_name(name):
    if not re.fullmatch(r'[\w][\w.-]{0,63}', name, re.UNICODE):
        raise ValueError('use a name of 1–64 letters, digits, dots, hyphens or underscores')
    return name


def session(ident):
    return read(path('sessions', ident))


def sessions():
    return [read(p) for p in sorted((root() / 'sessions').glob('*.json'))]


def claude_processes():
    """Find running sessions from Claude's per-process files, including resumed sessions."""
    found = {}
    folder = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / 'sessions'
    for file in folder.glob('*.json'):
        try:
            info = read(file)
        except (OSError, ValueError):
            continue  # the engine may be replacing a file while we read it
        if not isinstance(info, dict):
            continue
        sid = info.get('sessionId')
        pid = info.get('pid') or (int(file.stem) if file.stem.isdigit() else None)
        if not isinstance(sid, str) or not isinstance(pid, int) or pid <= 0:
            continue
        proc = process(pid)
        if proc and (proc['name'] == 'claude' or re.fullmatch(r'\d+\.\d+\.\d+', proc['name'])):
            found[sid] = (proc, info.get('status'))
    return found


def revive(row, live=None):
    """Refresh an already registered Claude session after resume, before its next hook."""
    if row.get('kind') != 'claude' or alive(row):
        return row
    live = claude_processes() if live is None else live
    hit = live.get(row.get('session'))
    if not hit:
        return row
    proc, status = hit
    file = path('sessions', row['id'])
    with lock(file, .05):
        current = read(file)
        if not current or alive(current):
            return current or row  # never recreate a record removed during discovery
        if not alive({'process': proc}):
            return current  # the discovered process ended or its PID was reused
        # Only known runnable statuses allow delivery; a newer engine can add states.
        state = {'busy': 'working', 'idle': 'idle'}.get(status, 'unknown')
        now = time.time()
        current.update(process=proc, state=state, reason='engine-file', since=now, observed=now)
        write(file, current)
        return current


def public(row):
    fields = ('id', 'session', 'name', 'kind', 'host', 'cwd', 'title', 'target', 'state', 'reason', 'since', 'observed', 'tmux_socket')
    out = {k: row.get(k) for k in fields}
    if not alive(row):
        out.update(state='gone', reason='process-ended')
    waiting = read(path('waiting', row['id']))
    if out['state'] not in ('blocked', 'gone', 'error') and waiting.get('until', 0) > time.time() and alive(waiting):
        out.update(state='waiting-on', waiting_on=waiting['name'], since=waiting['since'])
    return out


def me(required=True):
    chain = ancestry()
    matches = [row for row in sessions() if row.get('process') in chain and alive(row)]
    # The nearest harness wins if an agent runs another agent as a tool.
    if matches:
        return min(matches, key=lambda row: chain.index(row['process']))
    if required:
        raise ValueError('this session has no peer identity yet; install peer-hook or launch with peer spawn')
    return None


def pane_label():
    """`<tmux session>-<pane label>` when the pane has a label, e.g. `team-review`; else None.

    The label is the pane option @peer (`tmux set -p @peer review`), or @name as smux sets it.
    Folder names alone read like `src-claude`, which nobody can place in a room of ten agents."""
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        return None
    try:
        out = subprocess.run(['tmux', 'display-message', '-p', '-t', pane,
                              '#{session_name}\t#{?@peer,#{@peer},#{@name}}'],
                             capture_output=True, text=True, timeout=.5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    session, _, label = out.partition('\t')
    if not label:
        return None
    return re.sub(r'[^\w.-]+', '-', session + '-' + label).strip('-')[:64] or None


def permission_mode(pid):
    """How the agent at pid runs, attested to Claude's inbox: 'bypass' without prompts, else 'default'.

    Claude uses it to keep a prompting session from driving a bypass one, so it is read from the
    real command line, never chosen to make delivery pass."""
    try:
        args = subprocess.run(['ps', '-o', 'args=', '-p', str(pid)], capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.SubprocessError):
        return 'default'
    flags = ('--dangerously-skip-permissions', 'danger-full-access', '--dangerously-bypass-approvals')
    return 'bypass' if any(f in args for f in flags) else 'default'


def claim_name(ident, preferred, explicit=False, rename=False):
    file = root() / 'names.json'
    with lock(file, .02):
        names = read(file)
        previous = next((n for n, owner in names.items() if owner == ident), None)
        if previous and not explicit and not rename:
            return previous
        if previous == preferred:
            return previous
        preferred = valid_name(preferred)
        chosen = preferred
        n = 2
        while chosen in names and names[chosen] != ident:
            if explicit:
                raise ValueError('name already belongs to another session: ' + preferred)
            chosen = preferred[:55] + '-' + str(n)
            n += 1
        if previous:
            del names[previous]
        names[chosen] = ident
        write(file, names)
        return chosen


def event_state(event):
    kind = event.get('hook_event_name')
    if kind == 'PermissionRequest':
        return 'blocked', 'permission'
    if kind == 'PreToolUse' and event.get('tool_name', '').split('.')[-1] in ('AskUserQuestion', 'request_user_input'):
        return 'blocked', 'question'
    if kind == 'Notification':
        if event.get('notification_type') in ('permission_prompt', 'elicitation_dialog'):
            return 'blocked', 'permission-or-question'
        return None
    return {'SessionStart': ('idle', 'session-start'), 'UserPromptSubmit': ('working', 'prompt'),
            'PreToolUse': ('working', 'tool'), 'PostToolUse': ('working', 'tool'),
            'Stop': ('idle', 'turn-ended'), 'Interrupt': ('idle', 'interrupted'),
            'StopFailure': ('error', 'turn-failed'), 'SessionEnd': ('gone', 'session-ended')}.get(kind)


def observe(event, kind):
    state = event_state(event)
    sid = event.get('session_id')
    if not sid or event.get('agent_id') or not state:
        return None
    chain = ancestry()
    agent = next((p for p in chain if p['name'] in ('claude', 'codex') or
                  (kind == 'claude' and re.fullmatch(r'\d+\.\d+\.\d+', p['name']))), None)
    if not agent:
        return None
    ident = session_key(kind, sid)
    file = path('sessions', ident)
    with lock(file, .02):
        old = read(file)
        cwd = event.get('cwd') or os.getcwd()
        base = re.sub(r'[^\w.-]+', '-', Path(cwd).name).strip('-') or 'agent'
        name = old.get('name')
        # One look at the pane label per session; a name someone chose (peer name, PEER_NAME) stays.
        if not name or not (old.get('named') or old.get('label_checked')):
            label = None if os.environ.get('PEER_NAME') else pane_label()
            preferred = os.environ.get('PEER_NAME') or label or base[:45] + '-' + kind
            if not name or label:
                name = claim_name(ident, preferred, rename=bool(name))
        now = time.time()
        row = dict(id=ident, session=sid, kind=kind, name=name, host=host(), cwd=cwd,
                   title=old.get('title'), target=target(), process=agent, ancestors=chain,
                   state=state[0], reason=state[1], since=old.get('since', now) if old.get('state') == state[0] else now,
                   observed=now, tmux_socket=os.environ.get('TMUX', '').split(',')[0],
                   codex_home=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')),
                   codex_socket=os.environ.get('PEER_CODEX_SOCKET'), transcript=event.get('transcript_path'),
                   named=bool(old.get('named') or os.environ.get('PEER_NAME')), label_checked=True)
        write(file, row)
    return row


def receipt(ident):
    return read(path('receipts', ident))


def update_receipt(ident, **fields):
    file = path('receipts', ident)
    with lock(file, .02):
        row = read(file)
        if not row:
            return {}
        # A prompt hook can beat a transport ACK. Never regress delivered evidence.
        if row.get('stage') in ('delivered', 'replied') and fields.get('stage') not in ('delivered', 'replied'):
            fields.pop('stage', None)
        row.update(fields)
        row['updated'] = time.time()
        write(file, row)
        return row


def mail_path(recipient, ident):
    return root() / 'mail' / key(recipient) / (ident + '.json')


def inbox(ident):
    return sorted((root() / 'mail' / key(ident)).glob('*.json'), key=lambda p: p.stat().st_mtime)


def envelope(mail, labelled=True):
    """The text a recipient sees. Routes where the engine names the sender itself (Claude's inbox shows
    "Message from @NAME@HOST") pass labelled=False; a second [from ...] there only repeated the name."""
    label = '[from ' + mail['sender']['name'] + '@' + mail['sender']['host'] + '] ' if labelled else ''
    return label + mail['body'] + '\n[peer-id:' + mail['id'] + ']'


def mark_delivered(mail, route, evidence):
    update_receipt(mail['id'], stage='delivered', route=route, evidence=evidence, delivered=time.time(), reason=None)
    mail_path(mail['recipient'], mail['id']).unlink(missing_ok=True)


def prompt_receipts(row, event):
    prompt = event.get('prompt', '')
    if not isinstance(prompt, str):
        return
    for ident in set(re.findall(r'\[peer-id:([0-9a-f]{24})\]', prompt)):
        rec = receipt(ident)
        if rec.get('recipient') == row['id']:
            file = mail_path(row['id'], ident)
            mail = read(file)
            if mail:
                mark_delivered(mail, rec.get('route'), 'UserPromptSubmit')


def hook_deliver(row, event):
    """Claude tool boundaries consume only unclaimed mail; no network/process work."""
    if row['kind'] != 'claude' or event.get('hook_event_name') not in ('PostToolUse', 'Stop'):
        return None
    accepted = []
    deadline = time.monotonic() + .04
    # Bounded batch keeps hooks short. Keep already assembled context even if
    # another sender briefly owns a later message's lock.
    for file in inbox(row['id'])[:8]:
        if time.monotonic() >= deadline:
            break
        try:
            mail = read(file)
            if not mail:
                continue
            with lock(path('delivery', mail['id']), .002):
                rec = receipt(mail['id'])
                if rec.get('stage') not in ('queued', 'held'):
                    continue
                text = envelope(mail)
                mark_delivered(mail, 'claude-hook', event['hook_event_name'])
                accepted.append(text)
        except (OSError, TimeoutError):
            continue
    if not accepted:
        return None
    text = '\n\n'.join(accepted)
    if event['hook_event_name'] == 'Stop':
        return {'decision': 'block', 'reason': text}
    return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': text}}

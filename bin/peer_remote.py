"""Commands and files between machines when there is no ssh into the target (a laptop with Remote Login off).

Rides the same transport peer messages use, including the authenticated relay tunnels. A relay request must
finish within 25 s and travels as one string, so commands run detached on the target and are polled, and
files move in 64 KB pieces. Where ssh reaches the target directly, callers can use ssh/scp instead.

Target side: rpc ops exec_start / exec_poll / fget / fput (dispatched from `peer _rpc`). They give nothing
beyond what the transport already allows: ssh is a shell, and relay requests are signed with the owners' key.
Caller side: run(host, argv, cwd), get(host, src, dst) and put(host, src, dst), used by sm's `run` and `cp`.
"""
import base64
import json
import os
from pathlib import Path
import pwd
import secrets
import shlex
import subprocess
import sys
import time

import peer_core

CHUNK = 64 * 1024


def jobs_dir():
    return peer_core.root() / 'jobs'


# ---------- target side ----------

def exec_start(request):
    argv = request.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError('argv must be a nonempty list of strings')
    cwd = os.path.expanduser(request.get('cwd') or '~')
    argv = [os.path.expanduser(a) if a.startswith('~') else a for a in argv]  # '~/…' means the target's home
    job = secrets.token_hex(6)
    folder = jobs_dir() / job
    folder.mkdir(parents=True)
    env = dict(os.environ)
    for k in ('TMUX', 'TMUX_PANE', 'PEER_HOLD_SOCK', 'PEER_NAME', 'SM_PANE_ID', 'FINE_TAB_ID'):
        env.pop(k, None)
    # A login shell gives the target's own PATH (wrappers, brew, version managers); the command itself is argv-quoted.
    # The user's login shell, not $SHELL: a relay daemon started by launchd or systemd has none. The wrapper is
    # POSIX shell, so a fish or nu user gets /bin/sh's login profile instead.
    wrapper = '( ' + 'cd ' + shlex.quote(cwd) + ' && ' + shlex.join(argv) + ' ) >"$1/out" 2>"$1/err"; echo $? >"$1/code"'
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell or '/bin/sh'
    except KeyError:
        shell = '/bin/sh'
    if Path(shell).name not in ('sh', 'bash', 'zsh', 'dash', 'ksh'):
        shell = '/bin/sh'
    subprocess.Popen([shell, '-lc', wrapper, 'peer-run', str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, env=env, cwd=os.path.expanduser('~'))
    return dict(job=job)


def exec_poll(request):
    job = request.get('job', '')
    if not job.isalnum():
        raise ValueError('bad job id')
    folder = jobs_dir() / job
    if not folder.is_dir():
        raise ValueError('no such job (finished long ago?)')
    out = {}
    for stream in ('out', 'err'):
        offset = int(request.get(stream, 0))
        try:
            with open(folder / stream, 'rb') as f:
                f.seek(offset)
                data = f.read(CHUNK)
        except FileNotFoundError:
            data = b''
        out[stream] = base64.b64encode(data).decode()
    code = (folder / 'code').read_text().strip() if (folder / 'code').exists() else None
    out['code'] = int(code) if code not in (None, '') else None
    if out['code'] is not None and not out['out'] and not out['err']:
        for f in folder.iterdir():
            f.unlink()
        folder.rmdir()  # read to the end: nothing left to keep
    return out


def fget(request):
    path = Path(os.path.expanduser(request['path']))
    offset = int(request.get('offset', 0))
    st = path.stat()
    with open(path, 'rb') as f:
        f.seek(offset)
        data = f.read(CHUNK)
    return dict(data=base64.b64encode(data).decode(), size=st.st_size, mode=st.st_mode & 0o777)


def fput(request):
    path = Path(os.path.expanduser(request['path']))
    part = path.with_name('.' + path.name + '.part-' + request['token'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(part, 'r+b' if request.get('offset', 0) else 'wb') as f:
        f.seek(int(request.get('offset', 0)))
        f.write(base64.b64decode(request.get('data', '')))
    if request.get('final'):
        os.chmod(part, int(request.get('mode', 0o644)))
        os.replace(part, path)
    return dict(ok=True)


OPS = dict(exec_start=exec_start, exec_poll=exec_poll, fget=fget, fput=fput)


# ---------- caller side ----------

def call(host, request):
    from peer_transport import remote
    return remote(host, request)


def run(host, argv, cwd=None, out=None, err=None):
    """Run argv on host and stream its output here; returns its exit code."""
    out = out or sys.stdout.buffer
    err = err or sys.stderr.buffer
    job = call(host, dict(op='exec_start', argv=argv, cwd=cwd))['job']
    got = {'out': 0, 'err': 0}
    wait = .1
    while True:
        r = call(host, dict(op='exec_poll', job=job, out=got['out'], err=got['err']))
        moved = False
        for stream, sink in (('out', out), ('err', err)):
            data = base64.b64decode(r[stream])
            if data:
                sink.write(data); sink.flush()
                got[stream] += len(data)
                moved = True
        if r['code'] is not None and not moved:
            return r['code']
        if not moved:
            time.sleep(wait)
            wait = min(wait * 1.5, 1.0)
        else:
            wait = .1


def get(host, src, dst):
    token, offset, size, mode = secrets.token_hex(4), 0, None, 0o644
    dst = Path(dst)
    part = dst.with_name('.' + dst.name + '.part-' + token)
    with open(part, 'wb') as f:
        while size is None or offset < size:
            r = call(host, dict(op='fget', path=src, offset=offset))
            data = base64.b64decode(r['data'])
            size, mode = r['size'], r['mode']
            if not data:
                break
            f.write(data)
            offset += len(data)
    os.chmod(part, mode)
    os.replace(part, dst)
    return offset


def put(host, src, dst):
    token, offset = secrets.token_hex(4), 0
    src = Path(src)
    mode = src.stat().st_mode & 0o777
    with open(src, 'rb') as f:
        while True:
            data = f.read(CHUNK)
            final = len(data) < CHUNK
            call(host, dict(op='fput', path=dst, offset=offset, data=base64.b64encode(data).decode(), token=token,
                            final=final, mode=mode))
            offset += len(data)
            if final:
                return offset

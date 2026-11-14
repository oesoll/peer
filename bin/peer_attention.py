"""`peer notify`: ask for the person's attention through an app that shows it, on this machine or another.

The request goes to the first app here that answers `attention.post` on its control socket: sm's Mac app
(~/.space-manager/control.sock, or SM_CONTROL_SOCK), then Fine (~/.fine/control.sock). Without either it
fails and says so. Nothing is typed into a terminal."""
import json
import os
from pathlib import Path
import socket


def app_rpc(path, post):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall(json.dumps({'command': 'attention.post', 'args': post}).encode() + b'\n')
        with sock.makefile('rb') as stream:
            response = json.loads(stream.readline(8 * 1024 * 1024 + 1))
    if not response.get('ok'):
        raise ValueError(response.get('error', 'attention request failed'))
    return response.get('result')


def post(request, source_host):
    state = request.get('state', 'waiting')
    if state not in ('working', 'waiting', 'blocked', 'done', 'error', 'unknown'):
        raise ValueError('invalid attention state')
    fields = {}
    for key, limit in [('source', 100), ('key', 200), ('title', 100), ('message', 1800), ('workspace', 100)]:
        value = request.get(key, '')
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError('invalid attention ' + key)
        fields[key] = value
    fields.update(host=source_host, state=state, fingerprint='')
    # A test that isolates Fine (FINE_HOME) must not reach the person's real sm app unless it names a socket.
    paths = []
    if not os.environ.get('FINE_HOME') or os.environ.get('SM_CONTROL_SOCK'):
        paths.append(Path(os.environ.get('SM_CONTROL_SOCK', str(Path.home() / '.space-manager/control.sock'))))
    paths.append(Path(os.environ.get('FINE_HOME', str(Path.home() / '.fine'))) / 'control.sock')
    errors = []
    for path in paths:
        try:
            return app_rpc(path, fields)
        except (OSError, ValueError) as error:
            errors.append(str(error))
    raise ValueError("no app takes attention on this machine (open sm's app or Fine there): " + '; '.join(errors))

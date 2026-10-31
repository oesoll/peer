"""Codex app-server client and the ssh transport between machines."""
import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time
from peer_core import config, host, root, read

BIN = Path(__file__).resolve().parent


class RPCError(Exception):
    pass


class CodexClient:
    def __init__(self, endpoint):
        # Use the maintained WebSocket implementation; Codex Unix sockets speak
        # HTTP Upgrade + WebSocket frames, not raw JSONL (proxy is byte-transparent).
        try:
            import websocket
        except ImportError:
            raise RPCError('install websocket-client for Codex app-server delivery')
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(2)
        try:
            sock.connect(endpoint)
            self.ws = websocket.create_connection('ws://localhost/', socket=sock, timeout=2)
        except Exception:
            sock.close()
            raise
        self.seq = 0
        self.call('initialize', {'clientInfo': {'name': 'peer', 'title': 'Peer', 'version': '0.1'},
                                 'capabilities': {'experimentalApi': True}})
        self.ws.send(json.dumps({'method': 'initialized'}))

    def call(self, method, params):
        self.seq += 1
        self.ws.send(json.dumps({'id': self.seq, 'method': method, 'params': params}))
        end = time.monotonic() + 2
        while time.monotonic() < end:
            value = json.loads(self.ws.recv())
            if value.get('id') != self.seq:
                continue  # Never answer another client's permission requests.
            if 'error' in value:
                raise RPCError(value['error'].get('message', 'app-server refused request'))
            return value['result']
        raise TimeoutError('app-server response timed out')

    def close(self):
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def hosts():
    """Other machines from hosts.json: {"server": {"ssh": "me@server", "peer": "~/.local/bin/peer"}}.

    "peer" is the command on that machine; non-interactive ssh often lacks ~/.local/bin in PATH."""
    result = {}
    for name, entry in read(config() / 'hosts.json').items():
        if name == host():
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get('ssh'), str) or not entry['ssh'].strip():
            raise ValueError('hosts.json: "%s" needs {"ssh": "<destination>"}' % name)
        result[name] = entry
    return result


def remote(owner, request):
    """Run one peer operation on another machine over ssh.

    The other side trusts the origin name we send: ssh already proved we are the same user there,
    and that user could run anything on that machine anyway."""
    entry = hosts().get(owner)
    if not entry:
        raise ValueError('unknown peer host: %s (add it to %s)' % (owner, config() / 'hosts.json'))
    command = '%s _rpc %s %s' % (entry.get('peer', '~/.local/bin/peer'), shlex.quote(host()), shlex.quote(json.dumps(request)))
    result = subprocess.run(['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', entry['ssh'], command],
                            capture_output=True, timeout=8)
    if result.returncode:
        raise ValueError(owner + ': ' + result.stderr.decode(errors='replace').strip()[:500])
    return json.loads(result.stdout)


def inventory(local):
    rows = list(local)
    errors = []
    destinations = hosts()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(destinations), 8))) as pool:
        futures = {pool.submit(remote, owner, {'op': 'list'}): owner for owner in destinations}
        for future in concurrent.futures.as_completed(futures):
            owner = futures[future]
            try:
                rows.extend(future.result()['peers'])
            except Exception as error:
                errors.append(str(error))
    return rows, errors

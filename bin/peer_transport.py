"""Codex app-server client and the transport between machines: ssh, or an authenticated relay tunnel."""
import base64
import concurrent.futures
import hashlib
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


class WebSocket:
    """Just enough RFC 6455 for one local app-server on a Unix socket: the client handshake, masked
    text frames out, text frames in (fragments joined), pings answered. Codex app-server sockets speak
    HTTP Upgrade + WebSocket frames, not raw JSONL. Written here so Codex delivery needs nothing
    beyond Python; a pip step was where new installs stalled (and PEP 668 Pythons refuse --user)."""
    GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

    def __init__(self, sock):
        self.sock = sock
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                      'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % key).encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError('app-server closed the connection during the WebSocket handshake')
            head += chunk
            if len(head) > 65536:
                raise ConnectionError('oversized app-server WebSocket handshake')
        head, self.buffer = head.split(b'\r\n\r\n', 1)
        status = head.split(b'\r\n', 1)[0].split()
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest())
        if len(status) < 2 or status[1] != b'101' or accept not in head:
            raise ConnectionError('app-server refused the WebSocket upgrade: ' + b' '.join(status).decode(errors='replace'))

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buffer)))
            if not chunk:
                raise ConnectionError('app-server closed the connection')
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _frame(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 1 << 16:
            head = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, 'big')
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, 'big')
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send(self, text):
        self._frame(0x1, text.encode())

    def recv(self):
        parts = []
        while True:
            first, second = self._read(2)
            opcode, n = first & 0x0F, second & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), 'big')
            elif n == 127:
                n = int.from_bytes(self._read(8), 'big')
            if n > 64 * 1024 * 1024:
                raise ConnectionError('app-server WebSocket frame exceeds 64 MiB')
            mask = self._read(4) if second & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if opcode == 0x9:
                self._frame(0xA, data)
            elif opcode == 0x8:
                raise ConnectionError('app-server closed the connection')
            elif opcode in (0x0, 0x1, 0x2):
                parts.append(data)
                if first & 0x80:
                    return b''.join(parts).decode()

    def close(self):
        try:
            self._frame(0x8, b'')
        except OSError:
            pass
        self.sock.close()


class CodexClient:
    def __init__(self, endpoint):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(2)
        try:
            sock.connect(endpoint)
            self.ws = WebSocket(sock)
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
    """Other machines from hosts.json, each reached one of two ways:

        {"server": {"ssh": "me@server", "peer": "~/.local/bin/peer"},
         "laptop": {"relay": "home"}}

    "ssh" runs peer there over ssh; "peer" is the command on that machine (non-interactive ssh often lacks
    ~/.local/bin in PATH). "relay" names a hub from relay.json, for a machine that takes no ssh logins:
    it keeps a tunnel open to the hub itself (`peer relay`)."""
    result = {}
    for name, entry in read(config() / 'hosts.json').items():
        if name == host():
            continue
        ssh, relay = (entry.get(k) if isinstance(entry, dict) else None for k in ('ssh', 'relay'))
        if not any(isinstance(v, str) and v.strip() for v in (ssh, relay)):
            raise ValueError('hosts.json: "%s" needs {"ssh": "<destination>"} or {"relay": "<hub>"}' % name)
        result[name] = entry
    return result


def remote(owner, request):
    """Run one peer operation on another machine.

    The other side trusts the origin name we send: ssh already proved we are the same user there, and that
    user could run anything on that machine anyway. A relay request carries a signature made with relay.key,
    which only the user's own machines hold. The request travels on stdin: one argument is capped at 128 KB
    on Linux, which a long message in a non-Latin script passes once escaped.

    A machine paired on this network (peer pair) is used only when hosts.json does not name it: a paired
    machine answers messages only, while ssh and relay also carry sm run and sm cp."""
    entry = hosts().get(owner)
    if not entry:
        from peer_lan import call, paired
        if owner in paired():
            return call(owner, request)  # TLS against the certificate saved at pairing, no ssh
        raise ValueError('unknown peer host: %s (pair with it: peer pair, or add it to %s)' % (owner, config() / 'hosts.json'))
    body = json.dumps(request)
    if entry.get('relay'):
        import peer_relay
        result = peer_relay.call(entry['relay'], owner, ['_rpc', host(), body])
        if result['code']:
            raise ValueError(owner + ': ' + result['err'].strip()[:500])
        return json.loads(result['out'])
    command = '%s _rpc %s' % (entry.get('peer', '~/.local/bin/peer'), shlex.quote(host()))
    result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', entry['ssh'], command],
                            input=body.encode(), capture_output=True, timeout=8)
    if result.returncode:
        raise ValueError(owner + ': ' + result.stderr.decode(errors='replace').strip()[:500])
    return json.loads(result.stdout)


def inventory(local):
    rows = list(local)
    errors = []
    from peer_lan import paired
    destinations = list(dict.fromkeys([*hosts(), *(n for n in paired() if n != host())]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(destinations), 8))) as pool:
        futures = {pool.submit(remote, owner, {'op': 'list'}): owner for owner in destinations}
        for future in concurrent.futures.as_completed(futures):
            owner = futures[future]
            try:
                rows.extend(future.result()['peers'])
            except Exception as error:
                errors.append(str(error))
    return rows, errors

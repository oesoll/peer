"""Two machines in one test: this process is "alpha", a child process is "beta" (peer reads its
config from the environment, so each side needs its own)."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import peer_lan  # noqa: E402

BETA = textwrap.dedent('''
    import json, sys, threading
    sys.path.insert(0, %r)
    import peer_lan
    answer = sys.argv[1] == 'yes'
    name = peer_lan.pair_listen(lambda name, number: (print('NUMBER', number, flush=True), answer)[1],
                                out=lambda text: print(text, flush=True), timeout=20)
    print('PAIRED', name, flush=True)
    if name:
        ready = threading.Event()
        threading.Thread(target=peer_lan.serve, args=(lambda request, source: dict(echo=request, source=source),),
                         kwargs=dict(ready=ready), daemon=True).start()
        ready.wait(5)
        print('SERVING', flush=True)
        sys.stdin.read()
''') % str(BIN)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class LanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        for side in ('alpha', 'beta', 'gamma'):
            (root / side).mkdir()
            (root / side / 'hostname').write_text(side)
        self.root = root
        self.beta_port = free_port()
        env = patch.dict(os.environ, PEER_CONFIG=str(root / 'alpha'), PEER_LAN_NO_MDNS='1')
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.temp.cleanup)

    def beta(self, answer='yes'):
        env = dict(os.environ, PEER_CONFIG=str(self.root / 'beta'), PEER_LAN_PORT=str(self.beta_port))
        process = subprocess.Popen([sys.executable, '-c', BETA, answer], env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        line = process.stdout.readline()
        port = int(line.rsplit(':', 1)[1].split()[0].rstrip(')'))
        return process, port

    def test_pair_then_talk_and_strangers_get_nothing(self):
        process, port = self.beta()
        seen = []
        name = peer_lan.pair_join('127.0.0.1', port, lambda name, number: seen.append(number) or True,
                                  out=lambda text: None, fixed=True)
        self.assertEqual(name, 'beta')
        lines = [process.stdout.readline() for _ in range(4)]
        self.assertIn('NUMBER ' + seen[0], ''.join(lines), 'both screens show the same number')
        self.assertIn('PAIRED alpha', ''.join(lines))
        self.assertIn('SERVING', process.stdout.readline())
        self.assertEqual(peer_lan.paired()['beta']['address'], '127.0.0.1:%d' % self.beta_port)

        reply = peer_lan.call('beta', {'op': 'list'})
        self.assertEqual(reply, {'echo': {'op': 'list'}, 'source': 'alpha'}, 'the source is who the certificate says')

        # A machine beta never paired with: its handshake is refused and nothing runs.
        with patch.dict(os.environ, PEER_CONFIG=str(self.root / 'gamma')):
            beta_cert = json.loads((self.root / 'alpha/lan/paired.json').read_text())['beta']['cert']
            peer_lan.remember('beta', cert=beta_cert, address='127.0.0.1:%d' % self.beta_port, fixed=True)
            with self.assertRaises(ValueError):
                peer_lan.call('beta', {'op': 'list'})

    def test_paired_machine_gets_messages_only(self):
        process, port = self.beta()
        name = peer_lan.pair_join('127.0.0.1', port, lambda name, number: True, out=lambda text: None, fixed=True)
        self.assertEqual(name, 'beta')
        for _ in range(10):  # the pairing lines, then SERVING
            if 'SERVING' in process.stdout.readline():
                break
        for op in sorted(peer_lan.MESSAGE_OPS):
            self.assertEqual(peer_lan.call('beta', {'op': op})['echo'], {'op': op})
        # Commands, files, starting agents, and anything new: refused before beta's rpc sees them.
        for request in ({'op': 'exec_start', 'argv': ['true']}, {'op': 'exec_poll', 'job': 'x'},
                        {'op': 'fget', 'path': '/etc/hosts'}, {'op': 'fput', 'path': '/tmp/x', 'data': ''},
                        {'op': 'spawn', 'name': 'x'},
                        {'op': 'not-yet-invented'}, {}, {'op': ['list']}, ['list']):
            with self.assertRaisesRegex(ValueError, 'only messages'):
                peer_lan.call('beta', request)

    def test_hosts_json_wins_over_pairing(self):
        # ssh carries sm run and sm cp; a paired machine would refuse them, so a machine named in
        # hosts.json keeps going over ssh even when it is paired too.
        import peer_transport
        peer_lan.remember('beta', cert='', address='127.0.0.1:1', fixed=True)
        with patch.object(peer_lan, 'call', return_value='lan') as lan:
            self.assertEqual(peer_transport.remote('beta', {'op': 'list'}), 'lan')
            (self.root / 'alpha/hosts.json').write_text(json.dumps({'beta': {'ssh': 'me@beta'}}))
            done = subprocess.CompletedProcess([], 0, b'"ssh"', b'')
            with patch.object(peer_transport.subprocess, 'run', return_value=done) as run:
                self.assertEqual(peer_transport.remote('beta', {'op': 'exec_start'}), 'ssh')
            self.assertEqual(run.call_args[0][0][-2], 'me@beta')
            self.assertEqual(lan.call_count, 1)

    def test_no_on_either_side_saves_nothing(self):
        process, port = self.beta(answer='no')
        name = peer_lan.pair_join('127.0.0.1', port, lambda name, number: True, out=lambda text: None)
        self.assertIsNone(name)
        self.assertEqual(peer_lan.paired(), {})
        self.assertIn('PAIRED None', process.stdout.read())

    def test_numbers_differ_when_anything_differs(self):
        a = peer_lan.identity()[1].read_text()
        with patch.dict(os.environ, PEER_CONFIG=str(self.root / 'beta')):
            b = peer_lan.identity()[1].read_text()
        n1, n2 = '00' * 16, '11' * 16
        self.assertNotEqual(peer_lan.number(a, b, n1, n2), peer_lan.number(a, a, n1, n2))
        self.assertNotEqual(peer_lan.number(a, b, n1, n2), peer_lan.number(a, b, n1, '22' * 16))


if __name__ == '__main__':
    unittest.main()

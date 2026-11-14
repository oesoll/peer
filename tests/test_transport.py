"""Between machines (relay, remote commands and files) and the Codex launcher's resume handling."""
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import peer_codex
import peer_relay as relay
import peer_remote
from peer_transport import RPCError
loader = importlib.machinery.SourceFileLoader('peer_session_launcher', str(BIN / 'peer-session'))
spec = importlib.util.spec_from_loader(loader.name, loader)
session = importlib.util.module_from_spec(spec); loader.exec_module(session)


class RelayTests(unittest.TestCase):
    def test_relay_runs_only_peer_rpc_and_sends_the_body_on_stdin(self):
        body = json.dumps({'op': 'ping'})
        with patch.object(relay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')) as run:
            relay.execute({'version': 1, 'args': ['_rpc', 'client', body]})
        self.assertEqual(run.call_args.args[0], [sys.executable, str(BIN / 'peer'), '_rpc', 'client'])
        self.assertEqual(run.call_args.kwargs['input'], body.encode())
        for args in (['sh', '-c', 'echo bad'], ['_rpc', 'bad host', body], ['_rpc', 'client'],
                     ['--remote', 'a', 'b', 'c', 'd', 'e']):
            with self.assertRaises(ValueError):
                relay.execute({'version': 1, 'args': args})

    def test_install_retries_launchd_job_removal_race(self):
        with tempfile.TemporaryDirectory() as temp:
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                boot = [c for c in commands if 'bootstrap' in c]
                code = 5 if 'bootstrap' in command and len(boot) == 1 else 0
                return subprocess.CompletedProcess(command, code, '', 'Input/output error' if code else '')
            with patch.object(relay, 'hubs'), patch.object(relay, 'secret'), \
                    patch.object(relay, 'RUN', Path(temp) / 'run'), patch.object(Path, 'home', return_value=Path(temp)), \
                    patch.object(relay.sys, 'platform', 'darwin'), patch.object(relay.subprocess, 'run', side_effect=run), \
                    patch.object(relay.time, 'sleep'), patch('builtins.print'):
                relay.install()
            self.assertEqual(len([c for c in commands if 'bootstrap' in c]), 2)
            plist = Path(temp) / 'Library/LaunchAgents' / (relay.LABEL + '.plist')
            self.assertIn(b'peer-relay', plist.read_bytes())

    def test_hubs_come_from_relay_json_and_hosts_json(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = Path(temp)
            (conf / 'hostname').write_text('laptop')
            (conf / 'hosts.json').write_text(json.dumps({'server': {'ssh': 'server'}, 'desk': {'relay': 'home'},
                                                         'laptop': {'relay': 'home'}}))
            with patch.dict(os.environ, PEER_CONFIG=temp), patch.object(relay, 'CONFIG', conf):
                with self.assertRaisesRegex(ValueError, 'no relay configured'):
                    relay.hubs()
                (conf / 'relay.json').write_text(json.dumps({'name': 'home', 'ssh_alias': 'hub', 'remote_dir': '/srv/peer'}))
                self.assertEqual(relay.hubs(), [('laptop', 'home', 'hub', '/srv/peer', ['desk'])])

    def test_signed_socket_reaches_real_peer(self):
        with tempfile.TemporaryDirectory(prefix='peer-rpc-', dir='/tmp') as temp:
            root = Path(temp)
            conf = root / 'config'; conf.mkdir(); (conf / 'hostname').write_text('fixture')
            endpoint = root / 'socket'
            key = b'k' * 32
            server = relay.Server(endpoint, 'fixture', key)
            thread = threading.Thread(target=server.serve_forever); thread.start()
            try:
                with patch.dict(os.environ, PEER_CONFIG=str(conf), PEER_STATE=str(root / 'state'), PATH='/missing'):
                    result = relay.request(endpoint, 'fixture', ['_rpc', 'client', '{"op":"ping"}'], key)
                    self.assertEqual(result['code'], 0, result)
                    self.assertEqual(json.loads(result['out']), {'host': 'fixture', 'protocol': 1, 'transport': 'peer'})
                    with self.assertRaisesRegex(ValueError, 'failed authentication'):
                        relay.request(endpoint, 'fixture', ['_rpc', 'client', '{"op":"ping"}'], b'x' * 32)
            finally:
                server.shutdown(); thread.join(); server.server_close()


class RemoteTests(unittest.TestCase):
    """sm's `run` and `cp` toward a machine without ssh: the target side runs here, as `peer _rpc` would."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, PEER_STATE=str(Path(self.temp.name) / 'state')); self.env.start()
        self.call = patch.object(peer_remote, 'call', side_effect=lambda host, request: peer_remote.OPS[request['op']](request))
        self.call.start()

    def tearDown(self):
        self.call.stop(); self.env.stop(); self.temp.cleanup()

    def test_a_command_runs_there_and_streams_back(self):
        out, err = io.BytesIO(), io.BytesIO()
        code = peer_remote.run('there', ['sh', '-c', 'printf "out $1"; printf err >&2; exit 3', 'x', 'arg one'],
                               cwd=self.temp.name, out=out, err=err)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (3, b'out arg one', b'err'))
        self.assertEqual(list((Path(self.temp.name) / 'state/jobs').iterdir()), [])  # read to the end: cleaned up

    def test_files_move_in_pieces_and_keep_their_mode(self):
        src = Path(self.temp.name) / 'src.bin'
        src.write_bytes(os.urandom(peer_remote.CHUNK * 2 + 7)); src.chmod(0o750)
        there = Path(self.temp.name) / 'far/dst.bin'
        back = Path(self.temp.name) / 'back.bin'
        self.assertEqual(peer_remote.put('there', src, str(there)), src.stat().st_size)
        self.assertEqual(peer_remote.get('there', str(there), back), src.stat().st_size)
        self.assertEqual((there.read_bytes(), back.read_bytes()), (src.read_bytes(), src.read_bytes()))
        self.assertEqual(there.stat().st_mode & 0o777, 0o750)


class ResumeTests(unittest.TestCase):
    def test_only_an_explicit_unattended_resume_sets_the_policy(self):
        bypass = '--dangerously-bypass-approvals-and-sandbox'
        for args in (['-r', 'thread'], ['-y', 'new prompt'], ['--', '-ry', 'not-an-option'], ['resume', 'thread'],
                     [bypass, 'a new prompt'], ['-m', 'resume', bypass]):
            self.assertEqual(session.unattended_resume(args), (None, args), args)
        with self.assertRaisesRegex(ValueError, 'thread id'):
            session.unattended_resume(['-ry'])
        self.assertEqual(session.unattended_resume(['-ry', 'same-thread', '-m', 'model']),
                         ('same-thread', ['-ry', 'same-thread', '-m', 'model']))  # the wrapper reads -ry itself
        # Codex's own spelling: the override is set on the server and left out of the TUI's arguments
        self.assertEqual(session.unattended_resume(['-m', 'gpt', 'resume', bypass, 'abc']), ('abc', ['-m', 'gpt', 'resume', 'abc']))
        self.assertEqual(session.unattended_resume(['resume', '--last', bypass]), (None, ['resume', '--last', bypass]))

    def test_policy_is_set_on_that_thread_only(self):
        with patch.object(peer_codex, 'CodexClient') as client:
            peer_codex.prepare_resume('/fixture.sock', 'same-thread')
        client.return_value.__enter__.return_value.call.assert_called_once_with(
            'thread/resume', {'threadId': 'same-thread', 'approvalPolicy': 'never',
                              'sandbox': 'danger-full-access', 'excludeTurns': True})

    def test_policy_setup_failure_does_not_prevent_resume(self):
        for failure in (ConnectionError('frame exceeds 64 MiB'), TimeoutError('slow server'), RPCError('unsupported option')):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(peer_codex, 'CodexClient') as client, \
                    patch('sys.stderr', new_callable=io.StringIO) as stderr:
                client.return_value.__enter__.return_value.call.side_effect = failure
                peer_codex.prepare_resume('/fixture.sock', 'same-thread')
                self.assertIn('resuming anyway', stderr.getvalue())
                client.return_value.__exit__.assert_called_once()

    def test_omo_gets_its_identity_packed_into_one_variable(self):
        with patch.object(session.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                patch.object(session, 'launcher', return_value=['omo']), \
                patch.dict(os.environ, PEER_NAME='lane', PEER_STATE='/state', PEER_CONFIG='/config', PATH='/usr/bin'):
            self.assertEqual(session.run_omo(['--x']), 0)
        env = run.call_args.kwargs['env']
        identity = json.loads(env['OMO_PEER_IDENTITY'])
        self.assertEqual((identity['PEER_NAME'], identity['PEER_STATE'], identity['PEER_CONFIG'], identity['PEER_BIN']),
                         ('lane', '/state', '/config', str(BIN)))
        self.assertEqual(run.call_args.args[0], ['omo', '--x'])
        self.assertIn(str(BIN), env['PATH'].split(':'))
        self.assertEqual(env['PATH'].split(':')[0], '/usr/bin')  # appended, never ahead of the caller's PATH


if __name__ == '__main__':
    unittest.main()

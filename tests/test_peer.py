import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import peer_core as c
import peer_delivery as d
import peer_transport as t
import peer_setup as setup
loader = importlib.machinery.SourceFileLoader('peer_cli', str(BIN / 'peer'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec); loader.exec_module(cli)


class PeerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/hostname').write_text('test')
        self.env = patch.dict(os.environ, PEER_STATE=str(self.root / 'state'), PEER_CONFIG=str(self.root / 'config'), TMUX_PANE='%1')
        self.env.start()
        self.proc = c.process(os.getpid())
        self.agent = dict(self.proc, name='claude')
        self.chain = patch.object(c, 'ancestry', return_value=[self.agent]); self.chain.start()
        self.worker = patch.object(d, 'start_worker'); self.worker.start()

    def tearDown(self):
        self.worker.stop(); self.chain.stop(); self.env.stop(); self.temp.cleanup()

    def observe(self, event='SessionStart', sid='abc', **values):
        return c.observe(dict(hook_event_name=event, session_id=sid, cwd='/project/demo', **values), 'claude')

    def message(self, row, body='private payload', **values):
        request = dict(id=secrets.token_hex(12), recipient=row['id'], sender={'id':'codex:other', 'name':'other','host':'test'}, body=body, **values)
        return request, d.receive(request, 'test')

    def test_states_names_fork_and_resume(self):
        first = self.observe()
        self.assertEqual(first['name'], 'demo-claude')
        self.assertEqual(self.observe('PermissionRequest')['state'], 'blocked')
        self.assertEqual(self.observe('PostToolUse')['state'], 'working')
        self.assertEqual(self.observe('Stop')['state'], 'idle')
        self.assertEqual(self.observe('StopFailure')['state'], 'error')
        self.assertEqual(self.observe('SessionEnd')['state'], 'gone')
        self.assertEqual(self.observe()['name'], first['name'])
        self.assertEqual(self.observe(sid='fork')['name'], 'demo-claude-2')
        before = c.session(first['id'])
        self.assertIsNone(self.observe('Stop', agent_id='sub'))
        self.assertEqual(c.session(first['id']), before)

    def test_blocked_mail_held_then_hook_delivered_once(self):
        row = self.observe('PermissionRequest')
        req, rec = self.message(row)
        self.assertEqual(rec['stage'], 'held')
        self.assertEqual(d.receive(req, 'test'), rec)
        file = c.mail_path(row['id'], req['id'])
        d.attempt(file)
        self.assertEqual(c.receipt(req['id'])['stage'], 'held')
        row = self.observe('PostToolUse')
        out = c.hook_deliver(row, {'hook_event_name':'PostToolUse'})
        self.assertIn('private payload', out['hookSpecificOutput']['additionalContext'])
        self.assertFalse(file.exists())
        self.assertEqual(c.receipt(req['id'])['stage'], 'delivered')
        self.assertIsNone(c.hook_deliver(row, {'hook_event_name':'PostToolUse'}))
        for p in c.root().rglob('*.json'):
            self.assertNotIn('private payload', p.read_text())

    def test_stop_hook_and_handoff_race(self):
        row = self.observe('Stop')
        req, _ = self.message(row)
        out = c.hook_deliver(row, {'hook_event_name':'Stop'})
        self.assertEqual(out['decision'], 'block')
        c.update_receipt(req['id'], stage='accepted')
        self.assertEqual(c.receipt(req['id'])['stage'], 'delivered')

    def test_stale_process_cannot_receive(self):
        row = self.observe()
        row['process']['start'] = ['not-the-start-time']
        c.write(c.path('sessions', row['id']), row)
        self.assertEqual(c.public(row)['state'], 'gone')
        req, rec = self.message(row)
        self.assertEqual(rec['stage'], 'held')

    def test_prompt_id_cannot_ack_another_recipient(self):
        row = self.observe()
        other = self.observe(sid='other')
        req, rec = self.message(row)
        event = {'prompt':'[peer-id:'+req['id']+']'}
        c.prompt_receipts(other, event)
        self.assertEqual(c.receipt(req['id'])['stage'], 'queued')
        c.prompt_receipts(row, event)
        self.assertEqual(c.receipt(req['id'])['stage'], 'delivered')

    def test_ask_waiting_and_reply_body_lifetime(self):
        row = self.observe()
        c.write(c.path('waiting', row['id']), dict(message='ask-id', peer_id='codex:other', peer_host='test', name='other',
                                                 since=time.time(), until=time.time()+30, process=self.proc))
        self.assertEqual(c.public(row)['state'], 'waiting-on')
        req, rec = self.message(row, 'answer only')
        self.assertEqual(rec['route'], 'ask-return')
        self.assertFalse(c.mail_path(row['id'], req['id']).exists())
        self.assertEqual(c.read(c.path('answers', 'ask-id'))['body'], 'answer only')
        self.assertNotIn('answer only', json.dumps(c.receipt(req['id'])))
        row = self.observe('PermissionRequest')
        self.assertEqual(c.public(row)['state'], 'blocked')

    def test_native_error_never_falls_back_to_terminal(self):
        row = self.observe()
        row.update(kind='codex', codex_socket='/missing', state='idle')
        c.write(c.path('sessions', row['id']), row)
        req, _ = self.message(row)
        with patch.object(d, 'CodexClient', side_effect=OSError('no socket')), patch.object(d.subprocess, 'run') as run:
            d.attempt(c.mail_path(row['id'], req['id']))
        self.assertFalse(run.called)
        self.assertEqual(c.receipt(req['id'])['stage'], 'held')

    def test_host_spoof_refused(self):
        row = self.observe()
        with self.assertRaisesRegex(ValueError, 'authenticated transport'):
            d.receive(dict(id=secrets.token_hex(12), sender={'host':'fake'}, recipient=row['id'], body='x'), 'test')

    def test_nearby_grouping_and_collapsed_list(self):
        own = self.observe()
        own_public = c.public(own)
        own_public.update(group='main', window=0, pane=0)
        rows = [own_public]
        for i in range(30):
            rows.append(dict(own_public, id='claude:'+str(i), name='peer-'+str(i), group='far', cwd='/elsewhere/'+str(i)))
        with patch.object(cli, 'all_peers', return_value=(rows, [])), patch.object(cli, 'me', return_value=own):
            result = cli.list_peers(as_json=True)
            self.assertEqual(len(result['peers']), 1)
            self.assertEqual(result['hidden'], 30)
            found = cli.list_peers('peer-25', as_json=True)
            self.assertEqual(found['peers'][0]['name'], 'peer-25')

    def test_hook_latency(self):
        start = time.perf_counter()
        for _ in range(30):
            self.observe('PostToolUse')
        self.assertLess((time.perf_counter()-start)/30, .1)

    def test_codex_watch_names_only_the_tui_thread(self):
        import peer_codex
        import threading
        stopped = threading.Event()
        threads = {'main': {'id': 'main', 'cwd': '/w', 'status': {'type': 'idle'}, 'ephemeral': False},
                   'helper': {'id': 'helper', 'cwd': '/w', 'status': {'type': 'idle'}, 'ephemeral': True},
                   'child': {'id': 'child', 'cwd': '/w', 'status': {'type': 'idle'}, 'parentThreadId': 'main'}}

        class Fake:
            listed = 0
            def __init__(self, endpoint): pass
            def __enter__(self): return self
            def __exit__(self, *a): stopped.set()
            def call(self, method, params):
                if method == 'thread/loaded/list':
                    Fake.listed += 1
                    if Fake.listed > 1:  # one full observation round, then stop the watcher
                        stopped.set()
                        return {'data': []}
                    return {'data': list(threads)}
                return {'thread': threads[params['threadId']]}

        class Server:
            pid = os.getpid()
            def poll(self): return None

        with patch.object(peer_codex, 'CodexClient', Fake), patch.dict(os.environ, PEER_NAME='cx1'):
            peer_codex.watch(Server(), '/tmp/x.sock', stopped)
        names = {r['name']: r['session'] for r in c.sessions()}
        self.assertEqual(names, {'cx1': 'main'})


    def test_pane_label_names_and_chosen_names_stay(self):
        with patch.object(c, 'pane_label', return_value=None):
            self.assertEqual(self.observe(sid='old')['name'], 'demo-claude')  # registered before labels counted
        c.write(c.path('sessions', 'claude:old'), dict(c.session('claude:old'), label_checked=False))
        with patch.object(c, 'pane_label', return_value='team-main'):
            self.assertEqual(self.observe(sid='old')['name'], 'team-main')
            self.assertEqual(self.observe(sid='new')['name'], 'team-main-2')
        with patch.object(c, 'pane_label', return_value='team-qa') as label:
            self.observe(sid='old')
            label.assert_not_called()  # one look per session, not one tmux call per hook event
        row = c.session('claude:new'); c.claim_name(row['id'], 'reviewer', explicit=True)
        c.write(c.path('sessions', row['id']), dict(row, name='reviewer', named=True))
        with patch.object(c, 'pane_label', return_value='team-qa'):
            self.assertEqual(self.observe(sid='new')['name'], 'reviewer')

    def test_claude_native_inbox_delivery(self):
        import socket as so, threading
        row = self.observe('Stop', sid='native')  # idle
        short = tempfile.mkdtemp(prefix='pn-', dir='/tmp')  # macOS sun_path is 104 bytes
        sock_path = short + '/s.sock'
        sessions = self.root / 'claude/sessions'; sessions.mkdir(parents=True)
        pid = row['process']['pid']
        (sessions / ('%s.json' % pid)).write_text(json.dumps({'sessionId': 'native', 'peerProtocol': 1, 'messagingSocketPath': sock_path}))
        (sessions / ('%s.abc.key' % pid)).write_text(json.dumps({'peerToken': 'tok123'}))
        (self.root / 'claude/settings.json').write_text(json.dumps({'crossSessionInbound': 'accept'}))
        server = so.socket(so.AF_UNIX); server.bind(sock_path); server.listen(); got = []
        def serve():
            conn, _ = server.accept()
            with conn, conn.makefile('rb') as stream:
                got.extend(json.loads(line) for line in stream if line.strip())
        thread = threading.Thread(target=serve); thread.start()
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.root / 'claude')):
            request = dict(id=secrets.token_hex(12), recipient=row['id'], body='hello native',
                           sender={'id': 'codex:other', 'name': 'other', 'host': 'test', 'mode': 'bypass'})
            d.receive(request, 'test')
            d.attempt(c.mail_path(row['id'], request['id']))
        thread.join(5); server.close()
        self.assertEqual(got[0], {'type': 'auth', 'token': 'tok123'})
        content = got[1]['message']['content']
        self.assertIn('from-mode="bypass"', content)
        self.assertIn('[from other@test] hello native', content)
        self.assertEqual(c.receipt(request['id'])['stage'], 'accepted')
        self.assertEqual(c.receipt(request['id'])['route'], 'claude-native')

    def test_native_inbox_needs_accept_setting(self):
        row = self.observe('Stop', sid='held')
        config = self.root / 'claude2'; (config / 'sessions').mkdir(parents=True)
        pid = row['process']['pid']
        (config / 'sessions' / ('%s.json' % pid)).write_text(json.dumps({'sessionId': 'held', 'peerProtocol': 1, 'messagingSocketPath': '/tmp/x.sock'}))
        (config / 'sessions' / ('%s.k.key' % pid)).write_text(json.dumps({'peerToken': 't'}))
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(config)):
            self.assertIsNone(d.claude_native(row))  # no approval screen in front of the user
            (config / 'settings.json').write_text(json.dumps({'crossSessionInbound': 'accept'}))
            self.assertEqual(d.claude_native(row)['token'], 't')

    def test_accepted_without_evidence_is_settled(self):
        row = self.observe('Stop', sid='settle')
        request, rec = self.message(row)
        file = c.mail_path(row['id'], request['id'])
        c.update_receipt(request['id'], stage='accepted')
        d.settle_accepted(file)
        self.assertTrue(file.exists())  # recent: evidence may still come
        c.write(c.path('receipts', request['id']), dict(c.receipt(request['id']), updated=time.time() - d.ACCEPTED_TTL - 1))
        d.settle_accepted(file)
        self.assertFalse(file.exists())
        self.assertEqual(c.receipt(request['id'])['stage'], 'accepted')

    def test_say_reports_where_the_message_went(self):
        stages = iter([{'id': 'x', 'stage': 'sending'}, {'id': 'x', 'stage': 'accepted', 'route': 'claude-native'}])
        with patch.object(cli, 'dispatch', side_effect=lambda owner, req: next(stages)), patch.object(cli.time, 'sleep'):
            result = cli.settled('server', 'x', {'id': 'x', 'stage': 'queued'})
        self.assertEqual((result['stage'], result['route']), ('accepted', 'claude-native'))
        with patch.object(cli, 'dispatch') as dispatch:
            self.assertEqual(cli.settled('server', 'x', {'stage': 'held'})['stage'], 'held')
            dispatch.assert_not_called()  # held already says what happened

    def test_hosts_json_and_ssh_command(self):
        (self.root / 'config/hosts.json').write_text(json.dumps({'test': {'ssh': 'self'}, 'server': {'ssh': 'me@server'}}))
        self.assertEqual(list(t.hosts()), ['server'])  # this machine is skipped
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd)
            return type('R', (), {'returncode': 0, 'stdout': b'{"peers": []}', 'stderr': b''})()
        with patch.object(t.subprocess, 'run', side_effect=fake):
            t.inventory([])
        self.assertEqual(calls[0][:6], ['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3'])
        self.assertEqual(calls[0][6], 'me@server')
        self.assertTrue(calls[0][7].startswith('~/.local/bin/peer _rpc test '))
        (self.root / 'config/hosts.json').write_text(json.dumps({'bad': 'server'}))
        with self.assertRaises(ValueError):
            t.hosts()

    def test_install_keeps_other_settings_and_uninstall_is_clean(self):
        claude = self.root / 'claude'; claude.mkdir()
        mine = {'model': 'opus', 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'my-own-hook'}]}]}}
        (claude / 'settings.json').write_text(json.dumps(mine))
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(claude), CODEX_HOME=str(self.root / 'no-codex')), \
                contextlib.redirect_stdout(io.StringIO()):
            setup.install(['install'])
            once = (claude / 'settings.json').read_text()
            setup.install(['install'])
            self.assertEqual((claude / 'settings.json').read_text(), once)  # idempotent
            self.assertTrue(setup.hooks_installed(claude / 'settings.json', 'claude'))
            self.assertNotIn('crossSessionInbound', json.loads(once))  # only with --accept-inbound
            setup.install(['uninstall'])
        self.assertEqual(json.loads((claude / 'settings.json').read_text()), mine)
        self.assertFalse((self.root / 'no-codex').exists())  # no Codex, nothing created

    def test_run_codex_gives_config_overrides_to_the_server(self):
        loader = importlib.machinery.SourceFileLoader('peer_session', str(BIN / 'peer-session'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        session = importlib.util.module_from_spec(spec); loader.exec_module(session)
        args = ['-s', 'danger-full-access', '-c', 'model_reasoning_effort=high', '--enable', 'x', '--config=a=1', 'resume', 'abc']
        self.assertEqual(session.config_overrides(args), ['-c', 'model_reasoning_effort=high', '--enable', 'x', '--config=a=1'])

    def test_run_codex_routes_only_session_commands_through_the_server(self):
        loader = importlib.machinery.SourceFileLoader('peer_session2', str(BIN / 'peer-session'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        session = importlib.util.module_from_spec(spec); loader.exec_module(session)
        with patch.object(session, 'codex_subcommands', return_value={'exec', 'e', 'login', 'resume', 'fork'}):
            self.assertEqual(session.tui_args([], 's'), ['--remote', 'unix://s'])
            self.assertEqual(session.tui_args(['fix the bug'], 's'), ['--remote', 'unix://s', 'fix the bug'])
            self.assertEqual(session.tui_args(['-c', 'a=b', 'resume', '--last'], 's'), ['-c', 'a=b', 'resume', '--remote', 'unix://s', '--last'])
            self.assertEqual(session.tui_args(['-m', 'resume'], 's'), ['--remote', 'unix://s', '-m', 'resume'])  # a model named resume
            for passthrough in (['exec', 'hi'], ['-m', 'x', 'e', 'hi'], ['login'], ['--version']):
                self.assertIsNone(session.tui_args(passthrough, 's'), passthrough)

    def test_codex_alias_goes_in_and_out_of_the_shell_rc_cleanly(self):
        home = self.root / 'home'; home.mkdir()
        rc = home / '.zshrc'
        rc.write_text('export A=1\n')
        with patch.dict(os.environ, SHELL='/bin/zsh', ZDOTDIR=str(home)):
            self.assertFalse(setup.alias_installed())
            setup.set_alias()
            setup.set_alias()  # idempotent
            text = rc.read_text()
            self.assertEqual(text.count(setup.ALIAS_START), 1)
            self.assertIn("alias codex='peer run codex'", text)
            self.assertTrue(text.startswith('export A=1\n'))
            self.assertTrue(setup.alias_installed())
            setup.set_alias(remove=True)
            self.assertEqual(rc.read_text(), 'export A=1\n')

    def test_websocket_client_speaks_rfc6455(self):
        import base64, hashlib, socket, threading
        client_sock, server_sock = socket.socketpair()
        got = []

        def server():
            data = b''
            while b'\r\n\r\n' not in data:
                data += server_sock.recv(4096)
            key = [l.split(b': ')[1] for l in data.split(b'\r\n') if l.lower().startswith(b'sec-websocket-key')][0]
            accept = base64.b64encode(hashlib.sha1(key + t.WebSocket.GUID.encode()).digest())
            server_sock.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                                b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            # one masked client frame in
            head = server_sock.recv(2); n = head[1] & 0x7f
            if n == 126: n = int.from_bytes(server_sock.recv(2), 'big')
            mask = server_sock.recv(4); body = b''
            while len(body) < n: body += server_sock.recv(n - len(body))
            got.append(bytes(b ^ mask[i & 3] for i, b in enumerate(body)).decode())
            big = ('x' * 70000).encode()
            server_sock.sendall(bytes([0x89, 0]))                                    # ping, answered then skipped
            server_sock.sendall(bytes([0x81, 127]) + len(big).to_bytes(8, 'big') + big)  # 64-bit length
            server_sock.sendall(bytes([0x01, 3]) + b'{"a' + bytes([0x80, 4]) + b'":1}')  # fragmented
            pong = server_sock.recv(2)
            got.append(pong[0] & 0x0f)

        thread = threading.Thread(target=server); thread.start()
        ws = t.WebSocket(client_sock)
        ws.send(json.dumps({'id': 1, 'method': 'initialize'}))
        self.assertEqual(ws.recv(), 'x' * 70000)
        self.assertEqual(json.loads(ws.recv()), {'a': 1})
        thread.join(5)
        self.assertEqual(json.loads(got[0])['method'], 'initialize')
        self.assertEqual(got[1], 0xA)
        ws.close(); server_sock.close()

if __name__ == '__main__':
    unittest.main()

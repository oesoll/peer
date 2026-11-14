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
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import peer_core as c
import peer_codex
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
        # The suite may itself run inside an sm session or a Fine tab; that identity must not leak into the fixtures.
        for key in ('PEER_NAME', 'PEER_CODEX_SOCKET', 'TMUX', 'PEER_VISIBLE', 'PEER_PARENT', 'PEER_HOLD_SOCK',
                    'SM_PANE_ID', 'SM_WORKSPACE', 'SM_STATE', 'FINE_TAB_ID'):
            os.environ.pop(key, None)
        os.environ['FINE_HOME'] = str(self.root / 'fine-home')  # no Fine here unless a test opens one
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.root / 'claude')
        os.environ['CODEX_HOME'] = str(self.root / 'codex')
        self.proc = c.process(os.getpid())
        self.agent = dict(self.proc, name='claude')
        self.chain = patch.object(c, 'ancestry', return_value=[self.agent]); self.chain.start()
        self.worker = patch.object(d, 'start_worker'); self.worker.start()
        # Never the session layer of the machine running the tests; tests that need sm bring a fake one.
        self.no_sm = patch.object(c, 'sm', return_value=None); self.no_sm.start()

    def tearDown(self):
        self.no_sm.stop(); self.worker.stop(); self.chain.stop(); self.env.stop(); self.temp.cleanup()

    def fake_sm(self):
        """sm's records in a temp folder, the way sm_session keeps them: one JSON file per session name."""
        folder = self.root / 'sm'
        (folder / 'sessions').mkdir(parents=True, exist_ok=True)
        layer = SimpleNamespace(BIN=str(self.root / 'sm-bin'), read=c.read,
                                entry_path=lambda name: folder / 'sessions' / (name + '.json'))
        return folder, layer

    def observe(self, event='SessionStart', sid='abc', **values):
        return c.observe(dict(hook_event_name=event, session_id=sid, cwd='/project/demo', **values), 'claude')

    def test_a_name_from_a_dot_folder_or_label_still_registers(self):
        """Issue #1: a dot-named folder gave '.dotfiles-claude', valid_name refused it, the hook swallowed the error."""
        def name_for(sid, cwd):
            return c.observe(dict(hook_event_name='SessionStart', session_id=sid, cwd=cwd), 'claude')['name']
        with patch.object(c, 'pane_label', return_value=None):
            self.assertEqual(name_for('s1', '/home/me/.dotfiles'), 'dotfiles-claude')
            self.assertEqual(name_for('s2', '/home/me/-.config.'), 'config-claude')
            self.assertEqual(name_for('s3', '/home/me/...'), 'agent-claude')
        with patch.dict(os.environ, PEER_NAME='.hidden lane'), patch.object(c, 'pane_label', return_value=None):
            self.assertEqual(name_for('s4', '/project/demo'), 'hidden-lane')
        with patch.object(c.subprocess, 'run', return_value=SimpleNamespace(stdout='.dots\treview\n')):
            self.assertEqual(c.pane_label(), 'dots-review')
        try:
            raise ValueError('secret prompt text')
        except ValueError as error:
            c.note_hook_failure(error)
        logged = (c.root() / 'hook-errors.log').read_text()
        self.assertIn('ValueError', logged)
        self.assertNotIn('secret', logged)

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

    def dead_session(self, sid='resumed'):
        row = self.observe(sid=sid)
        row['process'] = dict(row['process'], start='ended')
        c.write(c.path('sessions', row['id']), row)
        return row

    def engine_file(self, sid='resumed', status='idle'):
        folder = self.root / 'claude/sessions'
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / (str(os.getpid()) + '.json')
        file.write_text(json.dumps(dict(sessionId=sid, status=status)))
        return file

    def test_resume_is_found_by_inventory_without_a_hook(self):
        row = self.dead_session()
        self.engine_file(status='busy')
        with patch.object(c, 'process', return_value=self.agent):
            back = cli.local_inventory()[0]
        self.assertEqual(back['state'], 'working')
        self.assertEqual(back['name'], row['name'])
        self.assertEqual(back['reason'], 'engine-file')
        self.assertEqual(c.session(row['id'])['process'], self.agent)
        self.assertGreaterEqual(back['since'], row['since'])

    def test_receive_and_worker_rediscover_resumed_recipient(self):
        row = self.dead_session()
        request, held = self.message(row)
        self.assertEqual(held['stage'], 'held')
        self.engine_file()
        with patch.object(c, 'process', return_value=self.agent), \
                patch.object(d, 'claude_native', return_value={'socket': 'fixture'}), \
                patch.object(d, 'native_send', return_value=True) as send:
            d.attempt(c.mail_path(row['id'], request['id']))
            send.assert_called_once()
            self.assertEqual(c.receipt(request['id'])['stage'], 'accepted')
            c.write(c.path('sessions', row['id']), row)
            _, fresh = self.message(row)
            self.assertEqual(fresh['stage'], 'queued')

    def test_resume_keeps_live_permission_state_and_unknown_status_held(self):
        blocked = self.observe('PermissionRequest')
        with patch.object(c, 'claude_processes') as scan:
            self.assertEqual(c.revive(blocked), blocked)
            scan.assert_not_called()
        row = self.dead_session()
        self.engine_file(status='future-status')
        with patch.object(c, 'process', return_value=self.agent), patch.object(d, 'native_send') as send:
            request, rec = self.message(row)
            d.attempt(c.mail_path(row['id'], request['id']))
            self.assertEqual(rec['stage'], 'held')
            self.assertEqual(c.session(row['id'])['state'], 'unknown')
            send.assert_not_called()

    def test_discovery_ignores_invalid_files_and_unrelated_processes(self):
        file = self.engine_file()
        for content in ('{', '[]', '{"pid": [], "sessionId": "x"}', '{"pid": "bad", "sessionId": "x"}'):
            file.write_text(content)
            self.assertEqual(c.claude_processes(), {})
        self.engine_file()
        with patch.object(c, 'process', return_value=dict(self.agent, name='other-program')):
            self.assertEqual(c.claude_processes(), {})
        with patch.object(c, 'process', return_value=None):
            self.assertEqual(c.claude_processes(), {})

    def test_resume_does_not_recreate_deleted_record_or_reuse_changed_pid(self):
        row = self.dead_session()
        hit = {row['session']: (self.agent, 'idle')}
        with patch.object(c, 'process', return_value=dict(self.agent, start='different-process')):
            self.assertEqual(c.revive(row, hit), row)
        c.path('sessions', row['id']).unlink()
        c.revive(row, hit)
        self.assertFalse(c.path('sessions', row['id']).exists())

    def test_live_namesake_wins_and_exact_ids_resolve(self):
        live = dict(id='claude:new', session='new', name='duplicate', host='test', state='idle')
        gone = dict(live, id='claude:old', session='old', state='gone')
        with patch.object(cli, 'all_peers', return_value=([gone, live], [])):
            self.assertEqual(cli.resolve('duplicate'), live)
            self.assertEqual(cli.resolve('old'), gone)
            self.assertEqual(cli.resolve('claude:old@test'), gone)
        other = dict(live, id='claude:second', session='second')
        with patch.object(cli, 'all_peers', return_value=([live, other], [])):
            with self.assertRaisesRegex(ValueError, 'claude:new@test.*claude:second@test'):
                cli.resolve('duplicate')
            self.assertEqual(cli.resolve('claude:second@test'), other)
        other['host'] = 'server'
        with patch.object(cli, 'all_peers', return_value=([live, other], [])):
            self.assertEqual(cli.resolve('duplicate@server'), other)
        named = dict(live, name='old')
        with patch.object(cli, 'all_peers', return_value=([gone, named], [])):
            self.assertEqual(cli.resolve('old'), named)  # names retain precedence over bare IDs

    def test_mail_for_removed_recipient_fails_and_worker_continues(self):
        row = self.observe(sid='removed')
        request, _ = self.message(row)
        other = self.observe('PermissionRequest', sid='kept')
        pending, _ = self.message(other)
        c.path('sessions', row['id']).unlink()
        d.work(once=True)
        rec = c.receipt(request['id'])
        self.assertEqual(rec['stage'], 'failed')
        self.assertEqual(rec['reason'], 'recipient session record removed')
        self.assertNotIn('body', rec)
        self.assertFalse(c.mail_path(row['id'], request['id']).exists())
        self.assertEqual(c.receipt(pending['id'])['stage'], 'held')
        self.assertTrue(c.mail_path(other['id'], pending['id']).exists())

    def test_update_hands_waiting_mail_to_worker_after_unlock(self):
        row = self.observe('PermissionRequest')
        request, _ = self.message(row)
        def replacement():
            with c.lock(c.root() / 'worker', .005):
                d.work(once=True)  # another worker cannot acquire the replacement's lock
        with patch.object(d, 'code_stamp', side_effect=['old', 'new']), \
                patch.object(d, 'start_worker', side_effect=replacement) as start:
            d.work()
            start.assert_called_once()
        self.assertTrue(c.mail_path(row['id'], request['id']).exists())
        self.assertEqual(c.receipt(request['id'])['stage'], 'held')

    def test_update_without_mail_does_not_start_worker(self):
        with patch.object(d, 'code_stamp', side_effect=['old', 'new']), \
                patch.object(d, 'start_worker') as start:
            d.work()
            start.assert_not_called()

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
        # Claude names the sender itself from from-name; the body carries no second [from ...] label
        self.assertIn('from-name="other@test"', content)
        self.assertIn('>\nhello native\n[peer-id:', content)
        self.assertNotIn('[from ', content)
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

    def test_a_record_left_behind_by_its_process_uses_the_live_conversations_inbox(self):
        # /clear or /resume: the same process goes on under a new conversation id.
        old = self.observe('PostToolUse', sid='before')
        live = self.observe('Stop', sid='real-sid')
        self.assertEqual(old['process'], live['process'])
        config = self.root / 'claude3'; (config / 'sessions').mkdir(parents=True)
        pid = live['process']['pid']
        (config / 'settings.json').write_text(json.dumps({'crossSessionInbound': 'accept'}))
        (config / 'sessions' / ('%s.json' % pid)).write_text(json.dumps({'sessionId': 'real-sid', 'peerProtocol': 1, 'messagingSocketPath': '/tmp/x.sock'}))
        (config / 'sessions' / ('%s.k.key' % pid)).write_text(json.dumps({'peerToken': 't'}))
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(config)):
            self.assertIsNone(d.claude_native(old))
            self.assertEqual(d.claude_native_of_process(old)['token'], 't')

    def test_mail_not_delivered_for_hours_is_closed(self):
        row = self.observe('PostToolUse', sid='busy')  # working: mail waits for its next hook
        request, rec = self.message(row)
        file = c.mail_path(row['id'], request['id'])
        d.attempt(file)
        self.assertTrue(file.exists())
        c.write(file, dict(c.read(file), created=time.time() - d.MAIL_TTL - 1))
        d.attempt(file)
        self.assertFalse(file.exists())
        self.assertEqual(c.receipt(request['id'])['stage'], 'failed')

    def test_worker_reads_transcripts_only_for_mail_awaiting_evidence(self):
        row = self.observe('PostToolUse', sid='waits', transcript_path=str(self.root / 't.jsonl'))
        (self.root / 't.jsonl').write_text('x')
        request, rec = self.message(row)
        searched = []
        with patch.object(d, 'transcript_evidence', side_effect=lambda r: searched.append(r['id'])), \
             patch.object(d, 'lock', return_value=contextlib.nullcontext()):
            d.work(once=True)
            self.assertEqual(searched, [])  # queued for a hook: nothing to look for
            c.update_receipt(request['id'], stage='accepted')
            d.work(once=True)
        self.assertEqual(searched, [row['id']])

    def test_a_retried_message_is_the_same_message(self):
        row = self.observe('PostToolUse', sid='retry')
        request, rec = self.message(row)
        self.worker.stop()
        with patch.object(d, 'start_worker') as started:
            self.assertEqual(d.receive(request, 'test')['id'], request['id'])
            started.assert_called_once()  # still waiting: a worker that exited must pick it up again
            with self.assertRaisesRegex(ValueError, 'content conflict'):
                d.receive(dict(request, body='something else'), 'test')
        self.worker.start()

    def failing_setup(self):
        row = self.observe('UserPromptSubmit', sid='worker')
        request, rec = self.message(row)
        c.update_receipt(request['id'], stage='delivered', delivered=time.time())
        return row, request

    def notices(self):
        sent = []
        with patch.object(d, 'receive', side_effect=lambda req, source: sent.append(req)):
            d.notify_turn_failures()
        return sent

    def test_a_failed_turn_tells_whoever_was_waiting_once(self):
        row, request = self.failing_setup()
        failed = self.observe('StopFailure', sid='worker', error='rate_limit', error_details='resets 5pm')
        self.assertEqual((failed['state'], failed['reason']), ('error', 'rate_limit resets 5pm'))
        sent = self.notices()
        self.assertEqual([m['recipient'] for m in sent], ['codex:other'])
        self.assertIn('rate_limit resets 5pm', sent[0]['body'])
        self.assertEqual(sent[0]['sender']['id'], 'service:peer')
        self.assertEqual(c.receipt(request['id'])['turn_failed'], 'rate_limit resets 5pm')
        self.assertEqual(self.notices(), [], 'one notice per failure')
        self.observe('StopFailure', sid='worker', error='rate_limit')
        self.assertEqual(self.notices(), [], 'a later failure does not repeat it for the same message')

    def test_a_finished_turn_closes_what_it_was_asked(self):
        row, request = self.failing_setup()
        time.sleep(.01)
        self.observe('Stop', sid='worker')
        time.sleep(.01)
        self.observe('StopFailure', sid='worker', error='overloaded')
        self.assertEqual(self.notices(), [], 'that message was handled in the turn that ended well')

    def test_failure_notices_do_not_chain_and_retry_when_the_sender_is_away(self):
        row, request = self.failing_setup()
        self.observe('StopFailure', sid='worker', error='rate_limit')
        with patch.object(d, 'receive', side_effect=OSError('away')):
            d.notify_turn_failures()
        self.assertFalse(c.receipt(request['id']).get('turn_failed'))
        self.assertEqual(len(self.notices()), 1, 'tried again next round')
        # a notice from peer itself never produces another notice
        other = self.observe('UserPromptSubmit', sid='w2')
        notice = dict(id=secrets.token_hex(12), recipient=other['id'], body='x', sender=dict(id='service:peer', name='peer', host='test', mode='default'))
        d.receive(notice, 'test')
        c.update_receipt(notice['id'], stage='delivered', delivered=time.time())
        self.observe('StopFailure', sid='w2', error='rate_limit')
        self.assertEqual(self.notices(), [])

    def test_codex_last_turn_outcome(self):
        client = SimpleNamespace(call=lambda method, params: {'data': [{'status': 'failed', 'error': {'message': "You've hit  your usage limit."}}]})
        self.assertEqual(peer_codex.last_turn(client, 'x'), (True, "You've hit your usage limit."))
        client = SimpleNamespace(call=lambda method, params: {'data': [{'status': 'completed'}]})
        self.assertEqual(peer_codex.last_turn(client, 'x'), (False, None))

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


    def test_hold_targets_and_fields(self):
        def held(name):  # a real holder: this test process stands in as the agent's ancestor
            (self.root / (name + '.json')).write_text(json.dumps(dict(holder=self.proc['pid'])))
            return str(self.root / (name + '.sock'))
        self.chain.stop()
        self.chain = patch.object(c, 'ancestry', return_value=[self.agent, self.proc]); self.chain.start()
        a, b = held('1a2b'), held('9f9f')
        with patch.dict(os.environ, {'SM_PANE_ID': 'ABC-1', 'PEER_HOLD_SOCK': a}):
            del os.environ['TMUX_PANE']
            row = c.public(self.observe())
            self.assertEqual(row['target'], 'sm:abc-1')
            self.assertTrue(row['visible'])
            self.assertEqual(row['hold'], a)
        with patch.dict(os.environ, {'PEER_HOLD_SOCK': b, 'PEER_PARENT': 'laptop/claude:lead'}):
            del os.environ['TMUX_PANE']
            row = c.public(self.observe(sid='lane'))
            self.assertEqual(row['target'], 'hold:9f9f')
            self.assertFalse(row['visible'])
            self.assertEqual(row['parent'], 'laptop/claude:lead')
        self.assertEqual(c.public(self.observe(sid='pane'))['target'], '%1')  # a tmux pane still wins
        self.assertTrue(c.public(c.session('claude:pane'))['visible'])  # no holder: a terminal someone sees

    def test_sm_name_wins_over_a_picked_up_name(self):
        first = self.observe()  # registered on its own (no PEER_NAME), e.g. its last hook in a plain terminal
        self.assertEqual(first['name'], 'demo-claude')
        with patch.dict(os.environ, PEER_NAME='picked'):
            self.assertEqual(self.observe()['name'], 'picked')
        cli_row = c.session(first['id']); cli_row['chosen'] = True; c.write(c.path('sessions', first['id']), cli_row)
        with patch.dict(os.environ, PEER_NAME='other'):
            self.assertEqual(self.observe()['name'], 'picked')  # a name chosen with `peer name` stays

    def test_peer_name_is_kept_by_hooks_and_the_codex_watch(self):
        row = self.observe()
        with patch.object(cli, 'me', return_value=row), contextlib.redirect_stdout(io.StringIO()):
            cli.main(['name', 'chosen-one'])
        self.assertTrue(c.session(row['id'])['chosen'])
        with patch.dict(os.environ, PEER_NAME='from-sm'):
            self.assertEqual(self.observe()['name'], 'chosen-one')

    def test_sm_reuses_a_closed_sessions_name(self):
        with patch.dict(os.environ, PEER_NAME='lane'):
            old = self.observe(sid='old')
        c.write(c.path('sessions', old['id']), dict(old, process=dict(old['process'], start=[0, 0])))
        folder, layer = self.fake_sm()
        (folder / 'sessions/lane.json').write_text(json.dumps(dict(name='lane', session='new')))
        with patch.object(c, 'sm', return_value=layer), patch.dict(os.environ, PEER_NAME='lane'):
            self.assertEqual(self.observe(sid='new')['name'], 'lane')
            self.assertEqual(c.session(old['id'])['name'], 'lane-2')
            # a live holder keeps it: sm's record alone does not take a name from a running session
            (folder / 'sessions/busy.json').write_text(json.dumps(dict(name='busy', session='third')))
            with patch.dict(os.environ, PEER_NAME='busy'):
                self.observe(sid='live')
                self.assertEqual(self.observe(sid='third')['name'], 'busy-2')

    def test_fine_tabs_get_their_titles_as_names(self):
        self.engine_file(sid='tabsid')
        old = self.observe(sid='autoname')  # registered by the hook under a folder name
        self.assertEqual(old['name'], 'demo-claude')
        tabs = [dict(id='AAAAAAAA-0000-0000-0000-000000000001', name='Trip planning', harness='claude', sessionId='tabsid', running=True),
                dict(id='AAAAAAAA-0000-0000-0000-000000000002', name='리뷰 담당', harness='claude', sessionId='autoname', running=True)]
        with patch.object(c, 'process', return_value=self.agent):
            c.adopt_fine_tabs(tabs)
            new = c.session('claude:tabsid')
            self.assertEqual((new['name'], new['title'], new['target']), ('Trip-planning', 'Trip planning', 'fine:aaaaaaaa-0000-0000-0000-000000000001'))
            self.assertEqual(c.public(new)['state'], 'idle')
        self.assertEqual(c.session(old['id'])['name'], '리뷰-담당')
        tabs[1]['name'] = 'Another title'
        c.adopt_fine_tabs(tabs, live={})
        self.assertEqual(c.session(old['id'])['name'], '리뷰-담당')  # renamed once; a name others use stays

    def test_identity_inherited_through_an_app_is_dropped(self):
        (self.root / 'h1.json').write_text(json.dumps(dict(holder=999999)))  # another agent's holder, not our ancestor
        inherited = {'PEER_NAME': 'someone-else', 'PEER_HOLD_SOCK': str(self.root / 'h1.sock'), 'PEER_VISIBLE': '1'}
        with patch.dict(os.environ, inherited):
            row = self.observe(sid='tabbed')
        self.assertEqual(row['name'], 'demo-claude')
        self.assertIsNone(row['hold'])
        (self.root / 'h2.json').write_text(json.dumps(dict(holder=self.agent['pid'])))  # our own holder: an ancestor
        with patch.dict(os.environ, {'PEER_NAME': 'real-lane', 'PEER_HOLD_SOCK': str(self.root / 'h2.sock')}):
            self.assertEqual(self.observe(sid='held')['name'], 'real-lane')
        with patch.dict(os.environ, {'PEER_NAME': 'someone-else', 'FINE_TAB_ID': 'aaaaaaaa-0000-0000-0000-000000000009',
                                     'SM_PANE_ID': 'p-x'}):
            del os.environ['TMUX_PANE']
            row = self.observe(sid='fine-tab')
        self.assertEqual((row['name'], row['target']), ('demo-claude-2', 'fine:aaaaaaaa-0000-0000-0000-000000000009'))

    def test_fine_tab_on_sm_keeps_its_sm_name_and_answers_to_its_title(self):
        held = self.observe(sid='finesm')
        row = c.session(held['id']); row.update(name='fine-718a8b4e', hold='/s/x.sock'); c.write(c.path('sessions', held['id']), row)
        c.adopt_fine_tabs([dict(id='718A8B4E-0000-0000-0000-000000000000', name='Trip planning', harness='claude',
                                sessionId='finesm', running=True)], live={})
        after = c.session(held['id'])
        self.assertEqual((after['name'], after['title']), ('fine-718a8b4e', 'Trip planning'))
        with patch.object(cli, 'all_peers', return_value=([c.public(after)], [])):
            self.assertEqual(cli.resolve('Trip-planning')['id'], held['id'])

    def test_a_sleeping_session_looks_idle_and_mail_wakes_it(self):
        row = self.observe(sid='sleeper')
        folder, layer = self.fake_sm()
        c.write(c.path('sessions', row['id']), dict(row, hold='/x/s.sock', process=dict(row['process'], start=[0, 0])))
        (folder / 'sessions' / (row['name'] + '.json')).write_text(json.dumps(dict(name=row['name'], slept=1)))
        with patch.object(c, 'sm', return_value=layer), patch.object(d, 'sm', return_value=layer):
            state = c.public(c.session(row['id']))
            self.assertEqual((state['state'], state['reason']), ('idle', 'asleep'))
            request, rec = self.message(c.session(row['id']))
            mail = c.mail_path(row['id'], request['id'])
            with patch.object(d.subprocess, 'Popen') as popen:
                d.attempt(mail)
                d.attempt(mail)  # a second try while it wakes does not start another
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args[0][0][-3:], [str(Path(layer.BIN) / 'sm-session'), 'wake', row['name']])
            self.assertEqual(c.receipt(request['id'])['reason'], 'waking the session')
        # without sm there is no sleep: a session whose process ended is gone
        self.assertEqual(c.public(c.session(row['id']))['state'], 'gone')

    def test_omo_registers_and_takes_mail_on_its_socket(self):
        import socket as sk, threading
        self.agent['name'] = 'bun'   # OmO's RPC host is the agent process
        short = tempfile.mkdtemp(prefix='po-', dir='/tmp')  # macOS sun_path is 104 bytes
        self.addCleanup(lambda: __import__('shutil').rmtree(short, ignore_errors=True))
        sock = short + '/o.sock'
        got = []
        server = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM); server.bind(sock); server.listen(1)
        def serve():
            conn, _ = server.accept()
            line = b''
            while not line.endswith(b'\n'):
                line += conn.recv(4096)
            got.append(json.loads(line))
            conn.sendall(b'{"ok": true, "mode": "start"}\n'); conn.close()
        thread = threading.Thread(target=serve); thread.start()
        row = c.observe(dict(hook_event_name='SessionStart', session_id='omo1', cwd='/project/demo', omo_socket=sock), 'omo')
        self.assertEqual((row['kind'], row['state'], row['omo_socket']), ('omo', 'idle', sock))
        self.assertEqual(row['name'], 'demo-omo')
        self.assertEqual(cli.delivery_route(row), 'omo-socket')
        req, _ = self.message(row)
        d.attempt(c.mail_path(row['id'], req['id']))
        thread.join(5); server.close()
        rec = c.receipt(req['id'])
        self.assertEqual((rec['stage'], rec['route']), ('delivered', 'omo-start'))
        self.assertIn('private payload', got[0]['text'])
        self.assertEqual(got[0]['id'], req['id'])
        # the socket is gone (OmO ended without its shutdown event): held, saying why, never lost
        os.unlink(sock)
        req, _ = self.message(row)
        d.attempt(c.mail_path(row['id'], req['id']))
        self.assertEqual(c.receipt(req['id'])['stage'], 'held')
if __name__ == '__main__':
    unittest.main()

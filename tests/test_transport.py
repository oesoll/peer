"""The Codex launcher's resume handling and the identity it passes to OmO."""
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import peer_codex
from peer_transport import RPCError
loader = importlib.machinery.SourceFileLoader('peer_session_launcher', str(BIN / 'peer-session'))
spec = importlib.util.spec_from_loader(loader.name, loader)
session = importlib.util.module_from_spec(spec); loader.exec_module(session)


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

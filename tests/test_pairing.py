"""Pairing tests use temporary identities and loopback sockets, never real accounts."""
import concurrent.futures
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from pairing_store import PairingStore
import paired_server


class SocketLinesTests(unittest.TestCase):
    def reader(self, chunks, authenticated=False):
        class Socket:
            def recv(self, _limit):
                item = next(chunks)
                if isinstance(item, Exception): raise item
                return item
        reader = paired_server.SocketLines(Socket())
        reader.allow_idle = authenticated
        return reader

    def test_authenticated_idle_timeout_keeps_stream_readable(self):
        reader = self.reader(iter([socket.timeout(), socket.timeout(), b'{"sessionId":"fixture"}\n']), True)
        self.assertEqual('fixture', paired_server.read_message(reader)['sessionId'])

    def test_initial_handshake_timeout_still_closes(self):
        with self.assertRaises(socket.timeout):
            paired_server.read_message(self.reader(iter([socket.timeout()])))

    def test_partial_frame_timeout_is_not_ignored(self):
        with self.assertRaises(socket.timeout):
            paired_server.read_message(self.reader(iter([b'{', socket.timeout()]), True))

    def test_coalesced_frames_are_preserved(self):
        reader = self.reader(iter([b'{"a":1}\n{"b":2}\n']))
        self.assertEqual({'a': 1}, paired_server.read_message(reader))
        self.assertEqual({'b': 2}, paired_server.read_message(reader))

    def test_frame_trickle_has_an_absolute_deadline(self):
        with patch.object(paired_server.time, 'monotonic', side_effect=[0, 0, 61]):
            with self.assertRaises(TimeoutError):
                paired_server.read_message(self.reader(iter([b'{', b' ']), True))


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.store = PairingStore(Path(self.tmp.name) / 'devices.db', lambda: self.now)
        self.secret = secrets.token_urlsafe(32)
    def tearDown(self): self.tmp.cleanup()
    def start(self):
        self.store.open_window()
        return self.store.start('Test phone', '127.0.0.1', self.secret)
    def test_pairing_closed_by_default(self):
        with self.assertRaises(ValueError): self.store.start('Phone', 'local', self.secret)
    def test_pending_cannot_authenticate(self):
        request = self.start()
        self.assertFalse(self.store.authorized(request['requestId'], self.secret))
        self.assertEqual('pending', self.store.status(request['requestId'], self.secret)['status'])
    def test_only_local_approval_issues_access(self):
        request = self.start()
        self.assertTrue(self.store.approve(request['requestId'], request['code']))
        self.assertTrue(self.store.authorized(request['requestId'], self.secret))
        self.assertEqual('approved', self.store.status(request['requestId'], self.secret)['status'])
    def test_wrong_secret_cannot_poll_or_authenticate(self):
        request = self.start()
        with self.assertRaises(ValueError): self.store.status(request['requestId'], 'x' * 43)
        self.assertFalse(self.store.authorized(request['requestId'], 'x' * 43))
    def test_codes_and_tokens_not_stored_in_plaintext(self):
        request = self.start()
        raw = (Path(self.tmp.name) / 'devices.db').read_bytes()
        self.assertNotIn(self.secret.encode(), raw)
        self.assertNotIn(request['code'].encode(), raw)
        self.assertNotIn('code_hash', self.store.pending()[0])
    def test_code_expires(self):
        request = self.start(); self.now += 181
        self.assertFalse(self.store.approve(request['requestId'], request['code']))
        self.assertEqual('expired', self.store.status(request['requestId'], self.secret)['status'])
    def test_five_wrong_attempts_lock_request(self):
        request = self.start()
        wrong = '000000' if request['code'] != '000000' else '111111'
        for _ in range(5): self.assertFalse(self.store.approve(request['requestId'], wrong))
        self.assertFalse(self.store.approve(request['requestId'], request['code']))
    def test_code_cannot_be_used_twice(self):
        request = self.start()
        self.assertTrue(self.store.approve(request['requestId'], request['code']))
        self.assertFalse(self.store.approve(request['requestId'], request['code']))
    def test_concurrent_approvals_create_exactly_one_device(self):
        request = self.start()
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.store.approve(request['requestId'], request['code']), range(4)))
        self.assertEqual(1, sum(results)); self.assertEqual(1, len(self.store.devices()))
    def test_close_denies_pending_requests(self):
        request = self.start(); self.store.close_window()
        self.assertFalse(self.store.approve(request['requestId'], request['code']))
        self.assertEqual('denied', self.store.status(request['requestId'], self.secret)['status'])
    def test_deny_blocks_code(self):
        request = self.start(); self.store.deny(request['requestId'])
        self.assertFalse(self.store.approve(request['requestId'], request['code']))
    def test_revoke_survives_reopen(self):
        request = self.start(); self.store.approve(request['requestId'], request['code'])
        self.store.revoke(request['requestId'])
        self.assertFalse(PairingStore(self.store.path).authorized(request['requestId'], self.secret))
        self.assertEqual('revoked', self.store.status(request['requestId'], self.secret)['status'])
    def test_cancel_cannot_target_another_device(self):
        request = self.start(); self.store.approve(request['requestId'], request['code'])
        with self.assertRaises(ValueError): self.store.cancel(request['requestId'], 'x' * 43)
        self.assertTrue(self.store.authorized(request['requestId'], self.secret))
        self.store.cancel(request['requestId'], self.secret)
        self.assertFalse(self.store.authorized(request['requestId'], self.secret))
    def test_rate_limit_and_terminal_control_characters(self):
        self.store.open_window()
        for name in ['\x1b[31mPhone', 'Fake\u202ePhone', 'bad\nname']:
            with self.assertRaises(ValueError): self.store.start(name, 'local', self.secret)
        for _ in range(5): self.store.start('Phone', 'local', secrets.token_urlsafe(32))
        with self.assertRaises(ValueError): self.store.start('Phone', 'local', self.secret)
    def test_malformed_device_identity_is_rejected(self):
        self.assertFalse(self.store.authorized({}, self.secret))
        self.assertFalse(self.store.authorized('x' * 32, '\u2603' * 43))


@unittest.skipUnless(importlib.util.find_spec('cryptography'), 'Optional TLS dependency not installed')
class TlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cert, self.key, self.fingerprint = paired_server.identity(self.home)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert, self.key)
        self.store = PairingStore(self.home / 'devices.db')
        self.patch = patch.object(paired_server, 'discover_sessions', return_value=[{'name': 'fixture', 'reachable': True}])
        self.patch.start()
        self.server = paired_server.PairedServer(('127.0.0.1', 0), context, self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.patch.stop(); self.tmp.cleanup()
    def request(self, value, pin=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(self.server.server_address, timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname='fixture') as tls:
                self.assertEqual(pin or self.fingerprint, hashlib.sha256(tls.getpeercert(True)).hexdigest())
                tls.sendall(json.dumps(value).encode() + b'\n')
                return json.loads(tls.makefile('rb').readline())
    def test_no_session_discovery_without_approval(self):
        result = self.request({'op': 'info', 'deviceId': 'x' * 32, 'secret': 'x' * 43})
        self.assertIn('error', result); self.assertNotIn('sessions', result)
        paired_server.discover_sessions.assert_not_called()
    def test_closed_pairing_window_returns_actionable_error(self):
        result = self.request({'op': 'pair.start', 'name': 'Phone', 'secret': secrets.token_urlsafe(32)})
        self.assertIn('Open Pair devices', result['error'])
    def test_pair_approve_discover_revoke(self):
        self.store.open_window(); secret = secrets.token_urlsafe(32)
        request = self.request({'op': 'pair.start', 'name': 'Phone', 'secret': secret})
        self.assertTrue(self.store.approve(request['requestId'], request['code']))
        auth = {'op': 'info', 'deviceId': request['requestId'], 'secret': secret}
        self.assertEqual('fixture', self.request(auth)['sessions'][0]['name'])
        self.store.revoke(request['requestId'])
        self.assertIn('error', self.request(auth))
    def test_network_cannot_approve(self):
        self.assertIn('error', self.request({'op': 'approve', 'requestId': 'x' * 32, 'code': '123456'}))
    def test_partial_identity_is_never_silently_replaced(self):
        self.key.unlink()
        with self.assertRaises(RuntimeError): paired_server.identity(self.home)
    def test_certificate_identity_is_stable(self):
        self.assertEqual(self.fingerprint, paired_server.identity(self.home)[2])

    def test_large_escaped_history_reply_does_not_close_stream(self):
        responder = self.home / 'large-reply.py'
        responder.write_text('import json,sys\nfor line in sys.stdin:\n'
            ' print(json.dumps({"text":"\\x01"*(1536*1024)}),flush=True)\n')
        self.server.responder = responder
        self.store.open_window(); secret = secrets.token_urlsafe(32)
        request = self.store.start('History phone', 'fixture', secret)
        self.assertTrue(self.store.approve(request['requestId'], request['code']))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(self.server.server_address, timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname='fixture') as tls:
                self.assertEqual(self.fingerprint, hashlib.sha256(tls.getpeercert(True)).hexdigest())
                tls.settimeout(15)
                tls.sendall(json.dumps({'op': 'stdio', 'deviceId': request['requestId'],
                    'secret': secret, 'session': 'fixture'}).encode() + b'\n')
                with tls.makefile('rb') as incoming:
                    self.assertTrue(json.loads(incoming.readline())['ok'])
                    for _ in range(2):
                        tls.sendall(b'{"sessionId":"fixture","type":"raw.read"}\n')
                        line = incoming.readline(paired_server.MAX_REPLY_LINE + 1)
                        self.assertGreater(len(line), 8 * 1024 * 1024)
                        self.assertEqual(1536 * 1024, len(json.loads(line)['text']))

    def test_request_budget_remains_small_when_replies_grow(self):
        self.assertEqual(128 * 1024, paired_server.MAX_LINE)

    def test_authenticated_idle_stream_survives_one_minute_and_revokes(self):
        responder = self.home / 'echo.py'
        responder.write_text('import sys\nfor line in sys.stdin:\n sys.stdout.write(line); sys.stdout.flush()\n')
        self.server.responder = responder
        self.store.open_window(); secret = secrets.token_urlsafe(32)
        request = self.store.start('Idle phone', 'fixture', secret)
        self.assertTrue(self.store.approve(request['requestId'], request['code']))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(self.server.server_address, timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname='fixture') as tls:
                self.assertEqual(self.fingerprint, hashlib.sha256(tls.getpeercert(True)).hexdigest())
                tls.settimeout(8)
                tls.sendall(json.dumps({'op': 'stdio', 'deviceId': request['requestId'],
                    'secret': secret, 'session': 'fixture'}).encode() + b'\n')
                incoming = tls.makefile('rb')
                self.assertTrue(json.loads(incoming.readline())['ok'])
                time.sleep(65)  # Real regression against the previous 60-second disconnect.
                tls.sendall(b'{"sessionId":"fixture","type":"snapshot.get"}\n')
                self.assertEqual('snapshot.get', json.loads(incoming.readline())['type'])
                self.store.revoke(request['requestId'])
                self.assertEqual(b'', incoming.readline())


if __name__ == '__main__': unittest.main()

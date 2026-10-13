"""TLS JSONL transport alongside SSH. Every control connection requires a paired device."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time

from bridge_common import HOME, discover_sessions, valid_session
from pairing_store import PairingStore

PAIR_HOME = HOME / 'paired'
MAX_LINE = 128 * 1024
# Optional dependencies live inside the protected bridge installation, not a user-site import path.
if (HOME / 'paired-runtime').is_dir():
    sys.path.insert(0, str(HOME / 'paired-runtime'))


def identity(directory=PAIR_HOME):
    """Generate once in the bridge owner's private directory; never rotate silently."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key = directory / 'server.pem', directory / 'server-key.pem'
    if cert.exists() != key.exists():
        raise RuntimeError('Incomplete pairing identity; restore it rather than replacing it')
    if not cert.exists():
        private = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Herdr Bridge')])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(private.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(private, hashes.SHA256()))
        for path, data in ((key, private.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                (cert, certificate.public_bytes(serialization.Encoding.PEM))):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding='ascii'))
    return cert, key, hashlib.sha256(der).hexdigest()


def read_message(stream):
    line = stream.readline(MAX_LINE + 1)
    if not line or len(line) > MAX_LINE or not line.endswith(b'\n'):
        raise ValueError('Invalid or oversized frame')
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError('Invalid message')
    return value


class SocketLines:
    """Bound each started frame without expiring an authenticated idle stream.

    Use recv directly: a socket.makefile buffer cannot be reused after a timeout.
    Socket write deadlines remain active for peers that stop reading.
    """
    def __init__(self, conn):
        self.conn, self.buffer = conn, bytearray()
        self.allow_idle = False

    def readline(self, limit):
        started = time.monotonic() if self.buffer else None
        while True:
            newline = self.buffer.find(b'\n')
            if newline >= 0:
                length = min(newline + 1, limit)
                line = bytes(self.buffer[:length])
                del self.buffer[:length]
                return line
            if len(self.buffer) >= limit:
                raise ValueError('Oversized frame')
            if started is not None and time.monotonic() - started >= 60:
                raise TimeoutError('Incomplete frame')
            try:
                chunk = self.conn.recv(min(4096, limit - len(self.buffer)))
            except socket.timeout:
                if self.allow_idle and not self.buffer:
                    continue
                raise
            if not chunk:
                return b''
            if started is None:
                started = time.monotonic()
            self.buffer.extend(chunk)


class PairedServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, context, store, responder=None, bridge_home=HOME):
        self.context, self.store = context, store
        self.responder = Path(responder or Path(__file__).with_name('stdio-responder.py')).resolve()
        self.bridge_home = Path(bridge_home)
        self.capacity = threading.BoundedSemaphore(16)
        self.stream_capacity = threading.BoundedSemaphore(8)
        self.stopping = threading.Event()
        super().__init__(address, PairHandler)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.capacity.release()
            raise

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()

    def handle_error(self, request, client_address):
        pass  # No authentication material, frames, or terminal contents in logs.

    def server_close(self):
        self.stopping.set()
        super().server_close()


class PairHandler(socketserver.BaseRequestHandler):
    def handle(self):
        conn, proc = None, None
        try:
            self.request.settimeout(5)
            conn = self.server.context.wrap_socket(self.request, server_side=True)
            conn.settimeout(10)
            incoming = SocketLines(conn)
            with conn.makefile('wb') as outgoing:
                def reply(data):
                    outgoing.write(json.dumps(data, ensure_ascii=True).encode() + b'\n')
                    outgoing.flush()
                first = read_message(incoming)
                op = first.get('op')
                if op == 'pair.start':
                    try:
                        reply(self.server.store.start(first.get('name'), self.client_address[0], first.get('secret')))
                    except (ValueError, UnicodeError) as exc:
                        reply({'error': str(exc)[:120]})
                    return
                if op == 'pair.status':
                    reply(self.server.store.status(first.get('requestId'), first.get('secret')))
                    return
                if op == 'pair.cancel':
                    self.server.store.cancel(first.get('requestId'), first.get('secret'))
                    reply({'ok': True})
                    return
                device, secret = first.get('deviceId'), first.get('secret')
                if not self.server.store.authorized(device, secret):
                    reply({'error': 'Device is not paired or was revoked'})
                    return
                if op == 'info':
                    reply({'version': 'herdr-mobile-bridge paired/1', 'sessions': discover_sessions()})
                    return
                session = first.get('session')
                if op != 'stdio' or not valid_session(session) or session not in {s['name'] for s in discover_sessions()}:
                    reply({'error': 'Selected session unavailable'})
                    return
                if not self.server.stream_capacity.acquire(blocking=False):
                    reply({'error': 'Too many bridge streams'})
                    return
                try:
                    env = dict(os.environ, HERDR_BRIDGE_HOME=str(self.server.bridge_home))
                    proc = subprocess.Popen([sys.executable, '-E', '-s', '-u', str(self.server.responder), 'stdio', '--session', session],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    conn.settimeout(60)
                    incoming.allow_idle = True
                    done = threading.Event()
                    def guard():
                        while not done.wait(1):
                            try: allowed = not self.server.stopping.is_set() and self.server.store.authorized(device, secret)
                            except Exception: allowed = False
                            if not allowed:
                                try: conn.shutdown(socket.SHUT_RDWR)
                                except OSError: pass
                                if proc.poll() is None: proc.terminate()
                                return
                    def output():
                        try:
                            while not done.is_set():
                                line = proc.stdout.readline(2 * 1024 * 1024 + 1)
                                if not line or len(line) > 2 * 1024 * 1024: break
                                outgoing.write(line)
                                outgoing.flush()
                        except (OSError, ValueError):
                            pass
                        finally:
                            done.set()
                            try: conn.shutdown(socket.SHUT_RDWR)
                            except OSError: pass
                    reply({'ok': True})
                    threading.Thread(target=guard, daemon=True).start()
                    output_thread = threading.Thread(target=output, daemon=True)
                    output_thread.start()
                    try:
                        while not done.is_set():
                            message = read_message(incoming)
                            if message.get('sessionId') != session:
                                raise ValueError('Wrong session')
                            proc.stdin.write(json.dumps(message, ensure_ascii=True).encode() + b'\n')
                            proc.stdin.flush()
                    finally:
                        done.set()
                        try: conn.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                        if proc.poll() is None: proc.terminate()
                        proc.wait(timeout=5)
                        output_thread.join(5)
                        proc.stdin.close()
                        proc.stdout.close()
                finally:
                    self.server.stream_capacity.release()
        except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError):
            pass
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if conn is not None: conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7443)
    args = parser.parse_args()
    cert, key, fingerprint = identity()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    store = PairingStore(PAIR_HOME / 'devices.sqlite3')
    store.close_window()
    with PairedServer((args.listen, args.port), context, store) as server:
        print(f'Herdr paired bridge: {args.listen}:{args.port}; certificate SHA256:{fingerprint}', flush=True)
        server.serve_forever(poll_interval=.25)


if __name__ == '__main__':
    main()

"""Verify the real Herdr approval tab only in a disposable session with synthetic pairing."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
from bridge_common import HERDR, run_cli
from paired_server import identity


def main():
    runtime = ROOT / 'build/paired-runtime'
    if not runtime.is_dir(): raise RuntimeError('Install requirements-pairing.txt into build/paired-runtime for this isolated test')
    name = 'pair-console-' + secrets.token_hex(6)
    output_dir = ROOT / 'build/pairing-console-check'
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'session': name}
    server = listener = None
    with tempfile.TemporaryDirectory(prefix='herdr-pair-console-ș-') as directory:
        home = Path(directory)
        try:
            shutil.copytree(runtime, home / 'paired-runtime')
            cert, key, fingerprint = identity(home / 'paired')
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
            (home / 'paired/config.json').write_text(json.dumps({'listen': '127.0.0.1', 'port': port}), encoding='utf-8')
            (home / 'pair-devices.cmd').write_text(f'@echo off\nchcp 65001 >nul\n"{sys.executable}" -E -s -u "{ROOT / "bridge/pairing_cli.py"}" %*\n', encoding='utf-8')
            env = dict(os.environ, HERDR_BRIDGE_HOME=str(home))
            listener = subprocess.Popen([sys.executable, '-E', '-s', '-u', str(ROOT / 'bridge/paired_server.py'),
                '--port', str(port)], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            server = subprocess.Popen([HERDR, '--session', name, 'server'], cwd=home,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            def cli(*args):
                result = run_cli(['--session', name, *args])
                if result.returncode: raise RuntimeError(result.stderr)
                return result.stdout.decode('utf-8', errors='replace')
            for _ in range(40):
                if run_cli(['--session', name, 'api', 'snapshot'], timeout=2).returncode == 0: break
                time.sleep(.25)
            workspace = json.loads(cli('workspace', 'create', '--cwd', str(home), '--label', 'Pairing console fixture', '--no-focus'))['result']['workspace']['workspace_id']
            launcher = json.loads(cli('tab', 'create', '--workspace', workspace, '--label', 'Pair launcher',
                '--env', f'HERDR_BRIDGE_HOME={home}', '--no-focus'))['result']['root_pane']['pane_id']
            cli('pane', 'run', launcher, f'cmd.exe /d /c ""{home / "pair-devices.cmd"}" --open-tab"')
            approval = None
            text = ''
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                snapshot = json.loads(cli('api', 'snapshot'))['result']['snapshot']
                tab = next((t for t in snapshot['tabs'] if t['label'] == 'Pair devices'), None)
                if tab:
                    approval = next(p['pane_id'] for p in snapshot['panes'] if p['tab_id'] == tab['tab_id'])
                    text = cli('pane', 'read', approval, '--source', 'recent-unwrapped', '--lines', '100', '--raw')
                    if 'Pair devices>' in text: break
                time.sleep(.25)
            assert approval and 'Pair devices>' in text, 'Approval console did not start: ' + text
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
            def request(message):
                with socket.create_connection(('127.0.0.1', port), timeout=5) as raw:
                    with context.wrap_socket(raw, server_hostname='fixture') as tls:
                        assert hashlib.sha256(tls.getpeercert(True)).hexdigest() == fingerprint
                        tls.sendall(json.dumps(message).encode() + b'\n')
                        return json.loads(tls.makefile('rb').readline())
            secret = secrets.token_urlsafe(32)
            pending = request({'op': 'pair.start', 'name': 'Synthetic console phone', 'secret': secret})
            request_id, code = pending['requestId'], pending['code']
            time.sleep(1.2)
            text = cli('pane', 'read', approval, '--source', 'recent-unwrapped', '--lines', '100', '--raw')
            assert 'Synthetic console phone' in text and code not in text and secret not in text
            cli('pane', 'send-text', approval, 'approve ' + request_id[:8])
            cli('pane', 'send-keys', approval, 'enter')
            for _ in range(40):
                text = cli('pane', 'read', approval, '--source', 'recent-unwrapped', '--lines', '100', '--raw')
                if '(hidden):' in text: break
                time.sleep(.1)
            assert '(hidden):' in text
            cli('pane', 'send-text', approval, code)
            cli('pane', 'send-keys', approval, 'enter')
            for _ in range(40):
                status = request({'op': 'pair.status', 'requestId': request_id, 'secret': secret})
                if status['status'] == 'approved': break
                time.sleep(.1)
            assert status['status'] == 'approved'
            text = cli('pane', 'read', approval, '--source', 'recent-unwrapped', '--lines', '200', '--raw')
            assert code not in text and secret not in text, 'Secret input appeared in terminal history'
            cli('pane', 'send-text', approval, 'revoke ' + request_id[:8])
            cli('pane', 'send-keys', approval, 'enter')
            for _ in range(30):
                if request({'op': 'pair.status', 'requestId': request_id, 'secret': secret})['status'] == 'revoked': break
                time.sleep(.1)
            else: raise AssertionError('Console revoke did not apply')
            refused_secret = secrets.token_urlsafe(32)
            refused = request({'op': 'pair.start', 'name': 'Synthetic declined phone', 'secret': refused_secret})
            cli('pane', 'send-text', approval, 'deny ' + refused['requestId'][:8])
            cli('pane', 'send-keys', approval, 'enter')
            for _ in range(30):
                if request({'op': 'pair.status', 'requestId': refused['requestId'], 'secret': refused_secret})['status'] == 'denied': break
                time.sleep(.1)
            else: raise AssertionError('Console deny did not apply')
            cli('pane', 'send-text', approval, 'quit')
            cli('pane', 'send-keys', approval, 'enter')
            report.update(passed=True, dedicated_tab=True, request_notice=True,
                hidden_code_approved=True, no_code_or_token_in_terminal_history=True, console_revoke=True,
                short_id_deny=True)
        finally:
            if listener: listener.terminate(); listener.wait(timeout=5)
            if server:
                run_cli(['--session', name, 'server', 'stop'], timeout=10)
                try: server.wait(timeout=5)
                except subprocess.TimeoutExpired: server.kill(); server.wait()
                report['server_stopped'] = server.poll() is not None
            (output_dir / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()

"""Run this interactive approval console in a dedicated Herdr tab, never an agent prompt."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlencode

from bridge_common import HOME, HERDR, run_cli
from paired_server import PAIR_HOME, identity
from pairing_store import PairingStore


def ensure_server(listen, host, port, fingerprint):
    def check():
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with socket.create_connection(('127.0.0.1' if listen == '0.0.0.0' else listen, port), timeout=1) as raw:
                with context.wrap_socket(raw, server_hostname=host) as connection:
                    if hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest() != fingerprint:
                        raise RuntimeError('Another service or certificate occupies this port')
            return True
        except (ConnectionError, TimeoutError, OSError):
            return False
    if check():
        return
    subprocess.Popen([sys.executable, '-E', '-s', '-u', str(Path(__file__).with_name('paired_server.py')),
                      '--listen', listen, '--port', str(port)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    for _ in range(30):
        if check(): return
        time.sleep(.1)
    raise RuntimeError('Paired bridge did not start; check installation and port')


def console(host, port, fingerprint):
    if not sys.stdin.isatty():
        raise RuntimeError('Pairing approval requires an interactive local terminal')
    sys.stdout.reconfigure(encoding='utf-8')
    uri = f'herdr-pair://{host}:{port}?' + urlencode({'fingerprint': fingerprint})
    store = PairingStore(PAIR_HOME / 'devices.sqlite3')
    print('\nHERDR BRIDGE — PAIR DEVICES\n')
    print('Approve only a phone you are currently pairing. Approval grants terminal control.')
    print('No Windows username or password is used. Pairing stays open for five minutes.')
    print('Scan with Herdr on the Go, or paste this pairing address on your phone:\n' + uri)
    try:
        import qrcode
        qr = qrcode.QRCode(border=4)
        qr.add_data(uri)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print('(Install the optional qrcode package to display a QR code.)')
    print('Commands: list | approve REQUEST_ID | deny REQUEST_ID | devices | revoke DEVICE_ID | quit')
    store.open_window()
    deadline = time.monotonic() + 300
    # A background notifier prints only sanitized names and IDs, never phone codes or tokens.
    import threading
    stopped = threading.Event()
    def notices():
        seen = set()
        while not stopped.wait(1):
            for request in store.pending():
                if request['id'] not in seen:
                    seen.add(request['id'])
                    print(f"\nDevice {request['name']} ({request['address']}) is requesting access.\n"
                          f"Request: {request['id'][:8]}\nType approve {request['id'][:8]} to enter the phone's code.\n", flush=True)
                    try:
                        run_cli(['notification', 'show', 'Device pairing request', '--body',
                                 f"{request['name']} is requesting access. Open Pair devices and enter the code from your phone.",
                                 '--position', 'top-right', '--sound', 'none'], timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass  # The dedicated console notice remains available if a TUI notification fails.
            if time.monotonic() >= deadline:
                store.close_window()
                print('\nPairing window expired. Reopen this tab to accept new requests.', flush=True)
                return
    threading.Thread(target=notices, daemon=True).start()
    try:
        while True:
            parts = input('Pair devices> ').strip().split()
            if not parts: continue
            command = parts[0].lower()
            if command in ('quit', 'exit'): break
            if command == 'list':
                for request in store.pending():
                    print(request['id'], request['name'], request['address'])
            elif command == 'devices':
                for device in store.devices(): print(device['id'], device['name'])
            elif command == 'approve' and len(parts) == 2:
                matches = [r for r in store.pending() if len(parts[1]) >= 8 and r['id'].startswith(parts[1])]
                if len(matches) != 1:
                    print('Request not found or ambiguous. Use list for full identifiers.'); continue
                print('Approving:', matches[0]['name'], matches[0]['address'])
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    code = getpass.getpass('Enter the six digits displayed on your phone (hidden): ')
                print('Device approved.' if store.approve(matches[0]['id'], code) else 'Code incorrect, request expired, or attempt limit reached.')
            elif command == 'deny' and len(parts) == 2:
                matches = [r for r in store.pending() if len(parts[1]) >= 8 and r['id'].startswith(parts[1])]
                if len(matches) != 1:
                    print('Request not found or ambiguous. Use list for full identifiers.'); continue
                store.deny(matches[0]['id']); print('Request denied.')
            elif command == 'revoke' and len(parts) == 2:
                matches = [d for d in store.devices() if len(parts[1]) >= 8 and d['id'].startswith(parts[1])]
                if len(matches) != 1:
                    print('Device not found or ambiguous. Use devices for full identifiers.'); continue
                store.revoke(matches[0]['id']); print('Device revoked; its active connections will close.')
            else:
                print('Use list, approve ID, deny ID, devices, revoke ID, or quit.')
    except (EOFError, KeyboardInterrupt):
        print('\nPairing closed.')
    finally:
        stopped.set()
        store.close_window()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lan', action='store_true', help='Explicitly listen on LAN interfaces; no firewall changes')
    parser.add_argument('--port', type=int)
    parser.add_argument('--advertise', help='Reachable PC address encoded in the pairing QR')
    parser.add_argument('--open-tab', action='store_true', help='Open a dedicated Pair devices tab from inside Herdr')
    args = parser.parse_args()
    config_path = PAIR_HOME / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    args.port = args.port if args.port is not None else config.get('port', 7443)
    if not isinstance(args.port, int) or not 1 <= args.port <= 65535: raise ValueError('Invalid port')
    listen = '0.0.0.0' if args.lan else config.get('listen', '127.0.0.1')
    import ipaddress
    ipaddress.IPv4Address(listen)
    host = args.advertise or (socket.gethostbyname(socket.gethostname()) if listen == '0.0.0.0' else listen)
    # IP/DNS only; no terminal control sequences, URL credentials, paths, or shell fragments.
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', host): raise ValueError('Use a valid IPv4 or DNS address')
    if args.open_tab:
        if os.environ.get('HERDR_ENV') != '1':
            raise RuntimeError('Run --open-tab from a Herdr terminal')
        workspace = os.environ.get('HERDR_WORKSPACE_ID')
        if not workspace: raise RuntimeError('Missing Herdr workspace context')
        result = run_cli(['tab', 'create', '--workspace', workspace, '--label', 'Pair devices', '--focus',
                          '--env', f'HERDR_BRIDGE_HOME={HOME}'])
        if result.returncode != 0: raise RuntimeError('Could not create pairing tab')
        pane = json.loads(result.stdout)['result']['root_pane']['pane_id']
        # Windows installs supply a launcher with the exact protected Python path.
        launcher = HOME / 'pair-devices.cmd'
        if not launcher.is_file(): raise RuntimeError('Reinstall the bridge to add the pairing launcher')
        command = f'cmd.exe /d /c ""{launcher}" ' + ('--lan ' if args.lan else '') + f'--port {args.port} --advertise {host}"'
        result = run_cli(['pane', 'run', pane, command])
        if result.returncode != 0: raise RuntimeError('Could not start pairing console')
        return
    _, _, fingerprint = identity()
    ensure_server(listen, host, args.port, fingerprint)
    console(host, args.port, fingerprint)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print('Herdr pairing:', str(exc), file=sys.stderr)
        sys.exit(1)

# Pair a phone inside Herdr

The optional **paired TLS protocol** runs alongside SSH on a separate TCP port
(default **7443**). It never asks the phone for a Windows username or password.
Approval happens in a dedicated **Pair devices** tab inside Herdr, which is a
bridge console rather than an agent prompt.

## Install on the PC

Requirements: Windows x64, the complete Herdr **0.9.3** release under the user's
normal standalone-release directory, Python **3.11+** with pip, and a reachable
LAN/VPN path from the phone. The existing SSH bridge can stay installed. The
paired-only installer does not require OpenSSH, Git, MSVC, or the SSH wrapper.

From the extracted bridge source, run as the user who owns the Herdr sessions:

```powershell
.\scripts\install-paired.ps1 -Python 'C:\path\to\python.exe' -ListenAddress '0.0.0.0' -Port 7443 -InstallDependencies
```

Replace the Python path. `0.0.0.0` explicitly enables listening on network
interfaces; the installer's default is loopback only. You may use the PC's
specific IPv4 address instead. The installer does **not** change Windows
firewall or SSH configuration. The selected port must be allowed from your
phone's LAN/VPN; a blocked port still needs an approved inbound rule.

This installs optional TLS/QR dependencies into the owner's protected
`%USERPROFILE%\.herdr-bridge-emu\paired-runtime`, backs up replaced bridge files,
creates a hidden logon launcher, starts the listener, and checks its certificate.
It reuses a running snapshot poller or starts one. Existing SSH accounts and
shell settings are unchanged. The listener runs under the logged-in owner;
Herdr sessions must already be running. Close a Pair devices tab before updating
the optional runtime, so its loaded Python libraries can be replaced.

## Pair

1. Inside a normal Herdr terminal, run this command, replacing `YOUR_PC_IP` with
   the address the phone can reach:

   ```powershell
   & "$env:USERPROFILE\.herdr-bridge-emu\pair-devices.cmd" --open-tab --advertise YOUR_PC_IP
   ```

   It creates a dedicated **Pair devices** tab and opens pairing for five minutes.
   Alternatively, run `pair-devices.cmd` directly in a dedicated terminal tab.
2. In Herdr on the Go **0.2.0+**, tap **Pair without Windows login**. Scan the QR
   code shown in that PC tab, or paste its complete `herdr-pair://...` address.
   Use only an address/QR obtained from your own PC; it includes the full
   certificate fingerprint needed to verify the PC before sending credentials.
3. Name the phone and tap **Request pairing**. The phone displays six digits.
4. Herdr shows a device-request notification when a TUI client is available,
   and prints the request in the Pair devices tab. Type
   `approve REQUEST_ID` using the displayed eight-character request prefix,
   then enter the phone's six digits at the hidden prompt. The digits are not
   echoed to terminal history.
5. The app remembers the approved device and discovers running Herdr sessions.
   Select one and use the same spaces, tabs, drafts, and terminal controls as SSH.

No message or code is sent to a coding agent. The pairing console handles its
own input. A request expires after three minutes; five wrong code attempts lock
it. Closing the console denies pending requests and closes pairing. Existing
approved devices continue to connect without reopening pairing.

Authenticated idle streams remain open without a one-minute expiry. Started
frames still have a deadline, stalled writes are bounded, and the authorization
guard continues to close revoked devices. Herdr on the Go 0.2.1 sends read-only
keep-alives and automatically reconnects after temporary network interruptions.

## Revoke a phone

In the Pair devices tab, type `devices`, then `revoke DEVICE_ID` for the chosen
device. Active streams close within about one second and future connections are
rejected. Revocation cannot undo terminal actions that already ran. `deny
REQUEST_ID` rejects a pending request; `quit` closes the approval console.

Forgetting a connection on Android erases its locally stored device credential.
To block that device on the PC, also revoke it in Herdr. If pairing fails after
approval, inspect `devices` and revoke any unused grant before trying again.

## Authentication and stored state

- TLS 1.2 or 1.3 protects the connection. The phone pins the exact certificate
  from the PC's physical QR/address and checks certificate validity **before**
  sending any device secret. There is no trust-all or plaintext fallback.
- The phone generates a random 256-bit device credential. Before local approval,
  it can only create, poll, or cancel its own pending request. It cannot discover
  sessions, read output, or send commands.
- The short approval code is not the transport credential. Codes and device
  credentials are stored as hashes on the PC; Android stores the device
  credential in its keystore-backed encrypted preferences, excluded from backup.
- The local console approves directly through the private database. The network
  protocol exposes no approval endpoint. Authorization is rechecked for active
  streams, and clients cannot select a different session inside a stream.
- Pending requests, connection slots, stream counts, device counts, frame sizes,
  deadlines, and attempts are bounded. Invalid names cannot inject terminal
  escape sequences or invisible formatting characters into the approval notice.
- The bridge owner's protected `paired` directory contains the server private
  key, certificate, configuration, and device database. Back it up privately;
  replacing its certificate requires phones to pair again. Do not publish it.

Paired devices receive control of the owner's Herdr terminals, just as an
authorized SSH client does. This is device pairing, not anonymous access.

## Checks

```powershell
python -B -m unittest discover -s tests -v
python -m pip install --target build/paired-runtime -r requirements-pairing.txt
python -B tests/pairing_console_check.py
```

The console check creates and removes its own disposable Herdr session. It opens
the real Pair devices tab, verifies the request notice, enters a **synthetic**
phone code, confirms neither code nor credential appears in terminal history,
and revokes the device from the console. It never touches working sessions.
Android's `tests/paired_bridge_check.py` verifies the TLS client, rotation,
encrypted credentials, real Herdr command/readback, wrong-pin rejection, and
active revocation on an explicit fresh emulator.

## Upgrade for phone-width fitting

Herdr on the Go 0.2.2 uses bridge **0.3.2** for terminal colors, phone-width
fitting, and cleanup of abandoned notice panes. Re-run the paired installer with
your existing listen address and port, then reconnect the app. The certificate
and device approvals are retained.

An existing snapshot poller is reused by the paired installer. Restart it to
load the new five-minute cleanup; otherwise the old process keeps its old code.
Signing out of Windows and back in starts the updated logon processes. To restart
only the current user's bridge poller without signing out, use:

```powershell
$bridgeDirectory = Join-Path $env:USERPROFILE '.herdr-bridge-emu'
$pollerPath = Join-Path $bridgeDirectory 'snapshot-poller.py'
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine.Contains($pollerPath)
} | ForEach-Object { Stop-Process -Id $_.ProcessId }
Start-Process -FilePath 'C:\path\to\python.exe' -ArgumentList @('-E', '-s', '-u', "`"$pollerPath`"") -WindowStyle Hidden
```

Replace the Python path with the same executable used for installation. This
restarts the snapshot refresher, not the Herdr sessions or terminals. The normal
SSH installer already restarts its poller during an update.

The work PC's actual port/firewall path and physical-phone QR scanning remain
separate installation checks. Tests here do not claim those were exercised.

## Deeper history in bridge 0.3.3

Herdr on the Go 0.2.3 can keep requesting older output beyond the previous
2,000-line ceiling. Update the PC responder to **0.3.3** and reconnect the app.
Each CLI history read is still capped at 2 MiB and depends on the scrollback
Herdr retains. Paired replies allow up to 16 MiB to accommodate escaped JSON;
the incoming request limit remains 128 KiB. Available scrollback and these
byte budgets determine how much history a read can return.

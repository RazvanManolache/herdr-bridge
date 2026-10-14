# herdr-bridge for Windows

A Windows adapter for the Herdr Mobile SSH bridge protocol. Connect a compatible
mobile client to an existing Windows Herdr session to view panes, read terminal
output, send text or keys, and close workspaces. No WSL, Docker, Python packages,
or separate web server is required.

This is an unofficial, single-user adapter using the real Herdr CLI, Windows
OpenSSH, a small native shell wrapper, and Python's standard library. It
implements a subset of the mobile bridge protocol.

The bridge also supports an optional **paired TLS connection**:
approve a phone inside Herdr using its displayed code, without Windows login
credentials. SSH remains supported. See the [pairing installation and use guide](docs/PAIRING.md).
The paired-only installer requires no OpenSSH, Git, or C++ build tools.
Optional dependency terms are recorded in [the notices](THIRD_PARTY_NOTICES.md).

## Contents

- [Compatibility and prerequisites](#compatibility-and-prerequisites)
- [Build and install](#build-and-install)
- [Configure Windows OpenSSH](#configure-windows-openssh)
- [Connect from a phone](#connect-from-a-phone)
- [Verify the installation](#verify-the-installation)
- [How it works](#how-it-works)
- [Security and retry behavior](#security-and-retry-behavior)
- [Configuration and installed files](#configuration-and-installed-files)
- [Updates, backups, and removal](#updates-backups-and-removal)
- [Troubleshooting](#troubleshooting)
- [Development and publishing](#development-and-publishing)

## Compatibility and prerequisites

| Component | Requirement / verified configuration |
| --- | --- |
| Windows | Windows x64 with Windows OpenSSH Server |
| Herdr | **0.9.3**, Windows x64 standalone release; snapshot protocol **22** |
| Bridge identity | Reports `herdr-mobile-bridge 0.3.3`; JSONL envelope version **1** |
| Python | Python 3 with standard-library SQLite; verified with **3.14.7** |
| Git for Windows | Full installation containing `bin/sh.exe` and `usr/bin/perl.exe` |
| Build tools | Visual Studio / Build Tools with **Desktop development with C++**, MSVC x64 tools, and a Windows SDK |
| PowerShell | PowerShell **7** used for verification |
| Mobile client | A compatible Herdr Mobile SSH/JSONL client |

Herdr paths are pinned to **0.9.3**. Installing a newer release does not
automatically update this bridge. Other component versions have not been
certified by this project.

Download prerequisites from their maintainers:

- [Herdr installation guide](https://herdr.dev/docs/install/). Obtain the 0.9.3
  Windows x64 standalone release. Keep its complete release directory, including
  terminal support files, rather than copying only `herdr.exe`.
- [Python for Windows](https://www.python.org/downloads/windows/). Use an
  administrator-managed installation that other local users cannot modify.
- [Git for Windows](https://git-scm.com/downloads/win).
- [Visual Studio C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/).
- [Windows OpenSSH setup guide](https://learn.microsoft.com/windows-server/administration/openssh/openssh_install_firstuse).

Allow approximately **400 MB of extra disk space** for the private Git runtime;
the actual size depends on your Git installation. MSVC is needed to build the
wrapper, but is not a background service used by the bridge.

Run the build and installer as the Windows user who owns the Herdr sessions.
Administrator privileges are needed separately for SSH setup. Installing under
another administrator account creates that account's bridge installation.

## Build and install

### 1. Obtain the source and check prerequisites

Clone the repository, or extract its source archive:

```powershell
git clone https://github.com/RazvanManolache/herdr-bridge.git
Set-Location herdr-bridge
```

Repository access is required while it is private. Open PowerShell in the
directory containing this README and `scripts`.

Replace the Python and Git paths below with your actual paths. The Git path is
the installation root, not its `bin` directory. Keep this PowerShell window
open to reuse the variables in subsequent steps.

```powershell
$pythonExe = 'C:\Python314\python.exe'
$gitRoot = 'C:\Program Files\Git'
$herdrExe = Join-Path $env:USERPROFILE '.herdr\packages\standalone\releases\0.9.3-x86_64-pc-windows-msvc\herdr.exe'

& $pythonExe --version
& $pythonExe -c 'import sqlite3; print(sqlite3.sqlite_version)'
Test-Path -LiteralPath "$gitRoot\bin\sh.exe"
Test-Path -LiteralPath "$gitRoot\usr\bin\perl.exe"
& $herdrExe --version
& $herdrExe session list --json
```

Both path checks must return `True`, and Herdr must report 0.9.3. Place the full
Herdr release at the path above if needed. Start Herdr normally and open a
session/workspace before testing mobile access. The adapter discovers running
sessions; it does not start your working sessions.

If Windows blocks downloaded scripts, review them and unblock them:

```powershell
Unblock-File -LiteralPath .\scripts\build.ps1
Unblock-File -LiteralPath .\scripts\install.ps1
```

On a personal machine whose execution policy still blocks them, permit locally
reviewed scripts for the current PowerShell process:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

Organization-managed policy may require your administrator's configuration.

### 2. Build the wrapper and run the offline tests

```powershell
.\scripts\build.ps1
& $pythonExe -B -m unittest discover -s tests -v
```

The build script locates Visual Studio through `vswhere`, loads the x64 compiler
environment, and produces `build/sh-log.exe` from `sshd/sh-log.c`. It enables
optimization and treats level-four warnings as errors (`/O2 /W4 /WX`).

Offline tests use temporary data and mocked CLI responses. They cover validation,
session routing, retries, concurrency, cache behavior, and diagnostics. They do
not require a phone or SSH connection.

### 3. Install for the current user

```powershell
.\scripts\install.ps1 -GitSource $gitRoot -Python $pythonExe
```

The installer:

- Protects `%USERPROFILE%\.herdr-bridge-emu` and copies a private Git runtime.
- Installs the wrapper, Python modules, keeper, and hidden logon launcher.
- Creates `%USERPROFILE%\.local\bin\herdr-mobile-bridge` and
  `%USERPROFILE%\bin\herdr` for mobile shell probes.
- Preserves timestamped backups of replaced bridge files.
- Attempts an initial snapshot refresh and starts the cache poller.

Default arguments are `-GitSource C:\Utils\Git` and
`-Python C:\Python314\python.exe`; explicit paths are recommended. Python's path
must be an absolute local drive path without a single quote. The installation
directory is fixed; `-BridgeHome` does not support a different location.

The installer does not configure SSH accounts, keys, firewall rules, or the
OpenSSH default shell. Follow the SSH section separately.

### 4. Start the compatibility keeper

Create the socket's parent directory, then run the installed logon launcher:

```powershell
New-Item -ItemType Directory -Path "$env:USERPROFILE\.config\herdr" -Force | Out-Null
$startupLauncher = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\herdr-bridge-keeper.vbs'
cscript.exe //nologo $startupLauncher
```

This starts the keeper and poller without visible windows. Process locks prevent
duplicate instances. The launcher runs at subsequent user logons. The keeper
socket is only a compatibility marker, not the real Herdr server.

This is a per-user logon installation, not a Windows service. After a reboot,
log in to the bridge owner's account and start your Herdr sessions.

## Configure Windows OpenSSH

### 1. Enable the server if needed

Skip installation if OpenSSH Server is already configured. Otherwise, use an
**administrator PowerShell**:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Get-NetFirewallRule -Name OpenSSH-Server-In-TCP
```

Windows normally creates the inbound port-22 rule during installation. Limit it
to your intended devices/networks. For example, for the local subnet on a
Private network:

```powershell
Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -Profile Private -RemoteAddress LocalSubnet
```

Use your trusted VPN subnet instead if needed. Check the active Windows network
profile and any other inbound SSH rules. See
[Microsoft's setup guide](https://learn.microsoft.com/windows-server/administration/openssh/openssh_install_firstuse)
for supported Windows versions and installation details.

### 2. Configure authentication

Use a dedicated SSH key from your mobile client where supported. Install its
**public** key on Windows and keep the private key on the client.

The default Windows OpenSSH configuration uses:

- `%USERPROFILE%\.ssh\authorized_keys` for ordinary users.
- `%ProgramData%\ssh\administrators_authorized_keys` for Administrators-group
  members under the default `Match Group administrators` block. Restrict this
  file's permissions to Administrators and SYSTEM.

Review `%ProgramData%\ssh\sshd_config` for actual key-file settings and account
restrictions such as `AllowUsers`. Restart `sshd` after changes. See
[Microsoft's configuration reference](https://learn.microsoft.com/windows-server/administration/openssh/openssh_server_configuration)
for key-file permissions and account configuration.

### 3. Set the shell wrapper

**This machine-wide SSH setting affects other SSH accounts too.** The runtime
is private to the bridge owner. This setup is intended for a host whose SSH
access is limited to that account; review existing SSH use first.

In administrator PowerShell, record the original `DefaultShell` value, including
whether it is absent, so you can restore it during removal:

```powershell
Get-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -ErrorAction SilentlyContinue
```

Replace `YOUR-USER` with the intended user's actual profile path, even if the
elevated console belongs to another account:

```powershell
$bridgeShell = 'C:\Users\YOUR-USER\.herdr-bridge-emu\sh-log.exe'
if (!(Test-Path -LiteralPath $bridgeShell)) { throw 'Installed bridge shell not found; check the user path.' }
if (!(Test-Path -LiteralPath 'HKLM:\SOFTWARE\OpenSSH')) {
    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' | Out-Null
}
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Value $bridgeShell -Force | Out-Null
Restart-Service sshd
```

Restarting SSH may interrupt connections. The `DefaultShell` registry setting
is documented in
[Microsoft's configuration reference](https://learn.microsoft.com/windows-server/administration/openssh/openssh_server_configuration).

## Connect from a phone

Configure the compatible mobile client's SSH connection:

| Setting | Value |
| --- | --- |
| Host | The PC's reachable LAN address or trusted VPN address |
| Port | `22`, unless configured otherwise |
| User | The account used to install the bridge and run Herdr |
| Authentication | That account's configured SSH credentials |
| Bridge path, if requested | `~/.local/bin/herdr-mobile-bridge` |
| Session | An existing running session returned by discovery |

Keep host-key verification enabled. Inspect the host's public-key fingerprint
on Windows and compare it with the fingerprint shown on the phone:

```powershell
ssh-keygen.exe -lf "$env:ProgramData\ssh\ssh_host_ed25519_key.pub"
```

If another algorithm is negotiated, compare the corresponding
`ssh_host_*_key.pub` instead. Never copy a host private key to the phone.

The client starts a long-lived JSONL connection using a command equivalent to:

```sh
~/.local/bin/herdr-mobile-bridge stdio --session SESSION_NAME
```

The selected session must exist and have a fresh cached snapshot. Each frame's
session must match the selection. Select another session with a new connection.

## Verify the installation

### Local smoke checks

Run as the bridge owner in ordinary PowerShell:

```powershell
$bridgeShell = Join-Path $env:USERPROFILE '.herdr-bridge-emu\sh-log.exe'
& $bridgeShell -c '~/.local/bin/herdr-mobile-bridge --version'
& $bridgeShell -c '~/.local/bin/herdr-mobile-bridge sessions --json'
& $bridgeShell -c '[ -S "$HOME/.config/herdr/herdr.sock" ] && echo keeper-ready'
```

Expect `herdr-mobile-bridge 0.3.3`, a JSON array of running sessions, and
`keeper-ready`. An empty array means no running sessions were discovered.

### Real CLI integration check

From the source directory, using the installed Python path:

```powershell
& $pythonExe -B tests\live_check.py
```

This creates a disposable headless `bridge-check-<random>` session and tests the
**installed** wrapper/responder: handshake, snapshot, harmless echo/readback,
reconnect deduplication, cross-session rejection, timeline, key input, workspace
close, and a fresh empty snapshot. It stops the test server afterward and writes
`build/live-check.json`. It does not target existing working panes. Herdr may
retain metadata for the stopped session.

### SSH and mobile check

From another device with an SSH client, substitute your user, host, and port:

```sh
ssh -p 22 YOUR-USER@WINDOWS-HOST '~/.local/bin/herdr-mobile-bridge --version'
ssh -p 22 YOUR-USER@WINDOWS-HOST '~/.local/bin/herdr-mobile-bridge sessions --json'
```

Then connect in the app, select a test session, check the pane list, and send/read
a harmless command in a disposable pane.

Existing verification covered 27 offline regression tests, an MSVC build with
warnings treated as errors, and the installed-wrapper integration check.
**A fresh phone-to-host SSH login and mobile UI test remain unverified.** Local
checks do not establish that your credentials or mobile UI work.

## How it works

```text
Mobile client
  | authenticated SSH exec / JSONL stdin and stdout
Windows OpenSSH Server
  | DefaultShell
sh-log.exe
  | inherited stdin/stdout/stderr
Private Git for Windows shell
  | ~/.local/bin/herdr-mobile-bridge
Python stdio-responder.py
  | reads session snapshots and bounded live pane output
  | sends explicit --session commands through Herdr 0.9.3
  +-- commands.sqlite3: mutation reservations and recorded results

snapshot-poller.py --> session discovery --> snapshot-cache.json
keeper.pl         --> ~/.config/herdr/herdr.sock compatibility probe
```

The poller queries up to four sessions concurrently, pausing 15 seconds between
refresh cycles. Cache replacement is atomic. Valid empty snapshots clear old
pane/agent data. Failed queries retain their old timestamps; snapshots older
than 60 seconds are rejected. Unknown sessions and foreign target IDs fail closed.

The keeper accepts and drops connections without forwarding RPC traffic. The
Windows Herdr CLI handles real session control. Despite its historical name,
`sh-log.exe` streams content without capturing or logging it.

Implemented requests: `hello`, `snapshot.get`, `timeline.subscribe`, `raw.read`,
`pane.sendText`, `pane.sendKeys`, `pane.fit`, `pane.unfit`, `pane.unfitAll`,
and `space.close`.
Timeline replies preserve per-pane batch/delta event shapes. `timeline.subscribe`
and `raw.read` accept an `ansi` flag for SGR-colored terminal text (plain by
default). `pane.fit` narrows one single-pane tab to a phone column width with a
notice panel beside it; `pane.unfit` closes only the panel the bridge opened,
and `pane.unfitAll` releases every recorded panel at once (fresh attaches sweep
first so killed clients leave nothing behind). The sweep also closes unrecorded
panes that print the notice line, keeping one pane per tab. `pane.unfitAll`
accepts `staleOlderThanMs` to reap only records older than the threshold, and
`pane.fitTouch` refreshes one record: viewing phones beat every minute while
the snapshot poller reaps fits unheard from for five minutes, so a killed app
leaves its panel behind only briefly. Tab lookups read the live layout first
so fast tab switches record and release the right tab. The notice runs print-then-sleep,
never a shell.
Unsupported requests receive failure replies and are not advertised as
capabilities.

## Security and retry behavior

### Access and private data

SSH is the authentication boundary. Terminal input has the Windows user's shell
authority; this bridge is not a sandbox or read-only viewer. Restrict access to
trusted clients and networks.

The installer limits bridge files, runtime, launchers, and backups to the owner,
Administrators, and SYSTEM, and rejects installation-tree reparse points. The
wrapper uses its private Git runtime. Python uses `-E -s` to ignore Python
environment variables and user site packages. Protect the Python installation
and its dependencies from modification by other local users too.

Current code does not log prompt text, terminal output, commands, or environment
values. Optional diagnostics contain fixed event labels, rotate at 256 KiB per
process file, and have bounded retention. Snapshot caches still contain private
session metadata. Historical logs/backups may contain sensitive content; review
them before sharing anything.

### Mutation keys and uncertain outcomes

SQLite atomically reserves keys across SSH processes. Each key is scoped to a
session and a hash of the operation, target, and content. Reusing it for a
different operation is rejected. Command text is not stored in the retry database.

| Outcome | Retry behavior |
| --- | --- |
| Recorded success | Returns the original success without sending again |
| Recorded definite failure | Returns the original failure |
| Text sent but Enter failed | Reports partial failure without resending text |
| Timeout, output limit, interrupted pending request, or legacy key | Outcome stays unknown; automatic replay is refused |

Exactly-once execution cannot be guaranteed across the CLI and a separate
database. Inspect the pane after an uncertain result before deciding whether to
issue a new command with a new key. Deleting the database removes duplicate
execution protection and must not be used to force retries.

Completed results expire after 24 hours; deduplication is not permanent.
Unresolved keys remain reserved. The store is capped at 100,000 entries and will
not silently discard unresolved reservations to make room. Legacy
`idempotency.json` keys are imported once as unknown outcomes.

### Limits

| Resource | Limit |
| --- | --- |
| Incoming JSONL frame | 128 KiB; oversized requests close the connection |
| Paired reply frame | 16 MiB, including JSON escaping |
| Discovered sessions | 32 |
| Panes per subscription | 64 |
| Concurrent pane-read workers | 4 per responder |
| Requested lines | Positive counts; available scrollback and captured-output budget apply |
| Send text / keys | 12,000 characters / 32 keys per operation |
| Captured pane output | 2 MiB per read |
| Ordinary CLI stdout/stderr | 2 MiB per stream |
| Snapshot capture / total cache file | 16 MiB |
| Accepted snapshot age | 60 seconds |

These are operation/process limits, not a global SSH connection quota.

## Configuration and installed files

There is no configuration file or `pip install` step.

| Setting | Default / purpose |
| --- | --- |
| Installer `-Python` | `C:\Python314\python.exe`; embedded in launchers |
| Installer `-GitSource` | `C:\Utils\Git`; copied to the private runtime |
| Installation root | `%USERPROFILE%\.herdr-bridge-emu`; fixed |
| `HERDR_BRIDGE_EXE` | Python-side real Herdr executable override |
| `HERDR_BRIDGE_HOME` | Python-side isolated-test data directory override |
| `HERDR_BRIDGE_DIAGNOSTICS=1` | Content-free diagnostic labels |

Overrides must reach the actual responder and poller processes. Setting a
variable in one PowerShell window does not update an existing poller or SSH
service. `HERDR_BRIDGE_EXE` does not change the pinned `bridge/herdr-shim`.
Changing Herdr versions requires reviewing both paths, checking protocol
compatibility, and repeating verification. `HERDR_BRIDGE_HOME` does not relocate
the wrapper, keeper, or installer and is not an authentication boundary.

```text
%USERPROFILE%/
  .herdr-bridge-emu/
    sh-log.exe                  Native SSH shell wrapper
    stdio-responder.py          Mobile protocol adapter
    bridge_common.py            Shared helpers
    command_store.py            SQLite retry handling
    snapshot-poller.py          Cache refresher
    keeper.pl / keeper2.pl      Socket helpers
    herdr-bridge-keeper.vbs     Hidden startup launcher
    runtime/Git/               Private runtime
    snapshot-cache.json        Generated session metadata
    commands.sqlite3*          Retry database and possible sidecars
    poller.lock / keeper.lock  Process-lifetime locks
    backups/<timestamp>/       Replaced files and ACL record
  .local/bin/herdr-mobile-bridge
  bin/herdr                    Herdr 0.9.3 probe shim
  .config/herdr/herdr.sock     Compatibility socket

%APPDATA%/Microsoft/Windows/Start Menu/Programs/Startup/
  herdr-bridge-keeper.vbs
```

## Updates, backups, and removal

### Update

Obtain the updated source, then use the same explicit paths as installation:

```powershell
.\scripts\build.ps1
& $pythonExe -B -m unittest discover -s tests -v
.\scripts\install.ps1 -GitSource $gitRoot -Python $pythonExe
cscript.exe //nologo $startupLauncher
& $pythonExe -B tests\live_check.py
```

Define the variables again in a new PowerShell window. Reconnect SSH/mobile
connections to load the new wrapper/responder. Reinstall after updating Git to
refresh its private copy, or after moving Python to update launcher paths.

Backups live in `%USERPROFILE%\.herdr-bridge-emu\backups\<timestamp>`.
These are **file backups, not a complete transactional rollback**. They do not
snapshot the private runtime, live database, or machine-wide SSH settings. To
revert code, rebuild/reinstall a known-good source version and preserve retry data.

### Remove

1. Disconnect bridge clients. In administrator PowerShell, restore the recorded
   `DefaultShell` value, or remove it if originally absent. Restart `sshd` before
   removing the wrapper so new SSH connections retain a working shell.
2. Remove the current user's Startup entry `herdr-bridge-keeper.vbs`.
3. Stop only this installation's poller and keeper processes. Identify them by
   their full bridge paths in Task Manager's command-line column; do not stop
   unrelated Python/Perl applications.
4. Remove `.local\bin\herdr-mobile-bridge` and `bin\herdr` under the user's
   profile, restoring backed-up preexisting files if appropriate.
5. After the keeper stops, remove its compatibility socket and bridge directory
   if no longer needed. Keep private data/backups if planning to reinstall.

Herdr, Git, Python, and OpenSSH can remain installed. Do not remove the rest of
`.config\herdr` or Herdr's own session directories as part of bridge removal.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing `vswhere`, compiler, or SDK headers | Install the C++ desktop workload, x64 tools, and Windows SDK. |
| Scripts blocked | Review/unblock the scripts and check execution policy as described above. |
| Python/Git missing | Pass absolute installation paths; Python needs `sqlite3`, Git needs shell and Perl. |
| Herdr not found / wrong version | Check the full pinned 0.9.3 release path and support files. `PATH` does not change the shim. |
| Selected session unavailable / stale snapshot | Start the selected session, check discovery and the poller, and wait for a refresh. |
| No `keeper-ready` | Create `.config\herdr`, run the Startup launcher, and check the private Perl runtime. |
| SSH refused / timed out | Check `sshd`, host address, port, firewall, network profile, and VPN routing. |
| `Permission denied (publickey)` | Check the account, authorized-keys location, administrators-group override, and permissions. |
| Local checks pass but SSH launch fails | Check `DefaultShell`, owner profile path, runtime permissions, and reconnect. |
| Host-key mismatch | Verify the fingerprint on the intended host before updating the saved key. |
| Unknown/partial command outcome | Inspect the pane before using a new key; do not delete retry data. |
| No background processes after reboot | Log in as the bridge owner and check the enabled Startup entry. |
| Reparse-point rejection | Inspect existing junctions/symlinks and use a normal local directory. |

For problem reports, include versions, the failing step, and the error. Redact
user paths, hostnames, session metadata, and terminal content. Do not upload
private keys, caches, databases, or historical logs.

## Development and publishing

### Source layout

`.gitattributes` keeps shell launchers and Perl scripts in LF format on Windows
clones, independently of the user's Git line-ending preference.

| Path | Purpose |
| --- | --- |
| [sshd/sh-log.c](sshd/sh-log.c) | Native streaming shell wrapper |
| [bridge/stdio-responder.py](bridge/stdio-responder.py) | Request validation and dispatch |
| [bridge/command_store.py](bridge/command_store.py) | Transactional retry handling |
| [bridge/bridge_common.py](bridge/bridge_common.py) | Shared paths, bounds, diagnostics, and cache checks |
| [bridge/snapshot-poller.py](bridge/snapshot-poller.py) | Per-session cache refresh |
| [bridge/herdr-mobile-bridge](bridge/herdr-mobile-bridge) | Source launcher template; installer generates deployed launcher |
| [bridge/herdr-shim](bridge/herdr-shim) | Version-pinned CLI shim |
| [keeper/](keeper/) | Compatibility socket and logon launcher |
| [scripts/build.ps1](scripts/build.ps1) | Wrapper build |
| [scripts/install.ps1](scripts/install.ps1) | Installation and backups |
| [tests/test_bridge.py](tests/test_bridge.py) | Offline regression suite |
| [tests/live_check.py](tests/live_check.py) | Installed-wrapper integration check |

`keeper2.pl` is retained as an alternate helper; the Startup launcher uses
`keeper.pl`. The unused local `sock-keeper.c` experiment is excluded from Git.

Report bugs through [GitHub Issues](https://github.com/RazvanManolache/herdr-bridge/issues).

### Before a public release

- Choose and add a `LICENSE` for your code; none has been selected in this source
  tree. Check the licensing of any copied code too.
- Review staged files. `.gitignore` excludes common logs, caches, retry data,
  executables, and `build/`; arbitrary backups/user data still need review.
- Publish source rather than the installed bridge directory, SSH configuration,
  private Git runtime, or Herdr binaries. Third-party runtimes retain their own
  licenses and distribution requirements.
- Repeat build, offline tests, integration checks, and the phone login test on
  the release version. State tested versions and remaining verification limits.

The build and installation scripts do not create repositories or publish files.

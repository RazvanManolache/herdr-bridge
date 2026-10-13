[CmdletBinding()]
param(
  [string]$Python = 'C:\Python314\python.exe',
  [string]$ListenAddress = '127.0.0.1',
  [ValidateRange(1,65535)][int]$Port = 7443,
  [switch]$InstallDependencies
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$BridgeHome = Join-Path $env:USERPROFILE '.herdr-bridge-emu'
$pairHome = Join-Path $BridgeHome 'paired'
$herdrExe = Join-Path $env:USERPROFILE '.herdr\packages\standalone\releases\0.9.3-x86_64-pc-windows-msvc\herdr.exe'
$parsedAddress = $null
if (![Net.IPAddress]::TryParse($ListenAddress, [ref]$parsedAddress) -or
    $parsedAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Use an IPv4 listen address.' }
if (!(Test-Path -LiteralPath $Python) -or !(Test-Path -LiteralPath $herdrExe)) { throw 'Install Python and the complete Herdr 0.9.3 release first.' }
if ($Python -notmatch '^[A-Za-z]:[\\/]' -or $Python.IndexOfAny([char[]]'%"&|<>^!') -ge 0 -or
    $BridgeHome.IndexOfAny([char[]]'%"&|<>^!') -ge 0) { throw 'Unsupported executable/profile path.' }
foreach ($path in @($BridgeHome, $pairHome)) {
  if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Bridge directories must not be reparse points.'
  }
}
. (Join-Path $PSScriptRoot 'private-acl.ps1')
New-Item -ItemType Directory -Force -Path $BridgeHome | Out-Null
Set-PrivateAcl $BridgeHome $true
New-Item -ItemType Directory -Force -Path $pairHome | Out-Null
Set-PrivateAcl $pairHome $true
$privateRuntime = Join-Path $BridgeHome 'paired-runtime'
if ((Test-Path -LiteralPath $privateRuntime) -and ((Get-Item -LiteralPath $privateRuntime -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw 'Private dependency runtime must not be a reparse point.'
}
New-Item -ItemType Directory -Force -Path $privateRuntime | Out-Null
Set-PrivateAcl $privateRuntime $true
$backup = Join-Path $BridgeHome ('backups\paired-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Force -Path $backup | Out-Null
# Stop only this installation's paired listener; never touch sshd or Herdr sessions.
Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and $_.CommandLine -and
  $_.CommandLine.Contains((Join-Path $BridgeHome 'paired_server.py'))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction Stop }
if ($InstallDependencies) {
  & $Python -E -s -m pip install --upgrade --target $privateRuntime -r (Join-Path $Root 'requirements-pairing.txt')
  if ($LASTEXITCODE -ne 0) { throw 'Could not install optional pairing dependencies.' }
}
& $Python -E -s -c 'import sys; sys.path.insert(0,sys.argv[1]); import cryptography, qrcode, sqlite3' $privateRuntime
if ($LASTEXITCODE -ne 0) { throw 'Run this installer with -InstallDependencies to install the optional TLS/QR packages privately.' }
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $Root 'bridge') -Filter '*.py') {
  $destination = Join-Path $BridgeHome $file.Name
  if (Test-Path -LiteralPath $destination) {
    if ((Get-Item -LiteralPath $destination -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing a reparse-point bridge file.' }
    Copy-Item -LiteralPath $destination -Destination $backup
  }
  Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
  Set-PrivateAcl $destination $false
}
$configuration = Join-Path $pairHome 'config.json'
if (Test-Path -LiteralPath $configuration) { Copy-Item -LiteralPath $configuration -Destination $backup }
@{listen=$ListenAddress;port=$Port} | ConvertTo-Json | Set-Content -LiteralPath $configuration -Encoding utf8
Set-PrivateAcl $configuration $false
$launcher = Join-Path $BridgeHome 'pair-devices.cmd'
$cmd = "@echo off`r`nchcp 65001 >nul`r`n`"$Python`" -E -s -u `"$BridgeHome\pairing_cli.py`" %*`r`n"
[IO.File]::WriteAllText($launcher, $cmd, [Text.UTF8Encoding]::new($false))
Set-PrivateAcl $launcher $false
# The listener starts at owner logon, with pairing closed until the Herdr console opens.
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\herdr-bridge-paired.vbs'
if (Test-Path -LiteralPath $startup) { Copy-Item -LiteralPath $startup -Destination $backup }
$command = '"' + $Python + '" -E -s -u "' + $BridgeHome + '\paired_server.py" --listen ' + $ListenAddress + ' --port ' + $Port
$vbs = 'CreateObject("WScript.Shell").Run "' + $command.Replace('"','""') + '", 0, False' + "`r`n"
[IO.File]::WriteAllText($startup, $vbs, [Text.Encoding]::Unicode)
Set-PrivateAcl $startup $false
& cscript.exe //nologo $startup
if ($LASTEXITCODE -ne 0) { throw 'Could not start paired listener.' }
& $Python -E -s -c 'import sys; sys.path.insert(0,sys.argv[1]); from paired_server import identity; from pairing_cli import ensure_server; ensure_server(sys.argv[2],sys.argv[2],int(sys.argv[3]),identity()[2])' $BridgeHome $ListenAddress $Port
if ($LASTEXITCODE -ne 0) { throw 'Paired listener failed its certificate-pinned startup check.' }
# Reuse an existing poller or start the protected per-user copy.
$pollerScript = Join-Path $BridgeHome 'snapshot-poller.py'
$pollers = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine.Contains($pollerScript)
})
if (!$pollers.Count) {
  Start-Process -FilePath $Python -ArgumentList @('-E','-s','-u',"`"$pollerScript`"") -WindowStyle Hidden | Out-Null
}
Write-Output "Installed paired TLS bridge on ${ListenAddress}:$Port. SSH configuration was not changed."
Write-Output "Inside Herdr, run: & '$launcher' --open-tab"
Write-Output 'No firewall rules were added. LAN use requires an organization-approved inbound rule for the chosen port.'

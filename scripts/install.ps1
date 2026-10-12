[CmdletBinding()]
param(
  [string]$BridgeHome = "$env:USERPROFILE\.herdr-bridge-emu",
  [string]$GitSource = 'C:\Utils\Git',
  [string]$Python = 'C:\Python314\python.exe'
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$BridgeHome = [IO.Path]::GetFullPath($BridgeHome)
$expected = [IO.Path]::GetFullPath("$env:USERPROFILE\.herdr-bridge-emu")
if ($BridgeHome -ne $expected) { throw 'This installer only updates the current user bridge installation.' }
if (!(Test-Path -LiteralPath $Python) -or !(Test-Path -LiteralPath "$Root\build\sh-log.exe")) {
  throw 'Python and build\sh-log.exe must exist before installation.'
}
if (!(Test-Path -LiteralPath "$GitSource\bin\sh.exe")) { throw 'Git runtime source is missing.' }
if ((Test-Path -LiteralPath $BridgeHome) -and
    ((Get-Item -LiteralPath $BridgeHome -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw 'Bridge installation root must not be a reparse point.'
}

$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
function Set-PrivateAcl([string]$Path, [bool]$Directory) {
  $allowed = @($owner.Value, 'S-1-5-18', 'S-1-5-32-544')
  $current = Get-Acl -LiteralPath $Path
  $expectedInheritance = if ($Directory) { 'ContainerInherit, ObjectInherit' } else { 'None' }
  $rules = @($current.Access)
  $alreadyPrivate = $current.AreAccessRulesProtected -and $rules.Count -eq 3
  foreach ($rule in $rules) {
    $identity = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($identity -notin $allowed -or $rule.FileSystemRights -ne 'FullControl' -or
        $rule.AccessControlType -ne 'Allow' -or $rule.InheritanceFlags.ToString() -ne $expectedInheritance) {
      $alreadyPrivate = $false
    }
  }
  # Some Windows/PowerShell versions request SACL privileges for an unchanged
  # descriptor. Avoid writing it when its effective private ACL is already exact.
  if ($alreadyPrivate) { return }
  if ($Directory) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
  } else {
    $acl = [Security.AccessControl.FileSecurity]::new()
    $inherit = [Security.AccessControl.InheritanceFlags]::None
  }
  $acl.SetOwner($owner)
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($sid in @($owner, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
                    [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', $inherit, 'None', 'Allow')
    $acl.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $Path -AclObject $acl
}

New-Item -ItemType Directory -Path $BridgeHome -Force | Out-Null
$tag = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backup = Join-Path $BridgeHome "backups\$tag"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
(Get-Acl -LiteralPath $BridgeHome).Sddl | Set-Content -LiteralPath "$backup\original-acl.txt"
Set-PrivateAcl $BridgeHome $true
# Remove explicit sandbox grants on legacy children too. The tree is private,
# contains no reparse points, and is the exact current user's installation.
$reparse = Get-ChildItem -LiteralPath $BridgeHome -Recurse -Force |
  Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
if ($reparse) { throw 'Unexpected reparse point in bridge tree; refusing recursive ACL reset.' }
foreach ($child in Get-ChildItem -LiteralPath $BridgeHome -Force) {
  & icacls.exe $child.FullName /reset /T /C /Q | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Could not reset inherited private permissions: $($child.Name)" }
}

$runtime = Join-Path $BridgeHome 'runtime\Git'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
& robocopy.exe $GitSource $runtime /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw 'Could not copy private Git runtime.' }
if (!(Test-Path -LiteralPath "$runtime\bin\sh.exe")) { throw 'Private shell missing.' }

# Stop only the known cache poller, never unrelated Python applications.
$pollers = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and $_.CommandLine -and
  $_.CommandLine.Contains((Join-Path $BridgeHome 'snapshot-poller.py'))
})
foreach ($process in $pollers) {
  Stop-Process -Id $process.ProcessId -ErrorAction Stop
  Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
}

foreach ($name in @('stdio-responder.py', 'snapshot-poller.py', 'bridge_common.py', 'command_store.py', 'sh-log.c', 'keeper.pl', 'keeper2.pl')) {
  $installed = Join-Path $BridgeHome $name
  if (Test-Path -LiteralPath $installed) { Copy-Item -LiteralPath $installed -Destination $backup }
}
foreach ($file in Get-ChildItem -LiteralPath "$Root\bridge" -Filter '*.py') {
  Copy-Item -LiteralPath $file.FullName -Destination $BridgeHome -Force
}
Copy-Item -LiteralPath "$Root\sshd\sh-log.c" -Destination "$BridgeHome\sh-log.c" -Force
Copy-Item -LiteralPath "$Root\keeper\keeper.pl" -Destination "$BridgeHome\keeper.pl" -Force
Copy-Item -LiteralPath "$Root\keeper\keeper2.pl" -Destination "$BridgeHome\keeper2.pl" -Force
Copy-Item -LiteralPath "$Root\keeper\herdr-bridge-keeper.vbs" -Destination "$BridgeHome\herdr-bridge-keeper.vbs" -Force
$startupText = [IO.File]::ReadAllText("$Root\keeper\herdr-bridge-keeper.vbs").Replace('C:\Python314\python.exe', $Python)
[IO.File]::WriteAllText("$BridgeHome\herdr-bridge-keeper.vbs", $startupText, [Text.UTF8Encoding]::new($false))
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\herdr-bridge-keeper.vbs'
if (Test-Path -LiteralPath $startup) {
  Copy-Item -LiteralPath $startup -Destination "$backup\startup-keeper.vbs"
}
Copy-Item -LiteralPath "$BridgeHome\herdr-bridge-keeper.vbs" -Destination $startup -Force
Set-PrivateAcl $startup $false
$shim = Join-Path $env:USERPROFILE 'bin\herdr'
if (Test-Path -LiteralPath $shim) { Copy-Item -LiteralPath $shim -Destination "$backup\herdr-shim" }
New-Item -ItemType Directory -Path (Split-Path $shim -Parent) -Force | Out-Null
Copy-Item -LiteralPath "$Root\bridge\herdr-shim" -Destination $shim -Force
Set-PrivateAcl $shim $false

$entry = Join-Path $env:USERPROFILE '.local\bin\herdr-mobile-bridge'
$pythonPosix = '/' + $Python.Substring(0, 1).ToLowerInvariant() + $Python.Substring(2).Replace('\', '/')
if ($pythonPosix.Contains("'")) { throw 'Python path must not contain a single quote.' }
$launcher = @'
#!/bin/sh
exec '@PYTHON@' -E -s -u "$HOME/.herdr-bridge-emu/stdio-responder.py" "$@"
'@
$launcher = $launcher.Replace('@PYTHON@', $pythonPosix).Replace("`r`n", "`n") + "`n"
if (Test-Path -LiteralPath $entry) { Copy-Item -LiteralPath $entry -Destination "$backup\herdr-mobile-bridge" }
New-Item -ItemType Directory -Path (Split-Path $entry -Parent) -Force | Out-Null
[IO.File]::WriteAllText($entry, $launcher, [Text.UTF8Encoding]::new($false))
Set-PrivateAcl $entry $false

# Rename the mapped old executable rather than overwriting a running image.
$wrapper = Join-Path $BridgeHome 'sh-log.exe'
$next = Join-Path $BridgeHome 'sh-log.next.exe'
Copy-Item -LiteralPath "$Root\build\sh-log.exe" -Destination $next -Force
if (Test-Path -LiteralPath $wrapper) { Move-Item -LiteralPath $wrapper -Destination "$backup\sh-log.exe" }
try { Move-Item -LiteralPath $next -Destination $wrapper }
catch {
  if (Test-Path -LiteralPath "$backup\sh-log.exe") {
    Move-Item -LiteralPath "$backup\sh-log.exe" -Destination $wrapper
  }
  throw
}

# Refresh before starting a detached poller, to verify the real CLI/cache contract.
& $Python -E -s -B -c "import sys; sys.path.insert(0, sys.argv[1]); import importlib.util; s=importlib.util.spec_from_file_location('poller',sys.argv[1]+'/snapshot-poller.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.refresh()" $BridgeHome
if ($LASTEXITCODE -ne 0) { throw 'Initial cache refresh failed.' }
$scriptPath = Join-Path $BridgeHome 'snapshot-poller.py'
$process = Start-Process -FilePath $Python -ArgumentList @('-E', '-s', '-B', '-u', "`"$scriptPath`"") -WindowStyle Hidden -PassThru
Write-Output "Installed bridge; cache poller pid $($process.Id)"
Write-Output "Private runtime: $runtime"
Write-Output "Backup: $backup"
Write-Output 'Existing SSH connections must reconnect to load the updated responder and shell.'

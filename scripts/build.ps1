$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$installation) { throw 'MSVC C++ build tools are required.' }
$vcvars = Join-Path $installation 'VC\Auxiliary\Build\vcvars64.bat'
$compilerEnv = & cmd.exe /c "`"$vcvars`" >nul && set"
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize MSVC.' }
foreach ($entry in $compilerEnv) {
  if ($entry -match '^([^=]+)=(.*)$') { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] }
}
New-Item -ItemType Directory -Path "$root\build" -Force | Out-Null
& cl.exe /nologo /O2 /W4 /WX /DUNICODE /D_UNICODE "$root\sshd\sh-log.c" "/Fe$root\build\sh-log.exe" "/Fo$root\build\sh-log.obj"
if ($LASTEXITCODE -ne 0) { throw 'SSH wrapper compilation failed.' }

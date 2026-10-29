[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][string]$ProfileDirectory,
    [Parameter(Mandatory = $true)][string]$RunId,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [ValidateRange(0.001, 0.25)][double]$PollSeconds = 0.25,
    [string]$Bootstrap
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'WINDOWS_REQUIRED'
}
foreach ($watchdogPath in @($StateRoot, $ProfileDirectory, $PythonExe)) {
    if (-not [System.IO.Path]::IsPathRooted($watchdogPath) -or $watchdogPath.StartsWith('\\')) {
        throw 'ABSOLUTE_LOCAL_PATH_REQUIRED'
    }
}
if (-not (Test-Path -LiteralPath $StateRoot -PathType Container) -or
    -not (Test-Path -LiteralPath $ProfileDirectory -PathType Container) -or
    -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'PRECREATED_PRIVATE_DIRECTORIES_AND_PYTHON_REQUIRED'
}
if ([string]::IsNullOrWhiteSpace($RunId) -or $RunId.Length -gt 1024 -or $RunId -match '[\x00-\x1f\x7f]') {
    throw 'RUN_ID_INVALID'
}
if ($Bootstrap -and $Bootstrap -notmatch '^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$') {
    throw 'BOOTSTRAP_INVALID'
}

$watchdogPackage = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$watchdogPython = (Get-Item -LiteralPath $PythonExe).FullName
$watchdogPaths = @(
    (Join-Path $watchdogPackage 'code'),
    (Join-Path $watchdogPackage 'packages\cdpd\src'),
    (Join-Path $watchdogPackage 'packages\chatgpt-web-api\src'),
    (Join-Path $watchdogPackage 'packages\cdpd-chatgpt-adapter\src')
)
foreach ($watchdogModulePath in $watchdogPaths) {
    if (-not (Test-Path -LiteralPath $watchdogModulePath -PathType Container)) {
        throw 'CANDIDATE_LAYOUT_INVALID'
    }
}

$watchdogArguments = @(
    '-B', '-m', 'cdpd_chatgpt_profile.supervisor',
    '--root', $StateRoot,
    '--profile-directory', $ProfileDirectory,
    '--run-id', $RunId,
    '--poll-seconds', $PollSeconds.ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
)
if ($Bootstrap) {
    $watchdogArguments += @('--bootstrap', $Bootstrap)
}

# Run in this explicitly opened independent console.  A subprocess launched
# from an arbitrary Consumer/IDE Job Object is not made independent by merely
# using Start-Process.  The Python startup gate rejects inherited job ownership.
# This script does not change ACLs, launch Chrome, register Scheduler tasks or
# reconstruct live permits from saved files.
$watchdogOldPythonPath = $env:PYTHONPATH
$watchdogOldBytecode = $env:PYTHONDONTWRITEBYTECODE
$watchdogExitCode = 2
try {
    $env:PYTHONPATH = $watchdogPaths -join [System.IO.Path]::PathSeparator
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $watchdogPython @watchdogArguments
    $watchdogExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $watchdogOldPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $watchdogOldBytecode
}
exit $watchdogExitCode

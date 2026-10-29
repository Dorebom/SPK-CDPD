<#
.SYNOPSIS
Prepare an optional CDPD L08 Task Scheduler definition. Default: no registration.
.DESCRIPTION
The fixed sibling run_l08_monitor.py owns configuration validation and the
OFFLINE_REPORT / SCHEDULED_CAPTURE boundary. This script neither executes that
entrypoint nor interprets saved idle/browser facts as runtime authority.

-Register is the explicit registration operation. Registration is disabled by
default; -EnableAutomaticStart also requires -Register. No immediate-start
operation is provided. -WhatIf prevents all Task Scheduler API calls.

Use a future FirstRunAt and call a 30-day interval "every 30 days", not monthly.
The emitted definition contains local paths; it is an operator plan, not the
privacy-filtered diagnostic report.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('\A[0-9a-f]{64}\z')][string]$ConfigSha256,
    [Parameter(Mandatory = $true)][datetime]$FirstRunAt,
    [ValidateRange(1, 365)][int]$DaysInterval = 30,
    [ValidatePattern('\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\z')]
    [string]$TaskName = 'CDPD-L08-Monitor',
    [switch]$Register,
    [switch]$EnableAutomaticStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($EnableAutomaticStart -and -not $Register) {
    throw 'ENABLE_REQUIRES_EXPLICIT_REGISTER'
}
if ($env:OS -ne 'Windows_NT') {
    throw 'WINDOWS_REQUIRED'
}

function Resolve-L08RegularFile {
    param([string]$Value)
    # Reject UNC/device paths, drive-relative paths, ADS and wildcard/control
    # characters. Resolve literal local files only; no content is executed.
    if ([string]::IsNullOrWhiteSpace($Value) -or
        $Value -notmatch '^[A-Za-z]:[\\/]' -or
        $Value -match '[\x00-\x1f\x7f"<>|?*]' -or
        $Value.Substring(2).Contains(':')) {
        throw 'ABSOLUTE_LOCAL_REGULAR_FILE_REQUIRED'
    }
    $segments = $Value.Substring(3).Replace('/', '\').Split('\')
    foreach ($segment in $segments) {
        if ($segment -eq '.' -or $segment -eq '..' -or
            $segment.Length -eq 0 -or $segment.EndsWith('.') -or
            $segment.EndsWith(' ')) {
            throw 'NONCANONICAL_LOCAL_PATH'
        }
    }
    $fullPath = [System.IO.Path]::GetFullPath($Value)
    $file = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
    if ($file.PSProvider.Name -ne 'FileSystem' -or $file.PSIsContainer -or
        $file -isnot [System.IO.FileInfo] -or $file.Length -le 0) {
        throw 'NONEMPTY_REGULAR_FILE_REQUIRED'
    }
    $component = $file
    while ($null -ne $component) {
        if (($component.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'REPARSE_PATH_NOT_ACCEPTED'
        }
        if ($component -is [System.IO.FileInfo]) {
            $component = $component.Directory
        } else {
            $component = $component.Parent
        }
    }
    return $file.FullName
}

function ConvertTo-L08NativeArgument {
    param([string]$Value)
    # The admitted paths cannot contain quotes or controls. Doubling trailing
    # backslashes still preserves the Windows C-runtime argument convention.
    if ($null -eq $Value -or $Value -match '[\x00-\x1f\x7f"]') {
        throw 'NATIVE_ARGUMENT_INVALID'
    }
    $tailLength = $Value.Length - $Value.TrimEnd([char]92).Length
    return '"' + $Value + (('\' * $tailLength) -join '') + '"'
}

$monitorPython = Resolve-L08RegularFile $PythonExe
if ([System.IO.Path]::GetExtension($monitorPython) -ine '.exe') {
    throw 'PYTHON_EXECUTABLE_REQUIRED'
}
$monitorConfig = Resolve-L08RegularFile $ConfigPath
if ([System.IO.Path]::GetExtension($monitorConfig) -ine '.json') {
    throw 'JSON_CONFIGURATION_REQUIRED'
}
$monitorEntry = Resolve-L08RegularFile (Join-Path $PSScriptRoot 'run_l08_monitor.py')
$monitorDirectory = [System.IO.Directory]::GetParent($PSScriptRoot).FullName
$monitorObservedHash = (Get-FileHash -LiteralPath $monitorConfig -Algorithm SHA256).Hash.ToLowerInvariant()
if ($monitorObservedHash -cne $ConfigSha256) {
    throw 'CONFIGURATION_SHA256_MISMATCH'
}

# Direct native process arguments: no shell, -Command, arbitrary module, or
# configuration-driven adapter loading. -B avoids bytecode writes.
$monitorArgumentTokens = @(
    '-B', $monitorEntry, '--config', $monitorConfig,
    '--expected-config-sha256', $ConfigSha256
)
$monitorArguments = ($monitorArgumentTokens | ForEach-Object {
    ConvertTo-L08NativeArgument $_
}) -join ' '
$monitorFirstRun = $FirstRunAt
if ($monitorFirstRun.Kind -eq [System.DateTimeKind]::Utc) {
    $monitorFirstRun = $monitorFirstRun.ToLocalTime()
}
$monitorDisabled = -not [bool]$EnableAutomaticStart
$monitorPlan = [ordered]@{
    schema = 'cdpd.l08.scheduler-plan/v1'
    status = 'DEFINITION_ONLY'
    scheduler_registered = $false
    task_name = $TaskName
    task_path = '\'
    action = [ordered]@{
        execute = $monitorPython
        argument_tokens = $monitorArgumentTokens
        arguments = $monitorArguments
        working_directory = $monitorDirectory
    }
    configuration_sha256 = $ConfigSha256
    trigger = [ordered]@{
        kind = 'DAILY_INTERVAL'
        first_run_local = $monitorFirstRun.ToString('s', [System.Globalization.CultureInfo]::InvariantCulture)
        days_interval = $DaysInterval
    }
    principal = [ordered]@{
        user = 'CURRENT_USER_AT_REGISTRATION'
        logon_type = 'Interactive'
        run_level = 'Limited'
    }
    settings = [ordered]@{
        disabled = $monitorDisabled
        multiple_instances = 'IgnoreNew'
        restart_on_failure = $false
        start_when_available = $false
        wake_to_run = $false
        execution_time_limit_seconds = 300
    }
    semantics = 'FIXED_CONFIG_OFFLINE_REPLAY_OR_FAIL_CLOSED_SCHEDULED_CAPTURE'
}
# No Task Scheduler cmdlet, native process, or browser is used above this guard.
if (-not $Register) {
    Write-Output ($monitorPlan | ConvertTo-Json -Depth 6)
    return
}
if ($monitorFirstRun -le [datetime]::Now.AddMinutes(1)) {
    throw 'FUTURE_START_AT_LEAST_ONE_MINUTE_REQUIRED'
}
$monitorOperation = 'Register disabled CDPD L08 task'
if ($EnableAutomaticStart) {
    $monitorOperation = 'Register enabled CDPD L08 task for future automatic execution'
}
if (-not $PSCmdlet.ShouldProcess($TaskName, $monitorOperation)) {
    Write-Output ($monitorPlan | ConvertTo-Json -Depth 6)
    return
}

# Read all tasks rather than treating a missing-name lookup or service error as
# permission to overwrite. Names admitted above contain no wildcard characters.
$monitorExisting = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
    $_.TaskPath -eq '\' -and $_.TaskName -ieq $TaskName
})
if ($monitorExisting.Count -ne 0) {
    throw 'EXISTING_TASK_NOT_OVERWRITTEN'
}
# The entrypoint repeats the pin check at every future invocation. Changing this
# configuration after registration invalidates that invocation; it is not a new
# capture authorization.
$monitorConfig = Resolve-L08RegularFile $monitorConfig
if ((Get-FileHash -LiteralPath $monitorConfig -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ConfigSha256) {
    throw 'CONFIGURATION_CHANGED_BEFORE_REGISTRATION'
}
$monitorUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$monitorAction = New-ScheduledTaskAction -Execute $monitorPython -Argument $monitorArguments -WorkingDirectory $monitorDirectory
$monitorTrigger = New-ScheduledTaskTrigger -Daily -At $monitorFirstRun -DaysInterval $DaysInterval
$monitorPrincipal = New-ScheduledTaskPrincipal -UserId $monitorUser -LogonType Interactive -RunLevel Limited
# Omit RestartCount/RestartInterval entirely: RestartOnFailure/Count=0 is not a
# valid XML representation of disabled restart (the schema minimum is one).
$monitorSettings = New-ScheduledTaskSettingsSet -Disable:$monitorDisabled -MultipleInstances IgnoreNew -StartWhenAvailable:$false -WakeToRun:$false -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$monitorRegistered = Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $monitorAction -Trigger $monitorTrigger -Principal $monitorPrincipal -Settings $monitorSettings -Description 'CDPD L08 fixed configuration; no automatic baseline adoption; no retry.' -ErrorAction Stop
Write-Output ([ordered]@{
    schema = 'cdpd.l08.scheduler-registration/v1'
    status = 'REGISTERED'
    scheduler_registered = $true
    task_name = $TaskName
    disabled = $monitorDisabled
    immediate_start_requested = $false
    configuration_sha256 = $ConfigSha256
} | ConvertTo-Json -Depth 3)

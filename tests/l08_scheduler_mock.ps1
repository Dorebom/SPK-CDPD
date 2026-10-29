<#
Self-contained mock-only test harness, run later on Windows PowerShell 5.1+.
No Task Scheduler service operation or native process launch is performed.
This harness has NOT_RUN status until its own output is collected. Even PASS
would prove mocked permission flow only, not actual registration or startup.
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Output '{"status":"NOT_RUN","reason":"WINDOWS_POWERSHELL_REQUIRED","scope":"MOCK_ONLY_NO_NATIVE_REGISTRATION"}'
    exit 0
}
$script:Calls = @{ Register = 0; Get = 0; New = 0; Start = 0 }
$script:Existing = $false
$script:LastAction = $null
$script:LastSettings = $null
$script:LastPrincipal = $null
$script:LastTrigger = $null
$script:Results = New-Object System.Collections.Generic.List[object]

function Assert-L08 {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}
function Reset-L08Mock {
    $script:Calls = @{ Register = 0; Get = 0; New = 0; Start = 0 }
    $script:Existing = $false
}
function Get-ScheduledTask {
    [CmdletBinding()]param()
    $script:Calls.Get++
    if ($script:Existing) {
        return [pscustomobject]@{ TaskName = 'CDPD-L08-Mock'; TaskPath = '\' }
    }
    return @()
}
function New-ScheduledTaskAction {
    [CmdletBinding()]param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    $script:Calls.New++
    $script:LastAction = [pscustomobject]@{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
    return $script:LastAction
}
function New-ScheduledTaskTrigger {
    [CmdletBinding()]param([switch]$Daily, [datetime]$At, [uint32]$DaysInterval)
    $script:Calls.New++
    $script:LastTrigger = [pscustomobject]@{ Daily = [bool]$Daily; At = $At; DaysInterval = $DaysInterval }
    return $script:LastTrigger
}
function New-ScheduledTaskPrincipal {
    [CmdletBinding()]param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    $script:Calls.New++
    $script:LastPrincipal = [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
    return $script:LastPrincipal
}
function New-ScheduledTaskSettingsSet {
    [CmdletBinding()]param([switch]$Disable, [string]$MultipleInstances, [switch]$StartWhenAvailable, [switch]$WakeToRun, [timespan]$ExecutionTimeLimit)
    $script:Calls.New++
    $script:LastSettings = [pscustomobject]@{
        Disable = [bool]$Disable; MultipleInstances = $MultipleInstances
        StartWhenAvailable = [bool]$StartWhenAvailable; WakeToRun = [bool]$WakeToRun
        ExecutionTimeLimit = $ExecutionTimeLimit
    }
    return $script:LastSettings
}
function Register-ScheduledTask {
    [CmdletBinding()]param([string]$TaskName, [string]$TaskPath, $Action, $Trigger, $Principal, $Settings, [string]$Description)
    $script:Calls.Register++
    if ($script:Existing) { throw 'MOCK_EXISTING_TASK' }
    return [pscustomobject]@{ TaskName = $TaskName; State = 'MOCK_ONLY' }
}
function Start-ScheduledTask {
    [CmdletBinding()]param([string]$TaskName)
    $script:Calls.Start++
    throw 'UNEXPECTED_START'
}
function Start-Process {
    [CmdletBinding()]param([string]$FilePath)
    $script:Calls.Start++
    throw 'UNEXPECTED_PROCESS_START'
}
function Invoke-L08Case {
    param([string]$Name, [scriptblock]$Body)
    Reset-L08Mock
    try {
        & $Body
        $script:Results.Add([ordered]@{ id = $Name; status = 'PASS' })
    } catch {
        # Emit the test ID and exception type only; no arbitrary fixture text.
        $script:Results.Add([ordered]@{ id = $Name; status = 'FAIL'; exception_type = $_.Exception.GetType().FullName })
    }
}

$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('cdpd-l08-scheduler-mock-' + [guid]::NewGuid().ToString('N'))
$fixtureScripts = Join-Path $fixtureRoot 'scripts'
try {
    [void][System.IO.Directory]::CreateDirectory($fixtureScripts)
    $fixtureRegistrar = Join-Path $fixtureScripts 'register_monitor_task.ps1'
    [System.IO.File]::Copy((Join-Path $PSScriptRoot '..\scripts\register_monitor_task.ps1'), $fixtureRegistrar, $false)
    [System.IO.File]::WriteAllText((Join-Path $fixtureScripts 'run_l08_monitor.py'), '# mock entrypoint: never executed')
    $fixturePython = Join-Path $fixtureRoot 'python.exe'
    [System.IO.File]::WriteAllText($fixturePython, 'not executable; launching this is a test failure')
    $fixtureConfig = Join-Path $fixtureRoot ('config (space) & $literal ' + [char]0x65E5 + '.json')
    [System.IO.File]::WriteAllText($fixtureConfig, '{"schema":"mock","note":"data is not command authority"}')
    $fixtureHash = (Get-FileHash -LiteralPath $fixtureConfig -Algorithm SHA256).Hash.ToLowerInvariant()
    $fixtureArgs = @{
        PythonExe = $fixturePython; ConfigPath = $fixtureConfig; ConfigSha256 = $fixtureHash
        FirstRunAt = [datetime]::Now.AddHours(2); DaysInterval = 30; TaskName = 'CDPD-L08-Mock'
    }

    Invoke-L08Case 'SCHED-MOCK-01-default-no-service-call' {
        $plan = (& $fixtureRegistrar @fixtureArgs | Out-String | ConvertFrom-Json)
        Assert-L08 ($script:Calls.Register -eq 0) 'default registration attempted'
        Assert-L08 ($script:Calls.Get -eq 0 -and $script:Calls.New -eq 0) 'default service API attempted'
        Assert-L08 ($plan.status -eq 'DEFINITION_ONLY' -and $plan.settings.disabled) 'default definition not disabled'
        Assert-L08 (-not $plan.scheduler_registered) 'definition falsely claims registered'
        Assert-L08 ($plan.action.argument_tokens[3] -ceq $fixtureConfig) 'config token changed'
    }
    Invoke-L08Case 'SCHED-MOCK-02-whatif-no-service-call' {
        $null = & $fixtureRegistrar @fixtureArgs -Register -WhatIf
        Assert-L08 ($script:Calls.Register -eq 0) 'WhatIf registered task'
        Assert-L08 ($script:Calls.Get -eq 0 -and $script:Calls.New -eq 0) 'WhatIf called Scheduler'
    }
    Invoke-L08Case 'SCHED-MOCK-03-enable-without-register-rejected' {
        $rejected = $false
        try { $null = & $fixtureRegistrar @fixtureArgs -EnableAutomaticStart } catch { $rejected = $_.Exception.Message -eq 'ENABLE_REQUIRES_EXPLICIT_REGISTER' }
        Assert-L08 $rejected 'enable-only request accepted'
        Assert-L08 ($script:Calls.Register -eq 0) 'enable-only registered task'
    }
    Invoke-L08Case 'SCHED-MOCK-04-register-disabled' {
        $null = & $fixtureRegistrar @fixtureArgs -Register -Confirm:$false
        Assert-L08 ($script:Calls.Register -eq 1 -and $script:Calls.Start -eq 0) 'incorrect registration/start calls'
        Assert-L08 $script:LastSettings.Disable 'registered enabled by default'
        Assert-L08 ($script:LastSettings.MultipleInstances -eq 'IgnoreNew') 'overlap not ignored'
        Assert-L08 (-not $script:LastSettings.StartWhenAvailable -and -not $script:LastSettings.WakeToRun) 'catch-up/wake enabled'
        Assert-L08 ($script:LastPrincipal.LogonType -eq 'Interactive' -and $script:LastPrincipal.RunLevel -eq 'Limited') 'principal contract changed'
        Assert-L08 ($script:LastTrigger.Daily -and $script:LastTrigger.DaysInterval -eq 30) 'interval contract changed'
        Assert-L08 ($script:LastAction.Argument.Contains('"' + $fixtureConfig + '"')) 'config argument not quoted as data'
    }
    Invoke-L08Case 'SCHED-MOCK-05-explicit-enabled-registration-no-start' {
        $null = & $fixtureRegistrar @fixtureArgs -Register -EnableAutomaticStart -Confirm:$false
        Assert-L08 ($script:Calls.Register -eq 1 -and $script:Calls.Start -eq 0) 'incorrect enabled registration/start calls'
        Assert-L08 (-not $script:LastSettings.Disable) 'explicit enable ignored'
    }
    Invoke-L08Case 'SCHED-MOCK-06-existing-task-no-overwrite' {
        $script:Existing = $true
        $rejected = $false
        try { $null = & $fixtureRegistrar @fixtureArgs -Register -Confirm:$false } catch { $rejected = $_.Exception.Message -eq 'EXISTING_TASK_NOT_OVERWRITTEN' }
        Assert-L08 $rejected 'existing task accepted'
        Assert-L08 ($script:Calls.Register -eq 0) 'existing task overwritten'
    }
    Invoke-L08Case 'SCHED-MOCK-07-hash-mismatch-no-service-call' {
        $badArgs = $fixtureArgs.Clone()
        $badArgs.ConfigSha256 = ('0' * 64)
        $rejected = $false
        try { $null = & $fixtureRegistrar @badArgs -Register -Confirm:$false } catch { $rejected = $_.Exception.Message -eq 'CONFIGURATION_SHA256_MISMATCH' }
        Assert-L08 $rejected 'wrong hash accepted'
        Assert-L08 ($script:Calls.Register -eq 0 -and $script:Calls.Get -eq 0) 'wrong hash reached service'
    }
    Invoke-L08Case 'SCHED-MOCK-08-nonlocal-path-rejected' {
        $badArgs = $fixtureArgs.Clone()
        $badArgs.ConfigPath = '\\server\share\config.json'
        $rejected = $false
        try { $null = & $fixtureRegistrar @badArgs } catch { $rejected = $_.Exception.Message -eq 'ABSOLUTE_LOCAL_REGULAR_FILE_REQUIRED' }
        Assert-L08 $rejected 'UNC path accepted'
        Assert-L08 ($script:Calls.Register -eq 0) 'UNC path reached registration'
    }
    Invoke-L08Case 'SCHED-MOCK-09-past-time-no-service-call' {
        $badArgs = $fixtureArgs.Clone()
        $badArgs.FirstRunAt = [datetime]::Now.AddMinutes(-1)
        $rejected = $false
        try { $null = & $fixtureRegistrar @badArgs -Register -Confirm:$false } catch { $rejected = $_.Exception.Message -eq 'FUTURE_START_AT_LEAST_ONE_MINUTE_REQUIRED' }
        Assert-L08 $rejected 'past time registered'
        Assert-L08 ($script:Calls.Register -eq 0 -and $script:Calls.Get -eq 0) 'past time reached service'
    }
    Invoke-L08Case 'SCHED-MOCK-10-task-name-terminal-LF-rejected' {
        $badArgs = $fixtureArgs.Clone()
        $badArgs.TaskName = 'CDPD-L08-Mock' + [char]10
        $rejected = $false
        try { $null = & $fixtureRegistrar @badArgs -Register -Confirm:$false } catch { $rejected = $_.Exception -is [System.Management.Automation.ParameterBindingException] }
        Assert-L08 $rejected 'task name terminal LF accepted'
        Assert-L08 ($script:Calls.Register -eq 0 -and $script:Calls.Get -eq 0) 'task name terminal LF reached service'
    }
    Invoke-L08Case 'SCHED-MOCK-11-hash-terminal-LF-rejected' {
        $badArgs = $fixtureArgs.Clone()
        $badArgs.ConfigSha256 = $fixtureHash + [char]10
        $rejected = $false
        try { $null = & $fixtureRegistrar @badArgs -Register -Confirm:$false } catch { $rejected = $_.Exception -is [System.Management.Automation.ParameterBindingException] }
        Assert-L08 $rejected 'hash terminal LF accepted'
        Assert-L08 ($script:Calls.Register -eq 0 -and $script:Calls.Get -eq 0) 'hash terminal LF reached service'
    }
} finally {
    if ([System.IO.Directory]::Exists($fixtureRoot)) {
        [System.IO.Directory]::Delete($fixtureRoot, $true)
    }
}
$failureCount = @($script:Results | Where-Object { $_.status -eq 'FAIL' }).Count
Write-Output ([ordered]@{
    schema = 'cdpd.l08.scheduler-mock-results/v1'
    scope = 'MOCK_ONLY_NO_NATIVE_REGISTRATION'
    status = $(if ($failureCount -eq 0) { 'PASS' } else { 'FAIL' })
    results = $script:Results.ToArray()
    native_registration = 'NOT_RUN'
    automatic_start = 'NOT_RUN'
} | ConvertTo-Json -Depth 5)
if ($failureCount -ne 0) { exit 1 }
exit 0

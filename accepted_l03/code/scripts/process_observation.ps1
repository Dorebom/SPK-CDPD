<# CDPD-L03 FIX005 Candidate: shared, bounded process snapshot resolution.
Definitions only. No launch, HTTP/CDP, privilege change, process memory read,
or protected-tree write. Optional native type compilation may use OS temporary files. A direct CIM Get is performed at most once for a process
whose enumerate result lacks required metadata. A result is never merged across
PID, creation time, image name or session. The supplemental native probe cannot
supply launch authority or fill a missing CommandLine.
#>
function Get-CdpdMetaField([object] $Row, [string] $Name) {
    if ($null -eq $Row) { return $null }
    if ($Row -is [Collections.IDictionary]) {
        if ($Row.Contains($Name)) { return $Row[$Name] }
        return $null
    }
    if ($Row -is [Microsoft.Management.Infrastructure.CimInstance]) {
        $native = $Row.CimInstanceProperties[$Name]
        if ($null -eq $native) { return $null }
        return $native.Value
    }
    $p = $Row.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}
function Test-CdpdMetaText([object] $Value) {
    return ($Value -is [string] -and -not [string]::IsNullOrWhiteSpace($Value) -and $Value.Length -le 32767)
}
function Get-CdpdMetaIdentity([object] $Row) {
    $p = Get-CdpdMetaField $Row 'ProcessId'
    $n = Get-CdpdMetaField $Row 'Name'
    $c = Get-CdpdMetaField $Row 'CreationDate'
    $s = Get-CdpdMetaField $Row 'SessionId'
    if ($null -eq $p -or $p -is [bool] -or $p -is [string] -or
        ($p -isnot [int] -and $p -isnot [uint32] -and $p -isnot [long])) { return $null }
    if ([long]$p -lt 1 -or [long]$p -gt 2147483647 -or
        $n -isnot [string] -or $n -inotmatch '^(chrome|chromium)\.exe$' -or
        $c -isnot [DateTime] -or
        $null -eq $s -or $s -is [bool] -or $s -is [string] -or
        ($s -isnot [int] -and $s -isnot [uint32] -and $s -isnot [long]) -or
        [long]$s -lt 0 -or [long]$s -gt 2147483647) { return $null }
    $handle = Get-CdpdMetaField $Row 'Handle'
    if ($null -eq $handle -or ([string]$handle) -cne ([string]$p)) { return $null }
    return [pscustomobject]@{ProcessId=[int]$p; Name=$n.ToLowerInvariant(); CreatedUtcTicks=$c.ToUniversalTime().Ticks; SessionId=[long]$s}
}
function Test-CdpdSameMetaIdentity([object] $First, [object] $Second) {
    return ($null -ne $First -and $null -ne $Second -and
        $First.ProcessId -eq $Second.ProcessId -and $First.Name -ceq $Second.Name -and
        $First.CreatedUtcTicks -eq $Second.CreatedUtcTicks -and $First.SessionId -eq $Second.SessionId)
}
function Invoke-CdpdDirectProcessGet([object] $Row) {
    # Microsoft-documented CIM Get, not another broad enumeration or a different
    # user/provider. Do not add -Property: InputObject has a different parameter set.
    Get-CimInstance -InputObject $Row -OperationTimeoutSec 1 -ErrorAction Stop
}
function Get-CdpdMetadataClock { return [Diagnostics.Stopwatch]::StartNew() }
function Resolve-CdpdProcessMetadata([object[]] $Rows, [int] $MaxDirectGets = 64, [int] $BudgetMilliseconds = 8000) {
    if ($MaxDirectGets -lt 0 -or $MaxDirectGets -gt 64 -or $BudgetMilliseconds -lt 1 -or $BudgetMilliseconds -gt 8000) {
        throw [InvalidOperationException]::new('Invalid observation budget.')
    }
    $d = [ordered]@{
        strategy='ENUMERATE_THEN_GET_SAME_PROCESS_V1'; process_count=@($Rows).Count;
        initial_missing_command_line_count=0; initial_missing_executable_path_count=0;
        direct_get_attempt_count=0; direct_get_completed_count=0; direct_get_resolved_count=0;
        direct_get_failed_count=0; direct_get_access_denied_count=0;
        identity_unavailable_count=0; identity_mismatch_count=0; inconsistent_snapshot_count=0;
        budget_skipped_count=0; final_missing_command_line_count=0;
        final_missing_executable_path_count=0; unresolved_count=0;
        max_direct_gets=$MaxDirectGets; budget_milliseconds=$BudgetMilliseconds;
        automatic_retries=0
    }
    $effective = [Collections.Generic.List[object]]::new()
    $seen = [Collections.Generic.HashSet[long]]::new()
    $clock = Get-CdpdMetadataClock
    foreach ($row in @($Rows)) {
        $line = Get-CdpdMetaField $row 'CommandLine'
        $image = Get-CdpdMetaField $row 'ExecutablePath'
        $lineMissing = -not (Test-CdpdMetaText $line)
        $imageMissing = -not (Test-CdpdMetaText $image)
        if ($lineMissing) { $d.initial_missing_command_line_count++ }
        if ($imageMissing) { $d.initial_missing_executable_path_count++ }
        $selected = $row
        $processValue = Get-CdpdMetaField $row 'ProcessId'
        $validPid = ($null -ne $processValue -and $processValue -isnot [bool] -and $processValue -isnot [string] -and
            ($processValue -is [int] -or $processValue -is [uint32] -or $processValue -is [long]))
        if ($validPid) { $validPid = ([long]$processValue -gt 0 -and [long]$processValue -le 2147483647) }
        if (-not $validPid -or -not $seen.Add([long]$processValue)) {
            $d.inconsistent_snapshot_count++
        } elseif ($lineMissing -or $imageMissing) {
            $identity = Get-CdpdMetaIdentity $row
            if ($null -eq $identity) {
                $d.identity_unavailable_count++
            } elseif ($d.direct_get_attempt_count -ge $MaxDirectGets -or $clock.ElapsedMilliseconds -ge $BudgetMilliseconds) {
                $d.budget_skipped_count++
            } else {
                $d.direct_get_attempt_count++
                try {
                    $fresh = @(Invoke-CdpdDirectProcessGet $row)
                    $d.direct_get_completed_count++
                    if ($clock.ElapsedMilliseconds -ge $BudgetMilliseconds) {
                        $d.budget_skipped_count++
                    } elseif ($fresh.Count -ne 1) {
                        $d.identity_mismatch_count++
                    } else {
                        $freshIdentity = Get-CdpdMetaIdentity $fresh[0]
                        if (-not (Test-CdpdSameMetaIdentity $identity $freshIdentity)) {
                            $d.identity_mismatch_count++
                        } else {
                            $newLine = Get-CdpdMetaField $fresh[0] 'CommandLine'
                            $newImage = Get-CdpdMetaField $fresh[0] 'ExecutablePath'
                            if ((-not $lineMissing -and $line -cne $newLine) -or
                                (-not $imageMissing -and $image -cne $newImage)) {
                                $d.inconsistent_snapshot_count++
                            } else {
                                # Adopt one entire identity-checked snapshot, never
                                # mix a line from one observation with another image.
                                $selected = $fresh[0]
                                if ((Test-CdpdMetaText $newLine) -and (Test-CdpdMetaText $newImage)) {
                                    $d.direct_get_resolved_count++
                                }
                            }
                        }
                    }
                } catch {
                    $d.direct_get_failed_count++
                    # Only the documented fixed enum is inspected; no exception
                    # text, private error message or stack is exported.
                    $code = Get-CdpdMetaField $_.Exception 'StatusCode'
                    if ($null -ne $code -and ([string]$code) -ceq 'AccessDenied') { $d.direct_get_access_denied_count++ }
                }
            }
        }
        $effective.Add($selected)
        $finalLineMissing = -not (Test-CdpdMetaText (Get-CdpdMetaField $selected 'CommandLine'))
        $finalImageMissing = -not (Test-CdpdMetaText (Get-CdpdMetaField $selected 'ExecutablePath'))
        if ($finalLineMissing) { $d.final_missing_command_line_count++ }
        if ($finalImageMissing) { $d.final_missing_executable_path_count++ }
        if ($finalLineMissing -or $finalImageMissing) { $d.unresolved_count++ }
    }
    return [pscustomobject]@{Rows=$effective.ToArray(); Diagnostics=$d}
}
function Invoke-CdpdLimitedProcessAccess([int] $ProcessId, [long] $CreatedUtcTicks) {
    # Supplemental read-only diagnostic, never used as an argv or ownership substitute.
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw [PlatformNotSupportedException]::new('Native observation requires Windows.')
    }
    if ($null -eq ('CdpdL03.Fix003.ReadAccess' -as [type])) {
        $source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'process_read_access.cs'))
        Add-Type -TypeDefinition $source -ErrorAction Stop | Out-Null
    }
    return [CdpdL03.Fix003.ReadAccess]::Probe($ProcessId, $CreatedUtcTicks)
}
function Get-CdpdSupplementaryAccess([object[]] $Rows) {
    $d = [ordered]@{
        strategy='WIN32_LIMITED_QUERY_DIAGNOSTIC_ONLY_V1'; requested_count=0; attempted_count=0;
        identity_verified_count=0; process_changed_count=0; limited_query_denied_count=0;
        image_path_readable_count=0; token_elevated_count=0; token_standard_count=0;
        token_unavailable_count=0; unavailable_count=0; skipped_count=0;
        classification='NOT_NEEDED'; automatic_retries=0; launch_authority=$false
    }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $nativeUnavailable = $false
    foreach ($row in @($Rows)) {
        if ((Test-CdpdMetaText (Get-CdpdMetaField $row 'CommandLine')) -and
            (Test-CdpdMetaText (Get-CdpdMetaField $row 'ExecutablePath'))) { continue }
        $d.requested_count++
        $identity = Get-CdpdMetaIdentity $row
        if ($nativeUnavailable -or $null -eq $identity -or $d.attempted_count -ge 64 -or $clock.ElapsedMilliseconds -ge 4000) {
            $d.skipped_count++; continue
        }
        $d.attempted_count++
        try {
            $o = Invoke-CdpdLimitedProcessAccess $identity.ProcessId $identity.CreatedUtcTicks
            if ($o.State -ceq 'ACCESS_DENIED') { $d.limited_query_denied_count++; continue }
            if ($o.State -ceq 'IDENTITY_CHANGED') { $d.process_changed_count++; continue }
            if ($o.State -cne 'VERIFIED') { $d.unavailable_count++; continue }
            $d.identity_verified_count++
            if ($o.ImageReadable) { $d.image_path_readable_count++ }
            if ($o.Elevation -ceq 'ELEVATED') { $d.token_elevated_count++ }
            elseif ($o.Elevation -ceq 'STANDARD') { $d.token_standard_count++ }
            else { $d.token_unavailable_count++ }
        } catch { $d.unavailable_count++; $nativeUnavailable = $true }
    }
    if ($d.requested_count -gt 0) {
        $d.classification='UNRESOLVED'
        if ($d.process_changed_count -gt 0) { $d.classification='PROCESS_CHANGED' }
        elseif ($d.limited_query_denied_count -gt 0) { $d.classification='LIMITED_QUERY_ACCESS_DENIED' }
        elseif ($d.token_elevated_count -gt 0 -and $d.token_standard_count -gt 0) { $d.classification='MIXED_ELEVATION_OBSERVED' }
        elseif ($d.token_elevated_count -gt 0) { $d.classification='ELEVATED_TARGETS_OBSERVED' }
        elseif ($d.image_path_readable_count -gt 0) { $d.classification='CIM_NATIVE_AVAILABILITY_DIFFERENCE' }
        elseif ($d.unavailable_count -gt 0) { $d.classification='NATIVE_PROBE_UNAVAILABLE' }
    }
    return $d
}

# FIX004: distinguish enumerated objects from processes that are still executing.
# Only a positively proven same-instance TERMINATED object can leave the required set.
function Initialize-CdpdLifecycleNative {
    if ($null -eq ('CdpdL03.Fix004.Lifecycle' -as [type])) {
        Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'process_lifecycle.cs'))) -ErrorAction Stop | Out-Null
    }
}
function Invoke-CdpdProcessLifecycle([int] $ProcessId, [long] $CreatedUtcTicks) {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw [PlatformNotSupportedException]::new('Lifecycle observation requires Windows.')
    }
    Initialize-CdpdLifecycleNative
    return [CdpdL03.Fix004.Lifecycle]::Probe($ProcessId, $CreatedUtcTicks)
}
function Get-CdpdLifecycleClock { return [Diagnostics.Stopwatch]::StartNew() }
function Select-CdpdRequiredProcesses([object[]] $Rows, [int] $MaxQueries=256, [int] $BudgetMilliseconds=8000) {
    if ($MaxQueries -lt 0 -or $MaxQueries -gt 256 -or $BudgetMilliseconds -lt 1 -or $BudgetMilliseconds -gt 8000 -or @($Rows).Count -gt 4096) {
        throw [InvalidOperationException]::new('Invalid lifecycle observation budget.')
    }
    $d=[ordered]@{
        strategy='SAME_HANDLE_ZERO_WAIT_V1'; enumerated_count=@($Rows).Count;
        running_count=0; terminated_count=0; unknown_count=0; retained_count=0;
        query_attempt_count=0; identity_unavailable_count=0; identity_changed_count=0;
        access_denied_count=0; query_failed_count=0; inconsistent_count=0;
        budget_skipped_count=0; late_result_count=0; native_unavailable_skipped_count=0;
        max_queries=$MaxQueries; budget_milliseconds=$BudgetMilliseconds; automatic_retries=0
    }
    $retained=[Collections.Generic.List[object]]::new()
    $terminated=[Collections.Generic.List[object]]::new()
    $seen=[Collections.Generic.HashSet[int]]::new()
    $clock=Get-CdpdLifecycleClock
    $nativeUnavailable=$false
    foreach ($row in @($Rows)) {
        $keep=$true; $identity=Get-CdpdMetaIdentity $row
        if ($null -eq $identity) {
            $d.identity_unavailable_count++; $d.unknown_count++
        } elseif (-not $seen.Add($identity.ProcessId)) {
            $d.identity_unavailable_count++; $d.unknown_count++
        } elseif ($nativeUnavailable) {
            $d.native_unavailable_skipped_count++; $d.unknown_count++
        } elseif ($d.query_attempt_count -ge $MaxQueries -or $clock.ElapsedMilliseconds -ge $BudgetMilliseconds) {
            $d.budget_skipped_count++; $d.unknown_count++
        } else {
            $d.query_attempt_count++
            try {
                $answers=@(Invoke-CdpdProcessLifecycle $identity.ProcessId $identity.CreatedUtcTicks)
                if ($clock.ElapsedMilliseconds -ge $BudgetMilliseconds) {
                    $d.late_result_count++; $d.unknown_count++
                } elseif ($answers.Count -ne 1) {
                    $d.query_failed_count++; $d.unknown_count++
                } else {
                    $a=$answers[0]
                    $state=Get-CdpdMetaField $a 'State'
                    $verified=Get-CdpdMetaField $a 'IdentityVerified'
                    $wait=Get-CdpdMetaField $a 'WaitState'
                    $exitTime=Get-CdpdMetaField $a 'ExitTimeObserved'
                    if ($state -ceq 'TERMINATED' -and $verified -is [bool] -and $verified -and
                        $wait -ceq 'SIGNALED' -and $exitTime -is [bool] -and $exitTime) {
                        $keep=$false; $d.terminated_count++; $terminated.Add($row)
                    } elseif ($state -ceq 'RUNNING' -and $verified -is [bool] -and $verified -and
                        $wait -ceq 'NOT_SIGNALED' -and $exitTime -is [bool] -and -not $exitTime) {
                        $d.running_count++
                    } else {
                        $d.unknown_count++
                        if ($state -ceq 'ACCESS_DENIED') { $d.access_denied_count++ }
                        elseif ($state -ceq 'IDENTITY_CHANGED') { $d.identity_changed_count++ }
                        elseif ($state -in @('RUNNING','TERMINATED','INCONSISTENT')) { $d.inconsistent_count++ }
                        else { $d.query_failed_count++ }
                    }
                }
            } catch { $d.query_failed_count++; $d.unknown_count++; $nativeUnavailable=$true }
        }
        if ($keep) { $retained.Add($row) }
    }
    $d.retained_count=$retained.Count
    return [pscustomobject]@{Rows=$retained.ToArray(); TerminatedRows=$terminated.ToArray(); Diagnostics=$d}
}

<# FIX005: bounded secondary LOCAL WMI Get. Definitions only.
No credentials, remote namespace, EnablePrivileges, process memory read, browser IO,
process termination, registry write, or automatic retry. Successful *complete*
WMI snapshots can supply metadata only after identity/nonmissing-field checks.
The original CIM instance is retained privately ONLY for GetOwnerSid invocation.
#>
function Get-CdpdWmiClock { return [Diagnostics.Stopwatch]::StartNew() }
function Invoke-CdpdLocalWmiSnapshot([object] $Row) {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw [PlatformNotSupportedException]::new('LOCAL_WMI_REQUIRES_WINDOWS')
    }
    # Refuse projected/deserialized/foreign objects in the actual OS backend.
    # Component tests inject a synthetic backend rather than relaxing this guard.
    if ($Row -isnot [Microsoft.Management.Infrastructure.CimInstance] -or
        $Row.CimSystemProperties.ClassName -cne 'Win32_Process' -or
        ($Row.CimSystemProperties.Namespace -ine 'root/cimv2' -and
         $Row.CimSystemProperties.Namespace -ine 'root\cimv2')) {
        throw [InvalidOperationException]::new('LOCAL_WMI_INPUT_UNVERIFIED')
    }
    $expected = Get-CdpdMetaIdentity $Row
    if ($null -eq $expected) { throw [InvalidOperationException]::new('LOCAL_WMI_IDENTITY_MISSING') }
    return Read-CdpdLocalWmiProcess -ProcessId $expected.ProcessId
}
function Read-CdpdLocalWmiProcess([int] $ProcessId) {
    if ($ProcessId -lt 1) { throw [InvalidOperationException]::new('LOCAL_WMI_INVALID_PID') }
    Add-Type -AssemblyName System.Management -ErrorAction Stop | Out-Null
    $connection = [System.Management.ConnectionOptions]::new()
    $connection.EnablePrivileges = $false
    $connection.Impersonation = [System.Management.ImpersonationLevel]::Impersonate
    $connection.Authentication = [System.Management.AuthenticationLevel]::PacketPrivacy
    # No ComputerName/Credential inputs. Always the local, original WMI namespace.
    $scope = [System.Management.ManagementScope]::new('\\.\root\cimv2', $connection)
    $path = [System.Management.ManagementPath]::new(('Win32_Process.Handle="' + $ProcessId + '"'))
    $options = [System.Management.ObjectGetOptions]::new()
    # Advisory operation timeout, NOT a guaranteed cancellation bound.
    # Admission/late-result checks and the outer 90s child-process limit also apply.
    $options.Timeout = [TimeSpan]::FromSeconds(1)
    $instance = $null
    try {
        $instance = [System.Management.ManagementObject]::new($scope, $path, $options)
        $instance.Get()
        $values = [ordered]@{}
        foreach ($name in @('ProcessId','Handle','Name','CreationDate','SessionId','ParentProcessId','CommandLine','ExecutablePath')) {
            $property = $instance.Properties[$name]
            $value = $null
            if ($null -ne $property) { $value = $property.Value }
            if ($name -ceq 'CreationDate' -and $value -is [string]) {
                # Only a complete, specified DMTF instant may supply an identity.
                if ($value -notmatch '^\d{14}\.\d{6}[+-]\d{3}$') { $value = $null }
                else { $value = [System.Management.ManagementDateTimeConverter]::ToDateTime($value) }
            }
            $values[$name] = $value
        }
        return [pscustomobject]$values
    } finally { if ($null -ne $instance) { $instance.Dispose() } }
}
function Get-CdpdOwnerQueryInput([object] $Row) {
    $property = $Row.PSObject.Properties['CdpdOriginalCimInstance']
    if ($null -eq $property) { return $Row }
    $original = $property.Value
    if (-not (Test-CdpdSameMetaIdentity (Get-CdpdMetaIdentity $Row) (Get-CdpdMetaIdentity $original))) {
        throw [InvalidOperationException]::new('OWNER_QUERY_IDENTITY_MISMATCH')
    }
    return $original
}
function Resolve-CdpdLocalWmiMetadata([object] $CimResolution, [int] $MaxQueries=64, [int] $BudgetMilliseconds=8000) {
    if ($MaxQueries -lt 0 -or $MaxQueries -gt 64 -or $BudgetMilliseconds -lt 1 -or $BudgetMilliseconds -gt 8000) {
        throw [InvalidOperationException]::new('Invalid local WMI budget.')
    }
    $rows = @($CimResolution.Rows)
    $m = $CimResolution.Diagnostics
    $d = [ordered]@{
        strategy='LOCAL_WMI_GET_SAME_PROCESS_V1'; namespace='LOCAL_ROOT_CIMV2'; privileges_enabled=$false;
        process_count=$rows.Count; requested_count=0; attempted_count=0; completed_count=0;
        resolved_count=0; incomplete_count=0; query_failed_count=0; access_denied_count=0;
        identity_unavailable_count=0; identity_mismatch_count=0; inconsistent_count=0;
        late_result_count=0; budget_skipped_count=0; upstream_blocked_count=0;
        initial_missing_command_line_count=$m.final_missing_command_line_count;
        initial_missing_executable_path_count=$m.final_missing_executable_path_count;
        final_missing_command_line_count=0; final_missing_executable_path_count=0; unresolved_count=0;
        max_queries=$MaxQueries; budget_milliseconds=$BudgetMilliseconds; automatic_retries=0
    }
    $unsafeUpstream = ($m.identity_unavailable_count -gt 0 -or $m.identity_mismatch_count -gt 0 -or
        $m.inconsistent_snapshot_count -gt 0 -or $m.budget_skipped_count -gt 0 -or $m.direct_get_failed_count -gt 0)
    $clock=Get-CdpdWmiClock
    $selectedRows=[Collections.Generic.List[object]]::new()
    foreach ($row in $rows) {
        $selected=$row
        $line=Get-CdpdMetaField $row 'CommandLine'
        $image=Get-CdpdMetaField $row 'ExecutablePath'
        $missingLine=-not (Test-CdpdMetaText $line)
        $missingImage=-not (Test-CdpdMetaText $image)
        if ($missingLine -or $missingImage) {
            $d.requested_count++
            $identity=Get-CdpdMetaIdentity $row
            if ($unsafeUpstream) { $d.upstream_blocked_count++ }
            elseif ($null -eq $identity) { $d.identity_unavailable_count++ }
            elseif ($d.attempted_count -ge $MaxQueries -or $clock.ElapsedMilliseconds -ge $BudgetMilliseconds) { $d.budget_skipped_count++ }
            else {
                $d.attempted_count++
                $completed=$false
                try {
                    $fresh=@(Invoke-CdpdLocalWmiSnapshot $row)
                    $d.completed_count++; $completed=$true
                    if ($clock.ElapsedMilliseconds -ge $BudgetMilliseconds) { $d.late_result_count++ }
                    elseif ($fresh.Count -ne 1 -or -not (Test-CdpdSameMetaIdentity $identity (Get-CdpdMetaIdentity $fresh[0]))) { $d.identity_mismatch_count++ }
                    else {
                        $newLine=Get-CdpdMetaField $fresh[0] 'CommandLine'
                        $newImage=Get-CdpdMetaField $fresh[0] 'ExecutablePath'
                        $oldParent=Get-CdpdMetaField $row 'ParentProcessId'
                        $newParent=Get-CdpdMetaField $fresh[0] 'ParentProcessId'
                        if ((-not $missingLine -and $line -cne $newLine) -or
                            (-not $missingImage -and $image -cne $newImage) -or
                            ($null -ne $oldParent -and $oldParent -ne $newParent)) { $d.inconsistent_count++ }
                        elseif (-not (Test-CdpdMetaText $newLine) -or -not (Test-CdpdMetaText $newImage)) { $d.incomplete_count++ }
                        else {
                            # Copy a WHOLE validated WMI observation, never mix source fields.
                            $values=[ordered]@{}
                            foreach($name in @('ProcessId','Handle','Name','CreationDate','SessionId','ParentProcessId','CommandLine','ExecutablePath')) {
                                $values[$name]=Get-CdpdMetaField $fresh[0] $name
                            }
                            $values['CdpdOriginalCimInstance']=$row
                            $selected=[pscustomobject]$values
                            $d.resolved_count++
                        }
                    }
                } catch {
                    if ($completed) { $d.inconsistent_count++ }
                    else {
                        $d.query_failed_count++
                        # Examine documented, fixed enums only; never error text.
                        $cause=$_.Exception
                        for($depth=0;$depth -lt 4 -and $null -ne $cause;$depth++) {
                            $errorCode=Get-CdpdMetaField $cause 'ErrorCode'
                            if (([string]$errorCode) -ceq 'AccessDenied' -or $cause -is [UnauthorizedAccessException]) {
                                $d.access_denied_count++; break
                            }
                            $cause=Get-CdpdMetaField $cause 'InnerException'
                        }
                    }
                }
            }
        }
        $selectedRows.Add($selected)
        $finalLine=-not (Test-CdpdMetaText (Get-CdpdMetaField $selected 'CommandLine'))
        $finalImage=-not (Test-CdpdMetaText (Get-CdpdMetaField $selected 'ExecutablePath'))
        if($finalLine){$d.final_missing_command_line_count++}
        if($finalImage){$d.final_missing_executable_path_count++}
        if($finalLine -or $finalImage){$d.unresolved_count++}
    }
    # Preserve prior resolver telemetry. Effective guards see both stages' failures.
    $effective=[ordered]@{}
    foreach($key in $m.Keys){$effective[$key]=$m[$key]}
    $effective.final_missing_command_line_count=$d.final_missing_command_line_count
    $effective.final_missing_executable_path_count=$d.final_missing_executable_path_count
    $effective.unresolved_count=$d.unresolved_count
    $effective.identity_mismatch_count += $d.identity_mismatch_count
    $effective.inconsistent_snapshot_count += $d.inconsistent_count
    $effective.budget_skipped_count += $d.budget_skipped_count + $d.late_result_count
    return [pscustomobject]@{Rows=$selectedRows.ToArray(); Diagnostics=$m; WmiDiagnostics=$d; EffectiveDiagnostics=$effective}
}
function Resolve-CdpdProcessMetadataWithWmi([object[]] $Rows,[int] $MaxDirectGets=64,[int] $BudgetMilliseconds=8000) {
    $cim=Resolve-CdpdProcessMetadata -Rows $Rows -MaxDirectGets $MaxDirectGets -BudgetMilliseconds $BudgetMilliseconds
    return Resolve-CdpdLocalWmiMetadata -CimResolution $cim -MaxQueries $MaxDirectGets -BudgetMilliseconds $BudgetMilliseconds
}

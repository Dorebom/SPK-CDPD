<# FIX006 selected-profile guard. Definitions only. No process metadata enumeration.
Chromium singleton resources are an implementation-specific witness; this is not
an authenticated identity service. Exact managed PID/argv/owner/record validation
remains mandatory. No browser window is sent a message.
#>
function Invoke-CdpdProfileScopeNative([string] $Profile, [string] $State, [int] $AllowedProcessId) {
    if ($null -eq ('CdpdL03.Fix006.ProfileScope' -as [type])) {
        Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'profile_scope.cs'))) -ErrorAction Stop | Out-Null
    }
    return [CdpdL03.Fix006.ProfileScope]::Probe($Profile, $State, $AllowedProcessId)
}
function ConvertTo-CdpdScopeEvidence([object] $Snapshot, [int] $AllowedProcessId) {
    # Revalidate the native decision in PowerShell. No opaque native PASS is trusted.
    $d=[ordered]@{strategy='SELECTED_CHROMIUM_PROFILE_RESOURCES_V1';state='UNKNOWN';detail_code='PROFILE_SCOPE_UNAVAILABLE';
        target_window_count=0;related_window_count=0;unexpected_owner_count=0;lockfile_state='UNKNOWN';
        scan_complete=$false;startup_mutex_acquired=$false;managed_pid_required=($AllowedProcessId -gt 0);
        ambient_metadata_required=$false;process_metadata_queries=0}
    if ($null -eq $Snapshot) { return $d }
    if ($Snapshot.Detail -in @('CHROMIUM_STARTUP_BUSY','CHROMIUM_STARTUP_MUTEX_ABANDONED')) {$d.detail_code=$Snapshot.Detail}
    foreach ($key in @('TargetWindows','RelatedWindows','UnexpectedOwners')) {
        if ($Snapshot.$key -isnot [int] -or $Snapshot.$key -lt 0 -or $Snapshot.$key -gt 2048) {return $d}
    }
    $d.target_window_count=$Snapshot.TargetWindows;$d.related_window_count=$Snapshot.RelatedWindows;$d.unexpected_owner_count=$Snapshot.UnexpectedOwners
    if ($Snapshot.ScanComplete -isnot [bool] -or $Snapshot.StartupMutexAcquired -isnot [bool]) {return $d}
    $d.scan_complete=$Snapshot.ScanComplete;$d.startup_mutex_acquired=$Snapshot.StartupMutexAcquired
    if ($Snapshot.LockFile -in @('ABSENT','PRESENT','INVALID','UNKNOWN')) {$d.lockfile_state=$Snapshot.LockFile}
    if (-not $d.scan_complete -or -not $d.startup_mutex_acquired -or $AllowedProcessId -lt 0 -or $d.lockfile_state -notin @('ABSENT','PRESENT')) {return $d}
    if ($d.related_window_count -gt 0 -or $d.unexpected_owner_count -gt 0 -or $d.target_window_count -gt 1) {
        $d.state='COLLISION';$d.detail_code='PROFILE_SCOPE_CONFLICT';return $d
    }
    if ($AllowedProcessId -eq 0) {
        if ($d.target_window_count -ne 0 -or $d.lockfile_state -ne 'ABSENT') {$d.state='COLLISION';$d.detail_code='PROFILE_SCOPE_CONFLICT';return $d}
        $d.state='FREE';$d.detail_code='NONE';return $d
    }
    if ($d.target_window_count -eq 1 -and $d.lockfile_state -eq 'PRESENT') {$d.state='MANAGED';$d.detail_code='NONE'}
    else {$d.detail_code='PROFILE_SCOPE_OWNER_NOT_WITNESSED'}
    return $d
}
function Get-CdpdSelectedProfileScope([string] $Profile,[string] $State,[int] $AllowedProcessId=0) {
    try {$s=Invoke-CdpdProfileScopeNative $Profile $State $AllowedProcessId;return ConvertTo-CdpdScopeEvidence $s $AllowedProcessId}
    catch {return ConvertTo-CdpdScopeEvidence $null $AllowedProcessId}
}

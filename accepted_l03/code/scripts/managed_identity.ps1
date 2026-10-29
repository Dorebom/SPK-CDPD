<# FIX008: selected managed process only. Definitions have no side effects.
The v2 origin record proves what CDPD passed to a successful CreateProcess call.
It is NOT a current CommandLine read-back. Every use separately observes current
native PID/generation, image, token owner/session, liveness, listener and profile.
No WMI metadata fallback, process-memory read, elevation, adoption or auto-retry.
#>
function Initialize-CdpdLaunchNative {
    if ($null -eq ('CdpdL03.Fix007.IsolatedLaunch' -as [type])) {
        Add-Type -TypeDefinition ([IO.File]::ReadAllText($script:CdpdLaunchModulePath)) -ErrorAction Stop | Out-Null
    }
}
function Get-CdpdManagedArguments {
    return ,([string[]]@('--remote-debugging-port=9222','--remote-debugging-address=127.0.0.1',
        ('--user-data-dir=' + $script:CdpdProfile),'--no-first-run','--no-default-browser-check','about:blank'))
}
function Get-CdpdExpectedArgumentDigest {
    Initialize-CdpdLaunchNative
    return [CdpdL03.Fix007.IsolatedLaunch]::CommandDigest($script:CdpdChrome,(Get-CdpdManagedArguments))
}
function Get-CdpdLaunchModuleHash {
    return Get-CdpdHash ([IO.File]::ReadAllBytes($script:CdpdLaunchModulePath))
}
function Assert-CdpdOriginRecordShape([object] $Record) {
    $expected=@('schema','launch_id','profile_id','profile_marker_sha256','endpoint','process_id','process_started_utc',
        'profile_path_sha256','executable_path_sha256','state_directory_sha256','owner_sid_sha256',
        'provenance_kind','argument_vector_sha256','process_creation_filetime','session_id','creator_launch_module_sha256')
    if ($null -eq $Record) { Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID' }
    $keys=@($Record.PSObject.Properties.Name)
    if ($keys.Count -ne $expected.Count) { Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID' }
    for($i=0;$i -lt $expected.Count;$i++) {
        if($keys[$i] -cne $expected[$i]) { Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID' }
    }
    if ($Record.schema -cne 'cdpd.chrome-launch-record/v2' -or
        $Record.provenance_kind -cne 'CREATE_PROCESS_RETURNED_HANDLE_V1' -or
        $Record.process_creation_filetime -isnot [string] -or
        $Record.process_creation_filetime -cnotmatch '^[1-9][0-9]{0,18}$' -or
        $Record.argument_vector_sha256 -isnot [string] -or $Record.argument_vector_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $Record.creator_launch_module_sha256 -isnot [string] -or $Record.creator_launch_module_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        ($Record.session_id -isnot [int] -and $Record.session_id -isnot [long]) -or
        $Record.session_id -lt 0 -or $Record.session_id -gt 2147483647) { Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID' }
    $ft=[long]0
    if (-not [long]::TryParse($Record.process_creation_filetime,[ref]$ft) -or $ft -lt 1) { Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID' }
    try { $stamp=[DateTime]::FromFileTimeUtc($ft).ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'",[Globalization.CultureInfo]::InvariantCulture) }
    catch { Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID' }
    if($stamp -cne $Record.process_started_utc) { Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID' }
}
function Assert-CdpdCurrentOrigin([object] $Record) {
    Set-CdpdStage 'LAUNCH_PROVENANCE'
    if($null -eq $Record -or $Record.schema -cne 'cdpd.chrome-launch-record/v2') {
        Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_REQUIRED'
    }
    Assert-CdpdOriginRecordShape $Record
    if($Record.argument_vector_sha256 -cne (Get-CdpdExpectedArgumentDigest) -or
       $Record.creator_launch_module_sha256 -cne (Get-CdpdLaunchModuleHash)) {
        Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID'
    }
    $script:CdpdManagedEvidence=[ordered]@{
        strategy='NATIVE_IDENTITY_WITH_CREATION_PROVENANCE_V1';
        argument_evidence='CREATE_PROCESS_ARGUMENTS_NOT_CURRENT_READBACK';
        origin_record_version=2; native_observation='NOT_OBSERVED';
        image_verified=$false;owner_verified=$false;generation_verified=$false;session_verified=$false;running_verified=$false
    }
}
function New-CdpdOriginRecord([hashtable] $Marker, [object] $Launched) {
    Set-CdpdStage 'LAUNCH_PROVENANCE'
    Initialize-CdpdLaunchNative
    # A persisted PID, caller-supplied boolean or existing listener cannot create provenance.
    if($Launched -isnot [CdpdL03.Fix007.LaunchedProcess] -or $Launched.Id -lt 1 -or $Launched.StartedFileTime -lt 1 -or
       $Launched.ArgumentVectorSha256 -cne (Get-CdpdExpectedArgumentDigest)) {
        Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_PROVENANCE_INVALID'
    }
    $record=[pscustomobject][ordered]@{
        schema='cdpd.chrome-launch-record/v2';launch_id=[Guid]::NewGuid().ToString('D');
        profile_id=$Marker.Document.profile_id;profile_marker_sha256=$Marker.Hash;endpoint=$Endpoint;
        process_id=[int]$Launched.Id;
        process_started_utc=[DateTime]::FromFileTimeUtc($Launched.StartedFileTime).ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'",[Globalization.CultureInfo]::InvariantCulture);
        profile_path_sha256=$script:CdpdHashes.profile_path_sha256;executable_path_sha256=$script:CdpdHashes.executable_path_sha256;
        state_directory_sha256=$script:CdpdHashes.state_directory_sha256;owner_sid_sha256=$script:CdpdHashes.owner_sid_sha256;
        provenance_kind='CREATE_PROCESS_RETURNED_HANDLE_V1';argument_vector_sha256=$Launched.ArgumentVectorSha256;
        process_creation_filetime=$Launched.StartedFileTime.ToString([Globalization.CultureInfo]::InvariantCulture);
        session_id=[int]$script:CdpdCallerSessionId;creator_launch_module_sha256=(Get-CdpdLaunchModuleHash)
    }
    Assert-CdpdCurrentOrigin $record
    return $record
}
function Assert-CdpdNativeManagedSnapshot([object] $Snapshot,[object] $Record) {
    Set-CdpdStage 'NATIVE_MANAGED_IDENTITY'
    if($null -eq $Snapshot) {Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'MANAGED_NATIVE_UNAVAILABLE'}
    foreach($name in @('ProcessId','CreatedFileTime','ExecutablePath','OwnerSid','SessionId','Elevated','Running')) {
        if($null -eq $Snapshot.PSObject.Properties[$name]) {Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'MANAGED_NATIVE_UNAVAILABLE'}
    }
    if(($Snapshot.ProcessId -isnot [int] -and $Snapshot.ProcessId -isnot [long]) -or
       ($Snapshot.CreatedFileTime -isnot [long] -and $Snapshot.CreatedFileTime -isnot [int]) -or
       $Snapshot.ProcessId -ne $Record.process_id -or $Snapshot.CreatedFileTime -ne [long]$Record.process_creation_filetime) {
        Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_NATIVE_IDENTITY_CHANGED'
    }
    $script:CdpdManagedEvidence.generation_verified=$true
    if($Snapshot.Running -isnot [bool] -or -not $Snapshot.Running) {Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'MANAGED_PROCESS_TERMINATED'}
    $script:CdpdManagedEvidence.running_verified=$true
    Set-CdpdStage 'MANAGED_PROCESS_EXECUTABLE'
    if($Snapshot.ExecutablePath -isnot [string] -or [string]::IsNullOrWhiteSpace($Snapshot.ExecutablePath)) {
        Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'EXECUTABLE_PATH_UNAVAILABLE'
    }
    $actual=Get-CdpdPath $Snapshot.ExecutablePath
    if(-not $actual.Equals($script:CdpdChrome,[StringComparison]::OrdinalIgnoreCase)) {
        Stop-Cdpd 'PORT_OCCUPIED' 'MANAGED_NATIVE_IMAGE_MISMATCH'
    }
    $script:CdpdManagedEvidence.image_verified=$true
    Set-CdpdStage 'MANAGED_PROCESS_OWNER'
    if($Snapshot.OwnerSid -isnot [string] -or [string]::IsNullOrWhiteSpace($Snapshot.OwnerSid)) {Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_OWNER_UNAVAILABLE'}
    if($Snapshot.OwnerSid -cne $script:CdpdOwnerSid) {Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_NATIVE_OWNER_MISMATCH'}
    $script:CdpdManagedEvidence.owner_verified=$true
    if(($Snapshot.SessionId -isnot [int] -and $Snapshot.SessionId -isnot [long]) -or
       $Snapshot.SessionId -ne $Record.session_id -or $Snapshot.SessionId -ne $script:CdpdCallerSessionId) {
        Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_NATIVE_SESSION_MISMATCH'
    }
    $script:CdpdManagedEvidence.session_verified=$true
    if($Snapshot.Elevated -isnot [bool]) {Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'MANAGED_NATIVE_UNAVAILABLE'}
    if($Snapshot.Elevated) {Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_NATIVE_ELEVATED'}
    $script:CdpdManagedEvidence.native_observation='VERIFIED'
}
function Invoke-CdpdNativeManagedRead([object] $Record,[object] $FreshlyCreated) {
    if($null -ne $FreshlyCreated) {
        if($FreshlyCreated -isnot [CdpdL03.Fix007.LaunchedProcess]) {throw [InvalidOperationException]::new('NATIVE_IDENTITY_INVALID')}
        return $FreshlyCreated.ObserveIdentity()
    }
    return [CdpdL03.Fix007.IsolatedLaunch]::ObserveCurrent([int]$Record.process_id,[long]$Record.process_creation_filetime)
}
function Get-CdpdManagedNativeProcess([object] $Record,[object] $FreshlyCreated = $null) {
    Assert-CdpdCurrentOrigin $Record
    Set-CdpdStage 'NATIVE_MANAGED_IDENTITY'
    Initialize-CdpdLaunchNative
    try {
        $snapshot=Invoke-CdpdNativeManagedRead $Record $FreshlyCreated
    } catch {
        $detail='MANAGED_NATIVE_UNAVAILABLE'
        $exception=$_.Exception
        for($i=0;$null -ne $exception -and $i -lt 8;$i++) {
            if($exception.Message -ceq 'NATIVE_IDENTITY_CHANGED') {$detail='MANAGED_NATIVE_IDENTITY_CHANGED';break}
            if($exception.Message -ceq 'NATIVE_PROCESS_TERMINATED') {$detail='MANAGED_PROCESS_TERMINATED';break}
            if($exception.Message -ceq 'NATIVE_IMAGE_UNAVAILABLE') {$detail='EXECUTABLE_PATH_UNAVAILABLE';break}
            if($exception.Message -ceq 'NATIVE_OWNER_UNAVAILABLE') {$detail='PROCESS_OWNER_UNAVAILABLE';break}
            $exception=$exception.InnerException
        }
        Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' $detail
    }
    Assert-CdpdNativeManagedSnapshot $snapshot $Record
    # Returned only to the launcher; never serialize the raw native snapshot.
    return $snapshot
}

function Assert-CdpdOriginStorage([string] $Path) {
    [void] (Assert-CdpdNoReparse $Path)
    $acl=Get-Acl -LiteralPath $Path -ErrorAction Stop
    if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $script:CdpdOwnerSid) {
        Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_STORAGE_UNTRUSTED'
    }
    $trusted=@($script:CdpdOwnerSid,'S-1-5-18','S-1-5-32-544','S-1-3-4')
    $writeMask=[long]([Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership)
    foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if($rule.IdentityReference.Value -ceq 'S-1-3-0' -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        if($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
           (([long]$rule.FileSystemRights -band $writeMask) -ne 0) -and $rule.IdentityReference.Value -cnotin $trusted) {
            Stop-Cdpd 'PROFILE_UNVERIFIED' 'MANAGED_STORAGE_UNTRUSTED'
        }
    }
}

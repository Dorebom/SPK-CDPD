<#
CDPD-L03 FIX008 Candidate. Windows PowerShell 5.1 / PowerShell 7 on Windows 11.
Start explicitly starts one managed browser, or inspects and reuses its record.
Inspect only reads Windows OS state and existing files; it never uses HTTP/CDP.
Probe checks start/reuse readiness without creating files or starting Chrome.
Probe readiness uses a separate schema and never creates a Target Binding.
Run in a child PowerShell process with -NoProfile -NonInteractive -File.
All script-produced stdout is one redacted JSON object; exit 0=PASS, 2=BLOCKED.
This script does not approve a gate, create a binding, log in, or navigate tabs.
#>
[CmdletBinding()]
param(
    [string] $Mode = 'Start',
    [string] $ChromePath,
    [string] $UserDataDir,
    [string] $StateDirectory,
    [string] $Endpoint = 'http://127.0.0.1:9222',
    [object] $StartupTimeoutSeconds = 15,
    [string] $ExpectedPortState = 'Any',
    [string] $ProgressDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$WarningPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$script:CdpdFailure = 'INTERNAL_ERROR'
$script:CdpdMutexOwned = $false
$script:CdpdMutex = $null
$script:CdpdStartedProcess = $null
$script:CdpdProgressDirectory = $null
$script:CdpdProgressSequence = 0
$script:CdpdProgressLastKey = ''
$script:CdpdBrowserSpawnAttempted = $false
$script:CdpdUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
$script:CdpdAllowedReasons = @(
    'INVALID_ARGUMENT', 'ENDPOINT_NOT_ALLOWED', 'UNSUPPORTED_PLATFORM',
    'OS_INSPECTION_UNAVAILABLE', 'PROFILE_UNVERIFIED', 'PROFILE_COLLISION',
    'PORT_OCCUPIED', 'LISTENER_NOT_LOOPBACK', 'CONCURRENT_LAUNCH',
    'STARTUP_FAILED', 'STARTUP_TIMEOUT', 'STORAGE_FAILED', 'INTERNAL_ERROR', 'TEST_PRECONDITION_NOT_MET'
)

function Initialize-CdpdDiagnostics {
    $script:CdpdStage = 'ARGUMENT_VALIDATION'
    $script:CdpdPhase = 'PREPARATION'
    $script:CdpdProcessCount = $null
    $script:CdpdMissingCommandLineCount = $null
    $script:CdpdProfileScope = $null
    $script:CdpdCallerElevated = $null
    $script:CdpdListenerCountBefore = $null
    $script:CdpdManagedEvidence = $null
    $script:CdpdAllowedReasons = @(
        'INVALID_ARGUMENT', 'ENDPOINT_NOT_ALLOWED', 'UNSUPPORTED_PLATFORM',
        'OS_INSPECTION_UNAVAILABLE', 'PROFILE_UNVERIFIED', 'PROFILE_COLLISION',
        'PORT_OCCUPIED', 'LISTENER_NOT_LOOPBACK', 'CONCURRENT_LAUNCH',
        'STARTUP_FAILED', 'STARTUP_TIMEOUT', 'STORAGE_FAILED', 'INTERNAL_ERROR', 'TEST_PRECONDITION_NOT_MET'
    )
    $script:CdpdAllowedStages = @(
        'ARGUMENT_VALIDATION', 'OS_QUERY', 'NATIVE_INITIALIZATION', 'CALLER_IDENTITY',
        'LOCK_ACQUIRE', 'PATH_CHROME', 'PATH_PROFILE', 'PATH_STATE',
        'ORDINARY_PROFILE_CHECK', 'BROWSER_POLICY_CHECK', 'HASH_INITIALIZATION',
        'LISTENER_QUERY', 'PROCESS_QUERY', 'MANAGED_PROCESS_EXECUTABLE',
        'MANAGED_PROCESS_OWNER', 'MANAGED_PROCESS_COMMAND_LINE', 'MANAGED_PROCESS_STARTED',
        'PROFILE_COLLISION_SCAN', 'PROFILE_COLLISION_COMMAND_LINE', 'PROFILE_COLLISION_PATH',
        'PROFILE_DIRECTORY_CHECK', 'STATE_DIRECTORY_CHECK', 'MARKER_CHECK', 'RECORD_CHECK',
        'START_PRECHECK', 'OLD_PROCESS_CHECK', 'STORAGE_CREATE', 'MARKER_WRITE',
        'START_PROCESS', 'STARTUP_WAIT', 'RECORD_WRITE', 'FINAL_OBSERVATION', 'COMPLETE', 'FINALIZE_PROCESS_HANDLE', 'RESULT_EMIT', 'LAUNCH_PROVENANCE', 'NATIVE_MANAGED_IDENTITY'
    )
    $script:CdpdAllowedDetails = @(
        'CHECK_REFUSED', 'UNEXPECTED_EXCEPTION', 'COMMAND_LINE_UNAVAILABLE',
        'EXECUTABLE_PATH_UNAVAILABLE', 'PROCESS_START_TIME_UNAVAILABLE',
        'PROCESS_ID_UNAVAILABLE', 'PROCESS_QUERY_FAILED', 'PROCESS_RECORD_UNAVAILABLE',
        'PROCESS_OWNER_QUERY_FAILED', 'PROCESS_OWNER_UNAVAILABLE', 'PROCESS_OWNER_MISMATCH',
        'OS_QUERY_FAILED', 'LISTENER_QUERY_FAILED', 'PATH_INSPECTION_UNAVAILABLE',
        'INSPECTION_LIMIT_EXCEEDED', 'INVALID_DIAGNOSTIC_STATE',
        'STANDARD_CONTEXT_REQUIRED', 'CALLER_CONTEXT_UNAVAILABLE',
        'PORT_EXPECTED_CLOSED', 'PORT_EXPECTED_OPEN', 'PROCESS_IDENTITY_CHANGED',
        'PROCESS_SNAPSHOT_INCONSISTENT', 'METADATA_RESOLUTION_LIMIT',
        'PROCESS_LIFECYCLE_UNVERIFIED', 'MANAGED_PROCESS_TERMINATED', 'PROFILE_SCOPE_UNAVAILABLE', 'PROFILE_SCOPE_CONFLICT', 'PROFILE_SCOPE_OWNER_NOT_WITNESSED', 'CHROMIUM_STARTUP_BUSY', 'CHROMIUM_STARTUP_MUTEX_ABANDONED',
        'MANAGED_PROVENANCE_REQUIRED', 'MANAGED_PROVENANCE_INVALID', 'MANAGED_NATIVE_UNAVAILABLE',
        'MANAGED_NATIVE_IDENTITY_CHANGED', 'MANAGED_NATIVE_IMAGE_MISMATCH',
        'MANAGED_NATIVE_OWNER_MISMATCH', 'MANAGED_NATIVE_SESSION_MISMATCH', 'MANAGED_NATIVE_ELEVATED', 'MANAGED_STORAGE_UNTRUSTED'
    )
}

function Set-CdpdStage([string] $Stage) {
    if ($Stage -cnotin $script:CdpdAllowedStages) {
        Stop-Cdpd 'INTERNAL_ERROR' 'INVALID_DIAGNOSTIC_STATE'
    }
    $script:CdpdStage = $Stage
    Write-CdpdStageReceipt
}

function Write-CdpdStageReceipt {
    # Optional runner-owned external directory. Fixed vocabulary only, no raw paths/argv/errors.
    $configured = Get-Variable -Name CdpdProgressDirectory -Scope Script -ErrorAction SilentlyContinue
    if ($null -eq $configured -or [string]::IsNullOrEmpty([string]$configured.Value)) { return }
    $key = $script:CdpdPhase + '/' + $script:CdpdStage
    if ($script:CdpdProgressLastKey -ceq $key) { return }
    if ($script:CdpdProgressSequence -ge 512) { Stop-Cdpd 'STORAGE_FAILED' }
    $script:CdpdProgressSequence++
    $doc = [ordered]@{schema='cdpd.launcher-stage/v1';sequence=$script:CdpdProgressSequence;
        phase=$script:CdpdPhase;stage=$script:CdpdStage;browser_spawn_attempted=$script:CdpdBrowserSpawnAttempted;
        observation_only=$true;launch_authority=$false}
    $name = $script:CdpdProgressSequence.ToString('D4')
    $temporary = [IO.Path]::Combine($script:CdpdProgressDirectory, $name + '.tmp')
    $destination = [IO.Path]::Combine($script:CdpdProgressDirectory, $name + '.json')
    try {
        $bytes = $script:CdpdUtf8.GetBytes((ConvertTo-Json -InputObject $doc -Compress))
        $file = [IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try { $file.Write($bytes,0,$bytes.Length);$file.Flush($true) } finally { $file.Dispose() }
        [IO.File]::Move($temporary,$destination)
        $script:CdpdProgressLastKey = $key
    } catch { Stop-Cdpd 'STORAGE_FAILED' }
}

function Stop-Cdpd([string] $Reason, [string] $Detail = 'CHECK_REFUSED') {
    # The exception, not a stale mutable reason variable, carries the refusal.
    # CdpdFailure remains internal for the existing component harness only.
    if ($Reason -cnotin $script:CdpdAllowedReasons -or $Detail -cnotin $script:CdpdAllowedDetails) {
        $Reason = 'INTERNAL_ERROR'; $Detail = 'INVALID_DIAGNOSTIC_STATE'
    }
    $script:CdpdFailure = $Reason
    $failure = [System.InvalidOperationException]::new('CDPD refused the operation.')
    $failure.Data['cdpd_reason'] = $Reason
    $failure.Data['cdpd_detail'] = $Detail
    $failure.Data['cdpd_stage'] = $script:CdpdStage
    throw $failure
}

function Get-CdpdFailureObservation([System.Management.Automation.ErrorRecord] $ErrorRecord) {
    $reason = 'INTERNAL_ERROR'
    $detail = 'UNEXPECTED_EXCEPTION'
    $stage = $script:CdpdStage
    $exception = $ErrorRecord.Exception
    # PowerShell can wrap a thrown .NET exception. Walk only a bounded chain.
    for ($depth = 0; $null -ne $exception -and $depth -lt 8; $depth++) {
        if ($exception -is [InvalidOperationException] -and
            $exception.Message -ceq 'CDPD refused the operation.' -and
            $exception.Data.Contains('cdpd_reason') -and
            $exception.Data.Contains('cdpd_detail') -and
            $exception.Data.Contains('cdpd_stage')) {
            $candidateReason = $exception.Data['cdpd_reason']
            $candidateDetail = $exception.Data['cdpd_detail']
            $candidateStage = $exception.Data['cdpd_stage']
            if ($candidateReason -is [string] -and $candidateReason -cin $script:CdpdAllowedReasons -and
                $candidateDetail -is [string] -and $candidateDetail -cin $script:CdpdAllowedDetails -and
                $candidateStage -is [string] -and $candidateStage -cin $script:CdpdAllowedStages) {
                $reason = $candidateReason; $detail = $candidateDetail; $stage = $candidateStage
            }
            break
        }
        $exception = $exception.InnerException
    }
    if ($stage -cnotin $script:CdpdAllowedStages) { $stage = 'ARGUMENT_VALIDATION' }
    $phase = $script:CdpdPhase
    if ($phase -cnotin @('PREPARATION','ENVIRONMENT','PROBE','PRE_START','PRE_LAUNCH_RECHECK','POST_START','INSPECT','REUSE')) {
        $phase = 'PREPARATION'
    }
    # Closed vocabulary and counts only. Never serialize exception text/stack,
    # raw CommandLine, ExecutablePath, owner SID, profile paths or WebSocket URLs.
    return [ordered]@{
        schema='cdpd.chrome-launch-observation/v1'; status='BLOCKED'; reason_code=$reason;
        diagnostics=[ordered]@{
            phase=$phase; stage=$stage; detail_code=$detail;
            chrome_process_count=$script:CdpdProcessCount;
            missing_command_line_count=$script:CdpdMissingCommandLineCount;
            profile_scope=$script:CdpdProfileScope; elevated=$script:CdpdCallerElevated;listener_count=$script:CdpdListenerCountBefore;
            managed_process_evidence=$script:CdpdManagedEvidence
        }
    }
}

function Get-CdpdProcessField([object] $Process, [string] $Name) {
    return Get-CdpdMetaField $Process $Name
}

function Get-CdpdRequiredProcessText([object] $Process, [string] $Name) {
    $value = Get-CdpdProcessField $Process $Name
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value)) {
        if ($Name -ceq 'CommandLine') { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'COMMAND_LINE_UNAVAILABLE' }
        if ($Name -ceq 'ExecutablePath') { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'EXECUTABLE_PATH_UNAVAILABLE' }
        Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_RECORD_UNAVAILABLE'
    }
    return $value
}

function Get-CdpdHash([byte[]] $Bytes) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Get-CdpdTextHash([string] $Value) {
    return Get-CdpdHash ($script:CdpdUtf8.GetBytes($Value))
}

function Initialize-CdpdNative {
    # Run-only Reflection.Emit avoids Add-Type compiler/temporary file writes in Inspect.
    $assemblyName = [System.Reflection.AssemblyName]::new('CdpdReadOnlyNative')
    if ($PSVersionTable.PSEdition -eq 'Core') {
        $assembly = [System.Reflection.Emit.AssemblyBuilder]::DefineDynamicAssembly($assemblyName, [System.Reflection.Emit.AssemblyBuilderAccess]::Run)
    } else {
        $assembly = [AppDomain]::CurrentDomain.DefineDynamicAssembly($assemblyName, [System.Reflection.Emit.AssemblyBuilderAccess]::Run)
    }
    $module = $assembly.DefineDynamicModule('CdpdReadOnlyNative')
    $type = $module.DefineType(('Cdpd.Native' + [Guid]::NewGuid().ToString('N')), [System.Reflection.TypeAttributes]'Public, Abstract, Sealed')
    $signatures = @(
        @{ Name='CommandLineToArgvW'; Dll='shell32.dll'; Return=[IntPtr]; Args=[type[]]@([string], [int].MakeByRefType()) },
        @{ Name='LocalFree'; Dll='kernel32.dll'; Return=[IntPtr]; Args=[type[]]@([IntPtr]) },
        @{ Name='CreateFileW'; Dll='kernel32.dll'; Return=[IntPtr]; Args=[type[]]@([string], [uint32], [uint32], [IntPtr], [uint32], [uint32], [IntPtr]) },
        @{ Name='GetFinalPathNameByHandleW'; Dll='kernel32.dll'; Return=[uint32]; Args=[type[]]@([IntPtr], [System.Text.StringBuilder], [uint32], [uint32]) },
        @{ Name='CloseHandle'; Dll='kernel32.dll'; Return=[bool]; Args=[type[]]@([IntPtr]) }
    )
    foreach ($signature in $signatures) {
        $method = $type.DefinePInvokeMethod($signature.Name, $signature.Dll,
            [System.Reflection.MethodAttributes]'Public, Static, PinvokeImpl',
            [System.Reflection.CallingConventions]::Standard, $signature.Return, $signature.Args,
            [System.Runtime.InteropServices.CallingConvention]::Winapi, [System.Runtime.InteropServices.CharSet]::Unicode)
        $method.SetImplementationFlags($method.GetMethodImplementationFlags() -bor [System.Reflection.MethodImplAttributes]::PreserveSig)
    }
    $script:CdpdNative = $type.CreateType()
}

function Get-CdpdArgv([string] $CommandLine) {
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'COMMAND_LINE_UNAVAILABLE' }
    if ($CommandLine.Length -gt 32767) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    [object[]] $nativeArgs = @($CommandLine, [int] 0)
    $memory = $script:CdpdNative.GetMethod('CommandLineToArgvW').Invoke($null, $nativeArgs)
    if ($memory -eq [IntPtr]::Zero) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    try {
        $count = [int] $nativeArgs[1]
        if ($count -lt 1 -or $count -gt 256) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        $result = New-Object 'System.Collections.Generic.List[string]'
        for ($index = 0; $index -lt $count; $index++) {
            $pointer = [System.Runtime.InteropServices.Marshal]::ReadIntPtr($memory, $index * [IntPtr]::Size)
            $result.Add([System.Runtime.InteropServices.Marshal]::PtrToStringUni($pointer))
        }
        return ,($result.ToArray())
    } finally {
        [void] $script:CdpdNative.GetMethod('LocalFree').Invoke($null, @($memory))
    }
}

function Assert-CdpdNoReparse([string] $Path) {
    $root = [IO.Path]::GetPathRoot($Path)
    $cursor = $root
    $parts = @($Path.Substring($root.Length).Split([char] '\', [StringSplitOptions]::RemoveEmptyEntries))
    $all = New-Object 'System.Collections.Generic.List[string]'
    $all.Add($root)
    foreach ($part in $parts) { $cursor = [IO.Path]::Combine($cursor, $part); $all.Add($cursor) }
    $lastExisting = $null
    $missing = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $all) {
        if ($missing.Count -gt 0) { $missing.Add([IO.Path]::GetFileName($item)); continue }
        try { $attributes = [IO.File]::GetAttributes($item) }
        catch [IO.FileNotFoundException] { $missing.Add([IO.Path]::GetFileName($item)); continue }
        catch [IO.DirectoryNotFoundException] { $missing.Add([IO.Path]::GetFileName($item)); continue }
        catch { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PATH_INSPECTION_UNAVAILABLE' }
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        if ($item -cne $Path -and ($attributes -band [IO.FileAttributes]::Directory) -eq 0) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        $lastExisting = $item
    }
    if ($null -eq $lastExisting) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    return @{ Existing=$lastExisting; Missing=$missing.ToArray() }
}

function Get-CdpdPath([string] $Path) {
    # Reject UNC/device/drive-relative paths, ADS, ambiguous suffixes and dot segments.
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.Length -gt 240 -or $Path -notmatch '^[A-Za-z]:[\\/]') { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    $normalized = $Path.Replace('/', '\')
    if ($normalized.Substring(2) -match '[\x00-\x1f<>:"|?*]') { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    $segments = @($normalized.Substring(3).Split([char] '\', [StringSplitOptions]::RemoveEmptyEntries))
    foreach ($segment in $segments) {
        if ($segment -in @('.', '..') -or $segment -match '[ .]$') { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    }
    $normalized = [IO.Path]::GetFullPath($normalized)
    if ($normalized.Length -gt 3) { $normalized = $normalized.TrimEnd([char] '\') }
    $walk = Assert-CdpdNoReparse $normalized
    [object[]] $openArgs = @($walk.Existing, [uint32] 0, [uint32] 7, [IntPtr]::Zero, [uint32] 3, [uint32] 0x02200000, [IntPtr]::Zero)
    $handle = $script:CdpdNative.GetMethod('CreateFileW').Invoke($null, $openArgs)
    if ($handle -eq [IntPtr]::Zero -or $handle -eq [IntPtr](-1)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    try {
        $buffer = [System.Text.StringBuilder]::new(32768)
        $length = $script:CdpdNative.GetMethod('GetFinalPathNameByHandleW').Invoke($null, @($handle, $buffer, [uint32] 32768, [uint32] 0))
        if ($length -eq 0 -or $length -ge 32768) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        $resolved = $buffer.ToString()
        if (-not $resolved.StartsWith('\\?\', [StringComparison]::Ordinal)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        $resolved = $resolved.Substring(4)
        if ($resolved -notmatch '^[A-Za-z]:\\') { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        foreach ($part in $walk.Missing) { $resolved = [IO.Path]::Combine($resolved, $part) }
        if ($resolved.Length -gt 3) { $resolved = $resolved.TrimEnd([char] '\') }
        $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($resolved))
        if ($drive.DriveType -ne [IO.DriveType]::Fixed) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        [void] (Assert-CdpdNoReparse $resolved)
        return $resolved
    } finally { [void] $script:CdpdNative.GetMethod('CloseHandle').Invoke($null, @($handle)) }
}

function Test-CdpdRelatedPath([string] $Left, [string] $Right) {
    return $Left.Equals($Right, [StringComparison]::OrdinalIgnoreCase) -or
        $Left.StartsWith(($Right.TrimEnd([char] '\') + '\'), [StringComparison]::OrdinalIgnoreCase) -or
        $Right.StartsWith(($Left.TrimEnd([char] '\') + '\'), [StringComparison]::OrdinalIgnoreCase)
}

function Assert-CdpdOwnedDirectory([string] $Path) {
    Set-CdpdStage 'PROFILE_DIRECTORY_CHECK'
    if (-not [IO.Directory]::Exists($Path)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    [void] (Assert-CdpdNoReparse $Path)
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $script:CdpdOwnerSid) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
}

function Get-CdpdCanonicalDocument([string] $Path, [string[]] $ExpectedKeys, [string[]] $AlternateKeys = @()) {
    [void] (Assert-CdpdNoReparse $Path)
    if (-not [IO.File]::Exists($Path)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        if ($stream.Length -lt 2 -or $stream.Length -gt 8192) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        $bytes = New-Object byte[] ([int] $stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
            $offset += $read
        }
    } finally { $stream.Dispose() }
    $json = $script:CdpdUtf8.GetString($bytes)
    $fromJsonOptions = @{ InputObject=$json; ErrorAction='Stop' }
    if ((Get-Command ConvertFrom-Json -ErrorAction Stop).Parameters.ContainsKey('DateKind')) {
        $fromJsonOptions['DateKind'] = 'String' # PowerShell 7.5+ must not infer DateTime fields.
    }
    $document = ConvertFrom-Json @fromJsonOptions
    if ($null -eq $document -or $document -isnot [PSCustomObject]) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    $keys = @($document.PSObject.Properties.Name)
    # One file read, two explicit record generations only. Never re-read an
    # invalid document using a weaker schema or upgrade a live legacy record.
    $matchesPrimary = ($keys.Count -eq $ExpectedKeys.Count)
    if ($matchesPrimary) { for ($index=0; $index -lt $keys.Count; $index++) { if ($keys[$index] -cne $ExpectedKeys[$index]) { $matchesPrimary=$false; break } } }
    $matchesAlternate = ($AlternateKeys.Count -gt 0 -and $keys.Count -eq $AlternateKeys.Count)
    if ($matchesAlternate) { for ($index=0; $index -lt $keys.Count; $index++) { if ($keys[$index] -cne $AlternateKeys[$index]) { $matchesAlternate=$false; break } } }
    if (-not $matchesPrimary -and -not $matchesAlternate) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    # PowerShell 6 through 7.4 lack DateKind and may infer these two timestamps.
    # Restore only the defined UTC spelling before the exact-byte round-trip;
    # offset forms, omitted fractional digits and alternate encodings still fail.
    foreach ($timeKey in @('created_at_utc', 'process_started_utc')) {
        $timeProperty = $document.PSObject.Properties[$timeKey]
        if ($null -ne $timeProperty -and $timeProperty.Value -is [DateTime]) {
            $timeProperty.Value = $timeProperty.Value.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture)
        }
    }
    # Machine-owned files use exact canonical bytes: duplicates, aliases, whitespace,
    # comments, reordered/unknown keys and alternative encodings cannot round-trip.
    if ((ConvertTo-Json -InputObject $document -Compress -Depth 4) -cne $json) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    return @{ Document=$document; Hash=(Get-CdpdHash $bytes) }
}

function Assert-CdpdUuid([object] $Value) {
    if ($Value -isnot [string] -or $Value -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') { Stop-Cdpd 'PROFILE_UNVERIFIED' }
}

function Assert-CdpdUtc([object] $Value) {
    if ($Value -isnot [string]) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    $parsed = [DateTime]::MinValue
    if (-not [DateTime]::TryParseExact($Value, "yyyy-MM-ddTHH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal, [ref] $parsed)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
}

function Get-CdpdMarker {
    Set-CdpdStage 'MARKER_CHECK'
    $result = Get-CdpdCanonicalDocument $script:CdpdMarkerPath @('schema','profile_id','created_at_utc','profile_path_sha256','executable_path_sha256','state_directory_sha256','owner_sid_sha256')
    $document = $result.Document
    if ($document.schema -cne 'cdpd.chrome-profile-marker/v1') { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    Assert-CdpdUuid $document.profile_id
    Assert-CdpdUtc $document.created_at_utc
    foreach ($entry in $script:CdpdHashes.GetEnumerator()) {
        if ($document.($entry.Key) -isnot [string] -or $document.($entry.Key) -cne $entry.Value) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    }
    return $result
}

function Get-CdpdRecord([hashtable] $Marker) {
    Set-CdpdStage 'RECORD_CHECK'
    $baseKeys = @('schema','launch_id','profile_id','profile_marker_sha256','endpoint','process_id','process_started_utc','profile_path_sha256','executable_path_sha256','state_directory_sha256','owner_sid_sha256')
    $originKeys = $baseKeys + @('provenance_kind','argument_vector_sha256','process_creation_filetime','session_id','creator_launch_module_sha256')
    $result = Get-CdpdCanonicalDocument $script:CdpdRecordPath $baseKeys $originKeys
    $document = $result.Document
    if ($document.schema -ceq 'cdpd.chrome-launch-record/v1' -and @($document.PSObject.Properties).Count -ne $baseKeys.Count) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    if ($document.schema -ceq 'cdpd.chrome-launch-record/v2') { Assert-CdpdOriginRecordShape $document }
    if ($document.schema -cnotin @('cdpd.chrome-launch-record/v1','cdpd.chrome-launch-record/v2') -or $document.endpoint -cne $Endpoint) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    Assert-CdpdUuid $document.launch_id
    if ($document.profile_id -cne $Marker.Document.profile_id -or $document.profile_marker_sha256 -cne $Marker.Hash) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    if (($document.process_id -isnot [int] -and $document.process_id -isnot [long]) -or $document.process_id -lt 1 -or $document.process_id -gt 2147483647) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    Assert-CdpdUtc $document.process_started_utc
    foreach ($entry in $script:CdpdHashes.GetEnumerator()) {
        if ($document.($entry.Key) -isnot [string] -or $document.($entry.Key) -cne $entry.Value) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    }
    return $result
}

function Write-CdpdAtomicDocument([string] $Path, [object] $Document) {
    # Only Start calls this function. Same-directory rename/replace is the commit.
    [void] (Assert-CdpdNoReparse ([IO.Path]::GetDirectoryName($Path)))
    $bytes = $script:CdpdUtf8.GetBytes((ConvertTo-Json -InputObject $Document -Compress -Depth 4))
    $temporary = [IO.Path]::Combine([IO.Path]::GetDirectoryName($Path), ('.cdpd-' + [Guid]::NewGuid().ToString('N') + '.tmp'))
    $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
    [void] (Assert-CdpdNoReparse $Path)
    # NullString supplies an actual null backup path across PowerShell editions.
    if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $Path) }
    # Failed commits leave their own temporary file for human inspection; no cleanup
    # ever deletes profile data or resets a browser instance.
}

function Get-CdpdListeners {
    Set-CdpdStage 'LISTENER_QUERY'
    try {
        # Enumerate all listeners first. Filtering via the cmdlet's port selector can
        # report 'no matching objects' as an error, obscuring an actually free port.
        return @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 9222 })
    } catch { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'LISTENER_QUERY_FAILED' }
}

function Get-CdpdSingleListener {
    $listeners = @(Get-CdpdListeners)
    if ($listeners.Count -eq 0) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    foreach ($listener in $listeners) {
        if ([string] $listener.LocalAddress -cne '127.0.0.1') { Stop-Cdpd 'LISTENER_NOT_LOOPBACK' }
    }
    if ($listeners.Count -ne 1 -or $listeners[0].OwningProcess -lt 1) { Stop-Cdpd 'PORT_OCCUPIED' }
    return $listeners[0]
}

function Get-CdpdProcess([int] $ProcessId) {
    Set-CdpdStage 'PROCESS_QUERY'
    try {
        $rows = @(Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = " + $ProcessId) -Property ProcessId,Name,CommandLine,ExecutablePath,CreationDate,SessionId,ParentProcessId -OperationTimeoutSec 2 -ErrorAction Stop)
    } catch { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_QUERY_FAILED' }
    if ($rows.Count -ne 1) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_RECORD_UNAVAILABLE' }
    if ((Get-CdpdProcessField $rows[0] 'ProcessId') -ne $ProcessId) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    $selection = Select-CdpdRequiredProcesses -Rows $rows -MaxQueries 1 -BudgetMilliseconds 2000
    if ($selection.Diagnostics.terminated_count -ne 0) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'MANAGED_PROCESS_TERMINATED' }
    if ($selection.Diagnostics.running_count -ne 1 -or $selection.Diagnostics.unknown_count -ne 0) {
        Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_LIFECYCLE_UNVERIFIED'
    }
    $resolved = Resolve-CdpdProcessMetadataWithWmi -Rows $selection.Rows -MaxDirectGets 1 -BudgetMilliseconds 2000
    Assert-CdpdResolutionConsistency $resolved.EffectiveDiagnostics
    return $resolved.Rows[0]
}

function Get-CdpdProcessStarted([object] $Process) {
    Set-CdpdStage 'MANAGED_PROCESS_STARTED'
    $created = Get-CdpdProcessField $Process 'CreationDate'
    if ($created -isnot [DateTime]) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_START_TIME_UNAVAILABLE' }
    return $created.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture)
}

function Assert-CdpdManagedProcess([object] $Process) {
    Set-CdpdStage 'MANAGED_PROCESS_EXECUTABLE'
    $executable = Get-CdpdPath (Get-CdpdRequiredProcessText $Process 'ExecutablePath')
    if (-not $executable.Equals($script:CdpdChrome, [StringComparison]::OrdinalIgnoreCase)) { Stop-Cdpd 'PORT_OCCUPIED' }
    Set-CdpdStage 'MANAGED_PROCESS_OWNER'
    try { $owner = Invoke-CimMethod -InputObject (Get-CdpdOwnerQueryInput $Process) -MethodName GetOwnerSid -OperationTimeoutSec 2 -ErrorAction Stop }
    catch { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_OWNER_QUERY_FAILED' }
    $ownerReturn = Get-CdpdProcessField $owner 'ReturnValue'
    $ownerSid = Get-CdpdProcessField $owner 'Sid'
    if ($null -eq $ownerReturn -or $ownerReturn -ne 0 -or $ownerSid -isnot [string] -or [string]::IsNullOrWhiteSpace($ownerSid)) {
        Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_OWNER_UNAVAILABLE'
    }
    if ($ownerSid -cne $script:CdpdOwnerSid) { Stop-Cdpd 'PROFILE_UNVERIFIED' 'PROCESS_OWNER_MISMATCH' }
    Set-CdpdStage 'MANAGED_PROCESS_COMMAND_LINE'
    $arguments = Get-CdpdArgv (Get-CdpdRequiredProcessText $Process 'CommandLine')
    if ($arguments.Count -ne 7) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    if (-not (Get-CdpdPath $arguments[0]).Equals($script:CdpdChrome, [StringComparison]::OrdinalIgnoreCase)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    # Preserve the exact original managed-process allowlist and its ordering.
    $expected = @('--remote-debugging-port=9222', '--remote-debugging-address=127.0.0.1',
        ('--user-data-dir=' + $script:CdpdProfile), '--no-first-run', '--no-default-browser-check', 'about:blank')
    for ($index=0; $index -lt $expected.Count; $index++) {
        if ($arguments[$index + 1] -cne $expected[$index]) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    }
    [void] (Get-CdpdProcessStarted $Process)
}

function Get-CdpdProfileSwitches([string[]] $Arguments) {
    $found = @{}
    for ($index=1; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        if ($argument -ceq '--') { break }
        if ($argument -match '^(?:--|-|/)(user-data-dir|type)(?:=(.*))?$') {
            $key = $Matches[1].ToLowerInvariant()
            if ($found.ContainsKey($key)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
            $value = ''
            if ($Matches.ContainsKey(2)) { $value = $Matches[2] }
            elseif ($index + 1 -lt $Arguments.Count -and $Arguments[$index + 1] -notmatch '^(?:--|-|/)') { $index++; $value = $Arguments[$index] }
            if ([string]::IsNullOrWhiteSpace($value)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
            $found[$key] = $value
        }
    }
    return $found
}

function Assert-CdpdResolutionConsistency([object] $Diagnostics) {
    if ($Diagnostics.identity_mismatch_count -gt 0) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_IDENTITY_CHANGED' }
    if ($Diagnostics.inconsistent_snapshot_count -gt 0) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_SNAPSHOT_INCONSISTENT' }
    if ($Diagnostics.budget_skipped_count -gt 0) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'METADATA_RESOLUTION_LIMIT' }
}

function Assert-CdpdNoProfileCollision([int] $AllowedProcessId = 0) {
    Set-CdpdStage 'PROFILE_COLLISION_SCAN'
    # FIX006: inspect only the selected profile's singleton resources. No global
    # Chrome enumeration, child argv, parent-process heuristic or metadata fallback.
    $script:CdpdProfileScope = Get-CdpdSelectedProfileScope $script:CdpdProfile $script:CdpdState $AllowedProcessId
    if ($script:CdpdProfileScope.state -ceq 'COLLISION') {
        Stop-Cdpd 'PROFILE_COLLISION' 'PROFILE_SCOPE_CONFLICT'
    }
    $expected = 'FREE'; if ($AllowedProcessId -gt 0) {$expected='MANAGED'}
    if ($script:CdpdProfileScope.state -cne $expected) {
        Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' $script:CdpdProfileScope.detail_code
    }
}

function Assert-CdpdStateContents {
    Set-CdpdStage 'STATE_DIRECTORY_CHECK'
    Assert-CdpdOwnedDirectory $script:CdpdState
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($script:CdpdState)) {
        if (-not $entry.Equals($script:CdpdRecordPath, [StringComparison]::OrdinalIgnoreCase) -or -not [IO.File]::Exists($entry)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    }
}

function Get-CdpdObservation([string] $Action) {
    $listener = Get-CdpdSingleListener
    Assert-CdpdOwnedDirectory $script:CdpdProfile
    Assert-CdpdStateContents
    $marker = Get-CdpdMarker
    $record = Get-CdpdRecord $marker
    Assert-CdpdOriginStorage $script:CdpdState
    Assert-CdpdOriginStorage $script:CdpdRecordPath
    Assert-CdpdOriginStorage $script:CdpdMarkerPath
    Assert-CdpdCurrentOrigin $record.Document
    if ([long] $listener.OwningProcess -ne [long] $record.Document.process_id) { Stop-Cdpd 'PORT_OCCUPIED' }
    [void] (Get-CdpdManagedNativeProcess $record.Document)
    Assert-CdpdNoProfileCollision ([int] $record.Document.process_id)
    # A second OS/file observation catches changes during the first enumeration.
    $lastListener = Get-CdpdSingleListener
    if ([long] $lastListener.OwningProcess -ne [long] $record.Document.process_id) { Stop-Cdpd 'PORT_OCCUPIED' }
    [void] (Get-CdpdManagedNativeProcess $record.Document)
    $lastMarker = Get-CdpdMarker
    $lastRecord = Get-CdpdRecord $lastMarker
    if ($lastMarker.Hash -cne $marker.Hash -or $lastRecord.Hash -cne $record.Hash) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    Assert-CdpdNoProfileCollision ([int] $record.Document.process_id)
    return [ordered]@{
        schema='cdpd.chrome-launch-observation/v1'; status='PASS'; action=$Action;
        endpoint=$Endpoint; launch_id=$record.Document.launch_id; record_sha256=$record.Hash;
        process_id=[int] $record.Document.process_id; process_started_utc=$record.Document.process_started_utc;
        profile_path_sha256=$script:CdpdHashes.profile_path_sha256;
        executable_path_sha256=$script:CdpdHashes.executable_path_sha256;
        owner_sid_sha256=$script:CdpdHashes.owner_sid_sha256;
        listener_address='127.0.0.1'; listener_port=9222; evidence_kind='WINDOWS_OS_INSPECTION'
    }
}

function Assert-CdpdStandardContext([object] $Elevated) {
    Set-CdpdStage 'CALLER_IDENTITY'
    if ($Elevated -isnot [bool]) {
        Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'CALLER_CONTEXT_UNAVAILABLE'
    }
    if ($Elevated) {
        Stop-Cdpd 'TEST_PRECONDITION_NOT_MET' 'STANDARD_CONTEXT_REQUIRED'
    }
}

function Assert-CdpdPortExpectation([int] $ListenerCount, [string] $Expected) {
    Set-CdpdStage 'LISTENER_QUERY'
    if ($ListenerCount -lt 0 -or $Expected -cnotin @('Any','Closed','Open')) {
        Stop-Cdpd 'INVALID_ARGUMENT'
    }
    if ($Expected -ceq 'Closed' -and $ListenerCount -ne 0) {
        Stop-Cdpd 'TEST_PRECONDITION_NOT_MET' 'PORT_EXPECTED_CLOSED'
    }
    if ($Expected -ceq 'Open' -and $ListenerCount -eq 0) {
        Stop-Cdpd 'TEST_PRECONDITION_NOT_MET' 'PORT_EXPECTED_OPEN'
    }
    # Open is not managed. Get-CdpdObservation still verifies every owner/record.
}

function Assert-CdpdPreviousProcessInactive([object] $PreviousRecord) {
    Set-CdpdStage 'OLD_PROCESS_CHECK'
        try { $oldProcesses = @(Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = " + $PreviousRecord.process_id) -Property ProcessId,Name,CreationDate,SessionId -OperationTimeoutSec 2 -ErrorAction Stop) }
        catch { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_QUERY_FAILED' }
        if ($oldProcesses.Count -gt 0) {
            if ($null -ne $PreviousRecord.PSObject.Properties['schema'] -and $PreviousRecord.schema -ceq 'cdpd.chrome-launch-record/v2') {
                if ($oldProcesses.Count -ne 1) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
                Initialize-CdpdLaunchNative
                $oldState=[CdpdL03.Fix007.IsolatedLaunch]::ObserveRecordedState([int]$PreviousRecord.process_id,[long]$PreviousRecord.process_creation_filetime)
                if ($oldState -ceq 'TERMINATED') { return }
                if ($oldState -ceq 'RUNNING' -or $oldState -ceq 'IDENTITY_CHANGED') { Stop-Cdpd 'PROFILE_UNVERIFIED' }
                Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_LIFECYCLE_UNVERIFIED'
            }
            if ($oldProcesses.Count -ne 1 -or (Get-CdpdProcessStarted $oldProcesses[0]) -cne $PreviousRecord.process_started_utc) {
                Stop-Cdpd 'PROFILE_UNVERIFIED'
            }
            $oldLife = Select-CdpdRequiredProcesses -Rows $oldProcesses -MaxQueries 1 -BudgetMilliseconds 2000
            if ($oldLife.Diagnostics.unknown_count -ne 0) { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'PROCESS_LIFECYCLE_UNVERIFIED' }
            if ($oldLife.Diagnostics.terminated_count -ne 1) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        }
}

function Get-CdpdStartContext {
    Set-CdpdStage 'START_PRECHECK'
    Assert-CdpdNoProfileCollision
    if ([IO.Directory]::Exists($script:CdpdProfile)) {
        Assert-CdpdOwnedDirectory $script:CdpdProfile
        if (-not [IO.File]::Exists($script:CdpdMarkerPath) -and @([IO.Directory]::EnumerateFileSystemEntries($script:CdpdProfile)).Count -gt 0) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    }
    if ([IO.Directory]::Exists($script:CdpdState)) { Assert-CdpdStateContents }
    if ([IO.File]::Exists($script:CdpdMarkerPath)) { $marker = Get-CdpdMarker }
    else {
        if ([IO.File]::Exists($script:CdpdRecordPath)) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        $marker = $null
    }
    if ([IO.File]::Exists($script:CdpdRecordPath)) {
        $previous = Get-CdpdRecord $marker
        Assert-CdpdPreviousProcessInactive $previous.Document
    }
    if (@(Get-CdpdListeners).Count -gt 0) { Stop-Cdpd 'PORT_OCCUPIED' }
    return @{ Marker=$marker }
}

Initialize-CdpdDiagnostics

try {
    . (Join-Path $PSScriptRoot 'process_observation.ps1')
    . (Join-Path $PSScriptRoot 'profile_scope.ps1')
    . (Join-Path $PSScriptRoot 'managed_identity.ps1')
    if ($Mode -cnotin @('Start', 'Inspect', 'Probe', 'Environment') -or [string]::IsNullOrWhiteSpace($ChromePath) -or
        [string]::IsNullOrWhiteSpace($UserDataDir) -or [string]::IsNullOrWhiteSpace($StateDirectory)) { Stop-Cdpd 'INVALID_ARGUMENT' }
    if ($ExpectedPortState -cnotin @('Any','Closed','Open')) { Stop-Cdpd 'INVALID_ARGUMENT' }
    if ($Endpoint -cne 'http://127.0.0.1:9222') { Stop-Cdpd 'ENDPOINT_NOT_ALLOWED' }
    $timeoutText = [string] $StartupTimeoutSeconds
    if ($timeoutText -notmatch '^(?:[1-9]|[12][0-9]|30)$') { Stop-Cdpd 'INVALID_ARGUMENT' }
    $startupSeconds = [int] $timeoutText
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or $PSVersionTable.PSVersion -lt [Version]'5.1') { Stop-Cdpd 'UNSUPPORTED_PLATFORM' }
    Set-CdpdStage 'OS_QUERY'
    $script:CdpdFailure = 'OS_INSPECTION_UNAVAILABLE'
    [void] (Get-Command Get-CimInstance, Invoke-CimMethod, Get-NetTCPConnection, Get-Acl -ErrorAction Stop)
    try { $operatingSystems = @(Get-CimInstance -ClassName Win32_OperatingSystem -OperationTimeoutSec 2 -ErrorAction Stop) }
    catch { Stop-Cdpd 'OS_INSPECTION_UNAVAILABLE' 'OS_QUERY_FAILED' }
    if ($operatingSystems.Count -ne 1 -or $operatingSystems[0].ProductType -ne 1 -or [int] $operatingSystems[0].BuildNumber -lt 22000) { Stop-Cdpd 'UNSUPPORTED_PLATFORM' }
    Set-CdpdStage 'NATIVE_INITIALIZATION'
    Initialize-CdpdNative
    Set-CdpdStage 'CALLER_IDENTITY'
    $caller = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $script:CdpdOwnerSid = $caller.User.Value
        $principal = [Security.Principal.WindowsPrincipal]::new($caller)
        $script:CdpdCallerElevated=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        Assert-CdpdStandardContext $script:CdpdCallerElevated
        $currentProcess = [Diagnostics.Process]::GetCurrentProcess()
        try { $script:CdpdCallerSessionId = $currentProcess.SessionId } finally { $currentProcess.Dispose() }
    } finally { $caller.Dispose() }
    Set-CdpdStage 'LOCK_ACQUIRE'
    $script:CdpdFailure = 'CONCURRENT_LAUNCH'
    $script:CdpdMutex = [Threading.Mutex]::new($false, 'Global\CDPD.ChatGPTProfile.Loopback9222.v1')
    try { $script:CdpdMutexOwned = $script:CdpdMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $script:CdpdMutexOwned = $true; Stop-Cdpd 'CONCURRENT_LAUNCH' }
    if (-not $script:CdpdMutexOwned) { Stop-Cdpd 'CONCURRENT_LAUNCH' }
    $script:CdpdFailure = 'PROFILE_UNVERIFIED'
    Set-CdpdStage 'PATH_CHROME'
    $script:CdpdChrome = Get-CdpdPath $ChromePath
    Set-CdpdStage 'PATH_PROFILE'
    $script:CdpdProfile = Get-CdpdPath $UserDataDir
    Set-CdpdStage 'PATH_STATE'
    $script:CdpdState = Get-CdpdPath $StateDirectory
    if (-not [IO.File]::Exists($script:CdpdChrome) -or [IO.Path]::GetFileName($script:CdpdChrome).ToLowerInvariant() -cnotin @('chrome.exe','chromium.exe')) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
    if ($script:CdpdProfile.Length -le 3 -or $script:CdpdState.Length -le 3 -or
        (Test-CdpdRelatedPath $script:CdpdProfile $script:CdpdState) -or
        (Test-CdpdRelatedPath $script:CdpdChrome $script:CdpdProfile) -or
        (Test-CdpdRelatedPath $script:CdpdChrome $script:CdpdState)) { Stop-Cdpd 'PROFILE_COLLISION' }
    if (-not [string]::IsNullOrEmpty($ProgressDirectory)) {
        $candidateDirectory = Get-CdpdPath ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')))
        $progressPath = Get-CdpdPath $ProgressDirectory
        if (-not [IO.Directory]::Exists($progressPath) -or
            @( [IO.Directory]::EnumerateFileSystemEntries($progressPath) ).Count -ne 0 -or
            (Test-CdpdRelatedPath $progressPath $candidateDirectory) -or
            (Test-CdpdRelatedPath $progressPath $script:CdpdProfile) -or
            (Test-CdpdRelatedPath $progressPath $script:CdpdState) -or
            (Test-CdpdRelatedPath $progressPath $script:CdpdChrome)) { Stop-Cdpd 'INVALID_ARGUMENT' }
        Assert-CdpdOwnedDirectory $progressPath
        $script:CdpdProgressDirectory = $progressPath
    }
    Set-CdpdStage 'ORDINARY_PROFILE_CHECK'
    $localData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    foreach ($suffix in @('Google\Chrome\User Data','Google\Chrome Beta\User Data','Google\Chrome Dev\User Data','Google\Chrome SxS\User Data','Google\Chrome for Testing\User Data','Chromium\User Data')) {
        $ordinary = Get-CdpdPath ([IO.Path]::Combine($localData, $suffix))
        if ((Test-CdpdRelatedPath $ordinary $script:CdpdProfile) -or (Test-CdpdRelatedPath $ordinary $script:CdpdState)) { Stop-Cdpd 'PROFILE_COLLISION' }
    }
    Set-CdpdStage 'BROWSER_POLICY_CHECK'
    # An enterprise UserDataDir policy can override the requested command line.
    foreach ($policy in @('HKCU:\Software\Policies\Google\Chrome','HKLM:\Software\Policies\Google\Chrome','HKCU:\Software\Policies\Chromium','HKLM:\Software\Policies\Chromium')) {
        if (Test-Path -LiteralPath $policy) {
            $values = Get-ItemProperty -LiteralPath $policy -ErrorAction Stop
            if ($null -ne $values.PSObject.Properties['UserDataDir']) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
        }
    }
    $script:CdpdLaunchModulePath = Join-Path $PSScriptRoot 'isolated_launch.cs'
    Set-CdpdStage 'HASH_INITIALIZATION'
    $script:CdpdHashes = [ordered]@{
        profile_path_sha256=(Get-CdpdTextHash $script:CdpdProfile.ToLowerInvariant());
        executable_path_sha256=(Get-CdpdTextHash $script:CdpdChrome.ToLowerInvariant());
        state_directory_sha256=(Get-CdpdTextHash $script:CdpdState.ToLowerInvariant());
        owner_sid_sha256=(Get-CdpdTextHash $script:CdpdOwnerSid)
    }
    $script:CdpdMarkerPath = [IO.Path]::Combine($script:CdpdProfile, '.cdpd-profile-v1.json')
    $script:CdpdRecordPath = [IO.Path]::Combine($script:CdpdState, 'launch_record.json')

    # Under the same mutex and before any profile/state creation. The case
    # cannot silently switch from New to Reuse, or Reuse to New.
    $caseListeners = @(Get-CdpdListeners)
    $script:CdpdListenerCountBefore=$caseListeners.Count
    Assert-CdpdPortExpectation $caseListeners.Count $ExpectedPortState

    if ($Mode -cin @('Probe','Environment')) {
        $script:CdpdPhase = $Mode.ToUpperInvariant()
        if ($caseListeners.Count -gt 0) {
            [void] (Get-CdpdObservation 'INSPECTED')
            $readiness = 'CAN_REUSE'
        } else {
            [void] (Get-CdpdStartContext)
            $readiness = 'CAN_START'
        }
        # Deliberately NOT chrome-launch-observation/v1: readiness is no permit.
        $outcome = [ordered]@{
            schema='cdpd.chrome-launch-readiness/v1'; status='PASS'; action=$readiness;
            endpoint=$Endpoint; browser_operation_performed=$false;
            evidence_kind='WINDOWS_OS_READINESS_ONLY'
        }
        if ($Mode -ceq 'Environment') {
            $outcome=[ordered]@{schema='cdpd.selected-profile-environment/v1';status='PASS';reason_code='NONE';
                scope='SELECTED_PROFILE_READINESS_NOT_LAUNCH_ACCEPTANCE';action=$readiness;endpoint=$Endpoint;
                browser_operation_performed=$false;diagnostics=[ordered]@{elevated=$script:CdpdCallerElevated;
                    listener_count=$script:CdpdListenerCountBefore;profile_scope=$script:CdpdProfileScope;ambient_metadata_required=$false}}
        }
    } elseif ($Mode -ceq 'Inspect') {
        $script:CdpdPhase = 'INSPECT'
        $outcome = Get-CdpdObservation 'INSPECTED'
    } else {
        $script:CdpdPhase = 'PRE_START'
        $existingListeners = $caseListeners
        if ($existingListeners.Count -gt 0) {
            # No filesystem writes or launch arguments are sent to an existing browser.
            $script:CdpdPhase = 'REUSE'
            $outcome = Get-CdpdObservation 'REUSED'
        } else {
            $context = Get-CdpdStartContext
            $marker = $context.Marker
            Set-CdpdStage 'STORAGE_CREATE'
            $script:CdpdFailure = 'STORAGE_FAILED'
            [void] [IO.Directory]::CreateDirectory($script:CdpdProfile)
            [void] [IO.Directory]::CreateDirectory($script:CdpdState)
            Assert-CdpdOwnedDirectory $script:CdpdProfile
            Assert-CdpdStateContents
            if ($null -eq $marker) {
                if (@([IO.Directory]::EnumerateFileSystemEntries($script:CdpdProfile)).Count -ne 0) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
                $newMarker = [ordered]@{
                    schema='cdpd.chrome-profile-marker/v1'; profile_id=[Guid]::NewGuid().ToString('D');
                    created_at_utc=[DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture);
                    profile_path_sha256=$script:CdpdHashes.profile_path_sha256;
                    executable_path_sha256=$script:CdpdHashes.executable_path_sha256;
                    state_directory_sha256=$script:CdpdHashes.state_directory_sha256;
                    owner_sid_sha256=$script:CdpdHashes.owner_sid_sha256
                }
                Set-CdpdStage 'MARKER_WRITE'
                Write-CdpdAtomicDocument $script:CdpdMarkerPath $newMarker
                $marker = Get-CdpdMarker
            }
            Assert-CdpdOriginStorage $script:CdpdState
            Assert-CdpdOriginStorage $script:CdpdMarkerPath
            $script:CdpdPhase = 'PRE_LAUNCH_RECHECK'
            # Recheck immediately before the one permitted launch; do not kill a race winner.
            Assert-CdpdNoProfileCollision
            if (@(Get-CdpdListeners).Count -gt 0) { Stop-Cdpd 'PORT_OCCUPIED' }
            [void] (Assert-CdpdNoReparse $script:CdpdChrome)
            Assert-CdpdOwnedDirectory $script:CdpdProfile
            # FIX007: Chrome must not inherit the Python -> PowerShell result pipes.
            # NUL-only HANDLE_LIST, no output readers and no lifetime-coupled Dispose.
            if ($null -eq ('CdpdL03.Fix007.IsolatedLaunch' -as [type])) {
                Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'isolated_launch.cs'))) -ErrorAction Stop | Out-Null
            }
            $launchArguments = Get-CdpdManagedArguments
            $script:CdpdBrowserSpawnAttempted = $true
            Set-CdpdStage 'START_PROCESS'
            $script:CdpdFailure = 'STARTUP_FAILED'
            $script:CdpdStartedProcess = [CdpdL03.Fix007.IsolatedLaunch]::Start($script:CdpdChrome, $launchArguments)
            if ($null -eq $script:CdpdStartedProcess) { Stop-Cdpd 'STARTUP_FAILED' }
            $script:CdpdPhase = 'POST_START'
            Set-CdpdStage 'STARTUP_WAIT'
            $startedId = $script:CdpdStartedProcess.Id
            $clock = [Diagnostics.Stopwatch]::StartNew()
            $ready = $false
            do {
                if ($script:CdpdStartedProcess.HasExited) { Stop-Cdpd 'STARTUP_FAILED' }
                $listeners = @(Get-CdpdListeners)
                if ($listeners.Count -gt 0) {
                    $listener = Get-CdpdSingleListener
                    if ([long] $listener.OwningProcess -ne [long] $startedId) { Stop-Cdpd 'PORT_OCCUPIED' }
                    $ready = $true
                    break
                }
                if ($clock.Elapsed.TotalSeconds -ge $startupSeconds) { break }
                Start-Sleep -Milliseconds 100 # Bounded OS readiness polling, never a Web retry.
            } while ($clock.Elapsed.TotalSeconds -lt $startupSeconds)
            if (-not $ready) { Stop-Cdpd 'STARTUP_TIMEOUT' }
            $script:CdpdFailure = 'PROFILE_UNVERIFIED'
            # FIX008: an actual successful CreateProcess return is the only
            # source of a NEW origin record. Never reconstruct it from a PID/file.
            $currentMarker = Get-CdpdMarker
            if ($currentMarker.Hash -cne $marker.Hash) { Stop-Cdpd 'PROFILE_UNVERIFIED' }
            Assert-CdpdStateContents
            $record = New-CdpdOriginRecord $marker $script:CdpdStartedProcess
            [void] (Get-CdpdManagedNativeProcess $record $script:CdpdStartedProcess)
            # Verify the selected profile's actual owner before committing a record.
            Assert-CdpdNoProfileCollision $startedId
            $script:CdpdFailure = 'STORAGE_FAILED'
            Set-CdpdStage 'RECORD_WRITE'
            Write-CdpdAtomicDocument $script:CdpdRecordPath $record
            $script:CdpdFailure = 'PROFILE_UNVERIFIED'
            Set-CdpdStage 'FINAL_OBSERVATION'
            $outcome = Get-CdpdObservation 'STARTED'
        }
    }
    $exitCode = 0
} catch {
    $outcome = Get-CdpdFailureObservation $_
    $exitCode = 2
} finally {
    try { Set-CdpdStage 'FINALIZE_PROCESS_HANDLE' } catch { $outcome = Get-CdpdFailureObservation $_; $exitCode = 2 }
    if ($null -ne $script:CdpdStartedProcess) { try { $script:CdpdStartedProcess.Dispose() } catch {} }
    if ($null -ne $script:CdpdMutex) {
        try { if ($script:CdpdMutexOwned) { $script:CdpdMutex.ReleaseMutex() } } catch {}
        try { $script:CdpdMutex.Dispose() } catch {}
    }
}
try { Set-CdpdStage 'RESULT_EMIT' } catch { $outcome = Get-CdpdFailureObservation $_; $exitCode = 2 }
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $outcome -Compress -Depth 6))
exit $exitCode

"""Static Scheduler permission/configuration checks; not native Windows proof.

The separate l08_scheduler_mock.ps1 executes mocked cmdlets on Windows. Static
checks here intentionally do not claim PowerShell parsing, service registration,
Windows argv round-trip, automatic startup, or any native filesystem acceptance.
"""
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "register_monitor_task.ps1"
MOCK = ROOT / "tests" / "l08_scheduler_mock.ps1"


def source():
    text = SCRIPT.read_text(encoding="utf-8")
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_l08_scheduler_registration_has_explicit_and_whatif_boundaries():
    text = source()
    guard = text.index("if (-not $Register)")
    whatif = text.index("$PSCmdlet.ShouldProcess(")
    register = text.index("= Register-ScheduledTask ")
    first_scheduler_api = text.index("@(Get-ScheduledTask ")
    assert guard < whatif < first_scheduler_api < register
    assert "SupportsShouldProcess = $true" in text
    assert re.search(r"if \(-not \$Register\)\s*\{[^}]*\breturn\s*\}", text)
    assert re.search(r"if \(-not \$PSCmdlet.ShouldProcess\([^\n]+\)\)\s*\{[^}]*\breturn\s*\}", text)
    assert text.count("= Register-ScheduledTask ") == 1


def test_l08_scheduler_default_disabled_and_enable_requires_register():
    text = source()
    assert "if ($EnableAutomaticStart -and -not $Register)" in text
    assert "throw 'ENABLE_REQUIRES_EXPLICIT_REGISTER'" in text
    assert "$monitorDisabled = -not [bool]$EnableAutomaticStart" in text
    assert "-Disable:$monitorDisabled" in text
    assert not re.search(r"\[switch\]\$(?:Register|EnableAutomaticStart)\s*=", text)


@pytest.mark.parametrize("operation", [
    "Start-ScheduledTask", "Enable-ScheduledTask", "Set-ScheduledTask",
    "Unregister-ScheduledTask", "Start-Process", "Invoke-Expression",
    "Invoke-WebRequest", "Invoke-RestMethod",
])
def test_l08_scheduler_has_no_implicit_operational_side_effect(operation):
    assert operation not in source()


def test_l08_scheduler_fixed_action_and_hash_pin():
    text = source()
    assert "(Join-Path $PSScriptRoot 'run_l08_monitor.py')" in text
    assert "'--config', $monitorConfig" in text
    assert "'--expected-config-sha256', $ConfigSha256" in text
    assert text.count("-Algorithm SHA256") == 2
    assert "throw 'CONFIGURATION_SHA256_MISMATCH'" in text
    assert "throw 'CONFIGURATION_CHANGED_BEFORE_REGISTRATION'" in text
    assert "ConvertFrom-Json" not in text
    assert not re.search(r"(?im)^\s*&\s*", text)
    assert "-Command" not in text
    assert "-EncodedCommand" not in text


def test_l08_scheduler_no_retry_no_catchup_no_registration_trigger():
    text = source()
    assert "-MultipleInstances IgnoreNew" in text
    assert "-StartWhenAvailable:$false" in text
    assert "-WakeToRun:$false" in text
    assert "-Daily -At $monitorFirstRun -DaysInterval $DaysInterval" in text
    assert "-RestartCount" not in text
    assert "-RestartInterval" not in text
    assert "-AtLogon" not in text
    assert "-AtStartup" not in text
    assert "RegistrationTrigger" not in text
    assert "-RepetitionInterval" not in text


def test_l08_scheduler_uses_interactive_limited_identity_without_password():
    text = source()
    assert "-LogonType Interactive -RunLevel Limited" in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "-Password" not in text
    assert "ServiceAccount" not in text
    assert "S4U" not in text
    assert "Highest" not in text


def test_l08_scheduler_existing_task_is_not_overwritten():
    text = source()
    check = text.index("if ($monitorExisting.Count -ne 0)")
    registration = text.index("= Register-ScheduledTask ")
    assert check < registration
    assert "throw 'EXISTING_TASK_NOT_OVERWRITTEN'" in text
    register_line = text[registration:].splitlines()[0]
    assert "-Force" not in register_line
    assert "-TaskPath '\\'" in register_line


@pytest.mark.parametrize("parameter, valid", [
    ("TaskName", "CDPD-L08-Mock"), ("ConfigSha256", "a" * 64),
])
def test_l08_scheduler_parameter_patterns_reject_terminal_control_characters(parameter, valid):
    """Inspect admitted regexes, not native PowerShell parameter binding.

    Python's strict \\Z is used only to model .NET's strict \\z for these simple
    ASCII patterns. In .NET, $ accepts a position before a terminal newline.
    """
    pattern = re.search(
        r"\[ValidatePattern\('([^']+)'\)\]\s*\[string\]\$" + parameter + r"\b",
        source(),
    ).group(1)
    assert pattern.startswith(r"\A") and pattern.endswith(r"\z")
    portable = pattern.replace(r"\z", r"\Z")
    assert re.search(portable, valid)
    for invalid in (valid + "\n", "\n" + valid, valid + "\r\n", valid + "\x00", valid + "*"):
        assert re.search(portable, invalid) is None


def test_l08_scheduler_local_paths_and_future_time_are_explicit():
    text = source()
    assert "-LiteralPath" in text
    assert "'^[A-Za-z]:[\\\\/]'" in text
    assert "Substring(2).Contains(':')" in text
    assert "[System.IO.FileAttributes]::ReparsePoint" in text
    assert "throw 'REPARSE_PATH_NOT_ACCEPTED'" in text
    assert "throw 'NONCANONICAL_LOCAL_PATH'" in text
    assert "[datetime]::Now.AddMinutes(1)" in text
    assert "[System.DateTimeKind]::Utc" in text
    assert "$monitorFirstRun.ToLocalTime()" in text
    assert "[ValidateRange(1, 365)]" in text
    assert "-WorkingDirectory $monitorDirectory" in text


def test_l08_scheduler_mock_is_separate_and_does_not_claim_native_registration():
    text = MOCK.read_text(encoding="utf-8")
    assert "function Register-ScheduledTask" in text
    assert "MOCK_ONLY_NO_NATIVE_REGISTRATION" in text
    assert "NOT_RUN" in text
    assert "function Start-ScheduledTask" in text
    assert "UNEXPECTED_START" in text
    assert "Assert-L08 ($script:Calls.Register -eq 0)" in text


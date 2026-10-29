# L08 Scheduler definition and execution boundary

## Contract and scope

`scripts/register_monitor_task.ps1` creates a reviewable definition by default.
It does not register, enable, launch, or invoke a browser during that operation.
The fixed action is the sibling `scripts/run_l08_monitor.py`, invoked through an
explicit local Python executable with `--config` and
`--expected-config-sha256`. The script verifies configuration bytes and does not
interpret configuration content as authority.

Original requirements are `tickets/CDPD-L08.md` AC04 and DIF-05;
`docs/04_Acceptance_Test_Matrix.md` explicitly makes actual registration optional.
The approved L02 `diagnostic_profile_limits.json` sets
`scheduler_default_enabled=false`. No prior acceptance or source is changed.

### Definition, registration, automatic start

| Operation | Required input | Effect |
|---|---|---|
| Definition generation | paths, SHA-256, time and interval | JSON plan on stdout; no Scheduler API |
| Disabled registration | explicit `-Register` | new disabled task; no immediate start |
| Enabled future registration | explicit `-Register -EnableAutomaticStart` | future calendar start is enabled; no immediate-start call |
| WhatIf | `-Register -WhatIf` | plan only; no Scheduler API |

`-EnableAutomaticStart` alone is rejected. The script supports ShouldProcess,
but does not add a second compulsory interactive prompt to an explicit command.
No operation is performed as part of this Candidate delivery.

The first registration time must be at least one minute in the future. The
trigger is one daily-interval trigger; the default interval is **30 days**, not
calendar-month semantics. There are no registration, logon, startup, catch-up,
or repetition triggers. An existing same-name root task is rejected; no Force,
Set, update, or delete operation is available.

The task uses the current user's `Interactive` principal with `Limited`
privileges, `IgnoreNew`, no stored password, no wake, and no catch-up. Failure
restart options are omitted entirely. The five-minute Scheduler time limit is
an external operating limit; it is not evidence of Supervisor deadline,
descendant termination, COMMIT atomicity, or native Windows acceptance.

### Offline replay and scheduled capture

The root entrypoint validates the pinned configuration and owns the distinction:

- `OFFLINE_REPORT` refers to an explicitly selected already committed bundle.
  Replaying it creates no new browser observation or capture authority.
- `SCHEDULED_CAPTURE` fails closed while trusted live Supervisor / idle-proof
  integration remains unavailable. Saved `idle` or browser-running flags cannot
  grant that authority. A read-only observation of absent Chrome is
  SKIPPED/BLOCKED, never PASS.
- Neither mode can approve or overwrite an accepted Baseline, import arbitrary
  configuration-named adapters, or rebind/retarget/recapture an exhausted request.

The registrar deliberately does not parse those modes. Its registration
permission is never proof of a valid runtime capture permission. The entrypoint
rechecks the raw configuration SHA on each invocation; changes invalidate that
invocation instead of granting a new action.

### Paths and arguments

Only nonempty regular files on absolute local drive paths are admitted. UNC,
device, drive-relative, alternate-stream, wildcard/control, noncanonical segment,
and observed reparse-component paths are rejected. Inputs are resolved with
literal paths. The entrypoint is fixed by the script's own directory, not an
arbitrary user module or command. Arguments are individually quoted according to
the Windows C-runtime convention; no shell or `-Command` is introduced.

These preflight checks are not a claim of adversarial native filesystem race or
ACL validation. Existing L03-L07 native NTFS/ACL/reparse/Cloud Files limits remain.
The emitted plan includes operator-selected local paths and is not the
privacy-filtered `report.json` / `report.md`; it should not be treated as one.

## Verification and limits

`tests/test_l08_scheduler_script.py` checks the written permission boundary,
fixed action, pin verification, no retry/catch-up, principal, no overwrite,
and prohibited operational calls. These are **static checks**.

`tests/l08_scheduler_mock.ps1` is a separate dependency-free Windows harness. It
shadows every Scheduler cmdlet used by the registrar and fails if immediate
task/process startup is attempted. Its synthetic executable is never launched;
fixtures live in a unique temporary directory. It covers definition-only,
WhatIf, rejected enable-only, disabled and explicitly enabled registration,
existing-task refusal, wrong configuration hash, UNC path refusal, and past-time
refusal. Even a future mock PASS is not actual registration/startup evidence.

At Candidate construction, this host has neither `pwsh` nor `powershell`.
PowerShell parsing/dynamic mocks, actual registration/readback/automatic start,
native Windows command-line round-trip, and login-session behavior are NOT_RUN.
Scheduler remains unregistered. No service, browser, Web, or SHIRABE operation
was performed for this implementation.

## Microsoft primary references

Documentation was read on 2026-10-29. The following observations informed the
implementation; these sources do not substitute for native test evidence.

1. [New-ScheduledTask](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtask?view=windowsserver2025-ps):
   creating a definition is separate from registering it.
2. [Register-ScheduledTask](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask?view=windowsserver2025-ps):
   registration stores a local task definition; executable compatibility is not
   verified by the cmdlet. Therefore a generated script is not native execution
   proof.
3. [New-ScheduledTaskSettingsSet](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset?view=windowsserver2025-ps):
   IgnoreNew declines overlapping starts; RestartCount/RestartInterval request
   retries; StartWhenAvailable permits late starts. The latter options are not
   enabled.
4. [RestartOnFailure Count schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-count-restarttype-element):
   Count has minimum one. Omitting RestartOnFailure is appropriate; emitting
   Count=0 is not a valid no-retry XML representation.
5. [TASK_LOGON_TYPE](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type):
   InteractiveToken requires an already logged-in user and an existing
   interactive session. No service-account or password fallback is introduced.
6. [New-ScheduledTaskPrincipal](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal?view=windowsserver2025-ps):
   supplies the selected account, logon type, and privilege level.
7. [New-ScheduledTaskAction](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskaction?view=windowsserver2019-ps):
   action executable, arguments, and working directory are separate fields.
   Omitted working directories default to Windows system32, so one is explicit.
8. [New-ScheduledTaskTrigger](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger?view=windowsserver2025-ps):
   supports once/daily/weekly/startup/logon parameter sets, not a Monthly switch.
9. [ScheduleByMonth XML](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-schedulebymonth-calendartriggertype-element):
   true calendar-month schedules use a different definition. No such requirement
   is invented for this original L08 implementation.
10. [TASK_CREATION](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_creation):
    disabled registration cannot execute until enabled. Registration and
    time-trigger execution can otherwise coincide; registration-then-disable
    is therefore not used.
11. [Windows C command-line parsing](https://learn.microsoft.com/en-us/cpp/c-language/parsing-c-command-line-arguments?view=msvc-170):
    quoting groups whitespace and backslashes before a quote require careful
    handling. Inputs containing quotes/controls are rejected, and admitted
    arguments are separately quoted.
12. [.NET regular-expression anchors](https://learn.microsoft.com/en-us/dotnet/standard/base-types/anchors-in-regular-expressions):
    `$` may match immediately before a final LF. TaskName and configuration hash
    parameter patterns therefore use strict `\A` / `\z` anchors. Static regex
    regressions and later Windows mock cases cover terminal control characters.

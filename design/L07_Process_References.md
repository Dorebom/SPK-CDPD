# L07 owned process boundary — implementation decisions and source references

Scope: new `owned_process.py` and `_l07_win32.py`. Accepted L06 modules remain
unchanged. These are Execution implementation decisions, not independent
Assessment or Windows native acceptance.

## Windows 11 process ownership

The Supervisor creates one unnamed, noninheritable Job Object per Worker.
`JOBOBJECT_EXTENDED_LIMIT_INFORMATION.BasicLimitInformation.LimitFlags` is
exactly `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (`0x2000`); both breakaway flags
remain unset. The limits and noninheritability are read back before launch.

The Win32 adapter creates only dedicated stdin/stdout pipe handles and a
discard-only NUL stderr handle. `STARTUPINFOEX` carries both
`PROC_THREAD_ATTRIBUTE_JOB_LIST` and `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`.
`CreateProcessW` uses an explicit executable, mutable Unicode command buffer,
`CREATE_SUSPENDED`, `EXTENDED_STARTUPINFO_PRESENT`, `CREATE_UNICODE_ENVIRONMENT`
and `CREATE_NO_WINDOW`. The Job handle is excluded from the inherited handle
list, and process/thread handles are created noninheritable.

Job assignment therefore occurs during process creation. The Supervisor checks
membership, the fixed absolute deadline and cancellation before `ResumeThread`.
It never falls back to starting an uncontained Worker. The initial thread handle
is retained until resume; plain `subprocess.Popen(CREATE_SUSPENDED)` is not used
because CPython closes that handle. The independent deadline monitor starts as
soon as returned handles are owned, before membership/resume operations.

This strengthens L02's suspended → Job assignment → resume sequence: the usual
gap between suspended creation and later assignment can leave an orphan if the
Supervisor dies in that gap. Windows 10's Job-list attribute removes that
application-level gap. No claim is made about cancelling an OS process-creation
syscall while it has not returned. A late returned process is not resumed;
the caller can independently seal its attempt as unconfirmed.

`kill()` targets the saved Job handle only. The exceptional failed-membership
cleanup may also terminate the exact process handle returned by CreateProcess;
it never reopens or kills a process by name/PID. `terminate_and_confirm()` checks
the leader's held handle and the Job's `ActiveProcesses == 0`, for at most two
seconds. API return success alone is insufficient. An unconfirmed result is
sealed and does not become confirmed on a later call. Closing the Job remains a
cleanup action and does not upgrade that evidence.

`RegisteredProcess` exposes only read-only liveness. Windows registration opens
a fixed handle with `SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION`, checks
its creation FILETIME against the caller's expected identity, then polls that
same handle. There is no terminate method for registered Chrome or Consumer.

## Deadline and thread boundaries

The absolute monotonic deadline is supplied by the runner and is never reset by
poll, wait, IPC, heartbeat, phase transitions or cleanup. A daemon monitor polls
at at most 50 ms and requests termination without running DOM, filesystem
publication or protocol callbacks. Concurrent explicit termination uses the
same object lock and requests only one kill. The runner must preserve an
unconfirmed latch and must not accept a temporary result solely because the
leader exited zero when descendants required termination.

Scheduler stalls, blocked native APIs, process creation itself, failed native
termination and global OS failure are not absolute real-time guarantees.
Such outcomes must remain unconfirmed/partial. Native evidence is still needed
for actual Windows Job teardown, descendant lifetimes, ACLs and syscall faults.

## Linux synthetic controls and limits

Linux launches a new session/process group. A separate Python exec wrapper
sets `PR_SET_PDEATHSIG(SIGKILL)`, checks that its parent PID still equals the
expected Supervisor in the same namespace, and then `execve`s the exact Worker
argv. The PID, group and standard pipes survive exec; no second Worker or
protocol output is introduced. Wrapper setup failure or changed parent exits
without executing the Worker. The implementation does not use `preexec_fn`,
which CPython documents as unsafe in a multithreaded parent.

Linux sends this signal when the creating thread dies, even when another
Supervisor thread remains alive. The runner's communication thread therefore
stays alive while it needs its Worker. Early thread death may terminate the
leader conservatively. The setting survives ordinary exec, but is cleared in
forked descendants and by credential-changing exec or credential transitions;
this backend is for the cooperative, nonprivileged synthetic Worker.

`waitid(..., WNOWAIT)` observes the
owned leader without reaping it until group operations finish, preventing PID/
PGID reuse while the object is live. Only that dedicated group receives SIGKILL.
Group confirmation counts non-zombie/non-dead processes; it is not a claim that
every descendant kernel record has already been reaped. A deliberately escaping
descendant using a new session is outside this cooperative synthetic backend.
Uncatchable Supervisor death stops the armed leader, but does not promise Linux
whole-group or descendant cleanup. Windows
KILL_ON_JOB_CLOSE is not inferred from these tests.

Registered Linux processes require `pidfd_open` and a `/proc` start-time identity.
An exit before/during registration is rejected; no PID-only fallback exists.
The backend checks that `/proc/self/stat` agrees with `os.getpid()`. A sandbox
whose PID namespace differs from its mounted `/proc` fails closed with
`PROC_PID_NAMESPACE_MISMATCH` before creating a child. Real process tests must
run in an authorized environment with matching namespaces; this guard is not
relaxed to make the tests pass.

## Primary sources inspected for this implementation

Accessed 2026-10-25. These documents explain APIs; they are not runtime Evidence.

1. Microsoft, [UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute): Job-list support, handle allowlists, attribute value lifetime.
2. Microsoft, [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw): explicit application name, mutable command buffer, inheritance and startup flags.
3. Microsoft, [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects): Job membership, process trees and breakaway rules.
4. Microsoft, [JOBOBJECT_BASIC_LIMIT_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information): KILL_ON_JOB_CLOSE and CPU-time limits. CPU-time limits do not implement the wall-clock deadline.
5. Microsoft, [TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject): termination scope includes nested child jobs.
6. Microsoft, [JOBOBJECT_BASIC_ACCOUNTING_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information): ActiveProcesses meaning.
7. Microsoft, [GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes): fixed-handle creation FILETIME.
8. Microsoft, [A more direct and mistake-free way of creating a process in a job object](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812/): suspended-then-assign orphan window and Job-list solution.
9. CPython, [Lib/subprocess.py](https://github.com/python/cpython/blob/main/Lib/subprocess.py): Windows Popen closes the primary thread handle after creation.
10. Linux man-pages, [PR_SET_PDEATHSIG](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html): creating-thread death, the pre-arming parent-death gap, ordinary exec preservation, and fork/credential exceptions.
11. CPython, [subprocess](https://docs.python.org/3/library/subprocess.html): `preexec_fn` and threads, `start_new_session`, and standard pipe inheritance.

## Execution verification

`tests/test_l07_owned_process.py` includes injected Windows API sequence/failure
controls, direct ctypes-adapter configuration inspection using fake kernel
calls, and real Linux pipe/deadline/process-group/identity controls. The fake
kernel test checks the actual adapter's Job flags, creation flags, Job-list and
pipe handle allowlist; it does not validate Windows ABI or native behavior.
Linux controls also check post-exec PID/argv/cwd/environment/pipe continuity,
setup refusal, and actual Supervisor SIGKILL with leader termination. These
controls do not establish descendant cleanup after Supervisor death.

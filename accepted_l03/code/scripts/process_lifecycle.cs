// FIX004 Candidate: authoritative per-handle lifecycle observation, not a launch permit.
// Only PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE; wait timeout is always zero.
// No termination, privilege adjustment, process memory access or browser traffic.
using System;
using System.Runtime.InteropServices;
namespace CdpdL03.Fix004 {
    public sealed class LifecycleObservation {
        public string State = "UNAVAILABLE";
        public bool IdentityVerified = false;
        public string WaitState = "NOT_OBSERVED";
        public bool ExitTimeObserved = false;
    }
    public static class Lifecycle {
        private const uint LimitedQuery = 0x1000;
        private const uint Synchronize = 0x00100000;
        [StructLayout(LayoutKind.Sequential)]
        private struct FileTime { public uint Low; public uint High; }
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern uint GetProcessId(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetProcessTimes(IntPtr handle, out FileTime creation, out FileTime exit, out FileTime kernel, out FileTime user);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);
        private static bool Match(FileTime value, long expectedTicks) {
            try {
                ulong raw = ((ulong)value.High << 32) | value.Low;
                // Native 100ns -> WMI microsecond precision, not a loose ms tolerance.
                return DateTime.FromFileTimeUtc(checked((long)raw)).Ticks / 10 == expectedTicks / 10;
            } catch { return false; }
        }
        // Pure decision function supports deterministic component coverage without Win32 calls.
        // Exit code 259 is deliberately NOT consulted; it is also a possible real exit code.
        public static string Decide(bool identity, uint wait, bool timesRead, bool exitTimeNonzero) {
            if (!identity) return "IDENTITY_CHANGED";
            if (wait == 0xFFFFFFFFu) return "WAIT_FAILED";
            if (wait != 0u && wait != 258u) return "UNAVAILABLE";
            if (!timesRead) return "UNAVAILABLE";
            if (wait == 0u) return exitTimeNonzero ? "TERMINATED" : "INCONSISTENT";
            return exitTimeNonzero ? "INCONSISTENT" : "RUNNING";
        }
        public static LifecycleObservation Probe(int processId, long expectedCreatedUtcTicks) {
            var r = new LifecycleObservation();
            if (processId < 1 || expectedCreatedUtcTicks < 1) return r;
            IntPtr h = OpenProcess(LimitedQuery | Synchronize, false, (uint)processId);
            if (h == IntPtr.Zero) {
                // Open failure never means TERMINATED. PID may have been reused or hidden.
                int error = Marshal.GetLastWin32Error();
                r.State = error == 5 ? "ACCESS_DENIED" : "UNAVAILABLE";
                return r;
            }
            try {
                FileTime created, exited, kernel, user;
                if (GetProcessId(h) != (uint)processId ||
                    !GetProcessTimes(h, out created, out exited, out kernel, out user) ||
                    !Match(created, expectedCreatedUtcTicks)) {
                    r.State = "IDENTITY_CHANGED"; return r;
                }
                r.IdentityVerified = true;
                uint wait = WaitForSingleObject(h, 0);
                r.WaitState = wait == 0u ? "SIGNALED" : (wait == 258u ? "NOT_SIGNALED" : "FAILED");
                // Read exit time AFTER the zero-time wait, using the SAME owned handle.
                bool timesRead = GetProcessTimes(h, out created, out exited, out kernel, out user);
                if (!timesRead) { r.State = "UNAVAILABLE"; return r; }
                if (!Match(created, expectedCreatedUtcTicks)) { r.State = "IDENTITY_CHANGED"; r.IdentityVerified=false; return r; }
                r.ExitTimeObserved = exited.High != 0 || exited.Low != 0;
                r.State = Decide(true, wait, true, r.ExitTimeObserved);
                return r;
            } finally { CloseHandle(h); }
        }
    }
}

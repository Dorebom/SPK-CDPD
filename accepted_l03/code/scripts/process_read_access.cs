// FIX003: documented Windows read-only APIs. No privilege adjustment, memory
// reads, process writes, injection, termination, remote connections or paths in output.
using System;
using System.Runtime.InteropServices;
using System.Text;
namespace CdpdL03.Fix003 {
    public sealed class AccessObservation {
        public string State = "UNAVAILABLE";
        public bool ImageReadable = false;
        public string Elevation = "UNAVAILABLE";
    }
    public static class ReadAccess {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileTime { public uint Low; public uint High; }
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, uint processId);
        [DllImport("kernel32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetProcessTimes(IntPtr process, out FileTime creation, out FileTime exit, out FileTime kernel, out FileTime user);
        [DllImport("kernel32.dll", EntryPoint="QueryFullProcessImageNameW", CharSet=CharSet.Unicode, SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder image, ref uint size);
        [DllImport("advapi32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool OpenProcessToken(IntPtr process, uint desiredAccess, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetTokenInformation(IntPtr token, int informationClass, out int value, uint size, out uint returnedLength);
        public static AccessObservation Probe(int processId, long createdUtcTicks) {
            var result = new AccessObservation();
            if (processId <= 0 || createdUtcTicks <= 0) return result;
            // PROCESS_QUERY_LIMITED_INFORMATION only. TOKEN_QUERY only below.
            IntPtr process = OpenProcess(0x1000, false, (uint)processId);
            if (process == IntPtr.Zero) {
                int e = Marshal.GetLastWin32Error();
                result.State = e == 5 ? "ACCESS_DENIED" : (e == 87 || e == 1168 ? "IDENTITY_CHANGED" : "UNAVAILABLE");
                return result;
            }
            try {
                FileTime created, exited, kernel, user;
                if (!GetProcessTimes(process, out created, out exited, out kernel, out user)) return result;
                ulong raw = ((ulong)created.High << 32) | created.Low;
                long observedTicks;
                try { observedTicks = DateTime.FromFileTimeUtc(checked((long)raw)).Ticks; }
                catch { return result; }
                // WMI timestamps are microsecond precision. Reject other creation
                // instants, without accepting an arbitrary millisecond race window.
                if (observedTicks / 10 != createdUtcTicks / 10) {
                    result.State = "IDENTITY_CHANGED"; return result;
                }
                result.State = "VERIFIED";
                uint size = 32768;
                var image = new StringBuilder((int)size);
                result.ImageReadable = QueryFullProcessImageName(process, 0, image, ref size) && size > 0;
                image.Clear(); // never return or serialize a path
                IntPtr token;
                if (OpenProcessToken(process, 0x0008, out token)) {
                    try {
                        int elevated; uint returned;
                        // TOKEN_INFORMATION_CLASS.TokenElevation == 20
                        if (GetTokenInformation(token, 20, out elevated, 4, out returned) && returned == 4 && (elevated == 0 || elevated == 1))
                            result.Elevation = elevated == 1 ? "ELEVATED" : "STANDARD";
                    } finally { CloseHandle(token); }
                }
                return result;
            } finally { CloseHandle(process); }
        }
    }
}

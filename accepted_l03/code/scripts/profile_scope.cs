// CDPD-L03 FIX006 Candidate. Selected Chromium profile resource inspection only.
// No messages, launch, kill, network, file creation/deletion, or privilege adjustment.
// Chromium implementation contract, not a public Chrome API. Requires Windows build qualification.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
namespace CdpdL03.Fix006 {
    public sealed class ScopeSnapshot {
        public string State = "UNKNOWN";
        public string Detail = "PROFILE_SCOPE_UNAVAILABLE";
        public int TargetWindows;
        public int RelatedWindows;
        public int UnexpectedOwners;
        public string LockFile = "UNKNOWN";
        public bool ScanComplete;
        public bool StartupMutexAcquired;
    }
    public static class ProfileScope {
        private static readonly IntPtr MessageOnly = new IntPtr(-3);
        [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
        private static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string klass, string name);
        [DllImport("user32.dll", SetLastError=true)]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
        private static extern uint GetFileAttributesW(string path);
        [DllImport("kernel32.dll")] private static extern void SetLastError(uint error);
        public static string FileState(string path) {
            uint attributes=GetFileAttributesW(path);
            if(attributes==0xffffffff) {
                int error=Marshal.GetLastWin32Error();
                return error==2 || error==3 ? "ABSENT" : "UNKNOWN";
            }
            // Never follow a directory or a reparse lockfile.
            return (attributes & (0x10u | 0x400u)) != 0 ? "INVALID" : "PRESENT";
        }
        private static int Windows(string name, int allowed, out int wrong) {
            wrong=0; int count=0; IntPtr previous=IntPtr.Zero;
            var seen=new List<IntPtr>();
            while(count<3) {
                SetLastError(0);
                IntPtr next=FindWindowExW(MessageOnly,previous,"Chrome_MessageWindow",name);
                if(next==IntPtr.Zero) {
                    if(Marshal.GetLastWin32Error()!=0) throw new InvalidOperationException("Scope unavailable.");
                    return count;
                }
                if(seen.Contains(next)) throw new InvalidOperationException("Scope changed.");
                seen.Add(next);
                uint pid;
                if(GetWindowThreadProcessId(next,out pid)==0 || pid==0) throw new InvalidOperationException("Scope changed.");
                count++; if(allowed==0 || pid!=(uint)allowed) wrong++;
                previous=next;
            }
            return count; // 3 already proves ambiguity; no unbounded scan.
        }
        private static bool ValidPath(string value) {
            if(String.IsNullOrEmpty(value) || value.Length>240 || value.Length<4 || value[1]!=':' || value[2]!='\\') return false;
            if(value.IndexOfAny(new char[]{'\r','\n','\0','"','<','>','|','*','?'})>=0 || value.Substring(2).Contains(":")) return false;
            return String.Equals(Path.GetFullPath(value).TrimEnd('\\'),value,StringComparison.OrdinalIgnoreCase);
        }
        public static ScopeSnapshot Decide(ScopeSnapshot s, int allowed) {
            if(s==null) s=new ScopeSnapshot();
            s.State="UNKNOWN";s.Detail="PROFILE_SCOPE_UNAVAILABLE";
            if(allowed<0 || !s.StartupMutexAcquired || !s.ScanComplete || s.TargetWindows<0 || s.RelatedWindows<0 || s.UnexpectedOwners<0) return s;
            if(s.LockFile!="ABSENT" && s.LockFile!="PRESENT") return s;
            if(s.RelatedWindows>0 || s.UnexpectedOwners>0 || s.TargetWindows>1) {
                s.State="COLLISION";s.Detail="PROFILE_SCOPE_CONFLICT";return s;
            }
            if(allowed==0) {
                if(s.TargetWindows!=0 || s.LockFile!="ABSENT") { s.State="COLLISION";s.Detail="PROFILE_SCOPE_CONFLICT";return s; }
                s.State="FREE";s.Detail="NONE";return s;
            }
            if(s.TargetWindows==1 && s.LockFile=="PRESENT") {s.State="MANAGED";s.Detail="NONE";}
            else s.Detail="PROFILE_SCOPE_OWNER_NOT_WITNESSED";
            return s;
        }
        public static ScopeSnapshot Probe(string profile, string state, int allowed) {
            var result=new ScopeSnapshot();
            if(Environment.OSVersion.Platform!=PlatformID.Win32NT || !ValidPath(profile) || !ValidPath(state) || allowed<0) return result;
            bool owned=false;
            using(var mutex=new System.Threading.Mutex(false,"Local\\ChromeProcessSingletonStartup!")) {
                try {
                    try { owned=mutex.WaitOne(0); }
                    catch(System.Threading.AbandonedMutexException) { owned=true; result.Detail="CHROMIUM_STARTUP_MUTEX_ABANDONED";return result; }
                    if(!owned) {result.Detail="CHROMIUM_STARTUP_BUSY";return result;}
                    result.StartupMutexAcquired=true;
                    int wrong;
                    result.TargetWindows=Windows(profile,allowed,out wrong);
                    result.UnexpectedOwners=wrong;
                    // Scope ancestors only, not ambient process argv. Existing nonempty unmanaged
                    // directories are separately refused before Start by the launcher.
                    // Dictionary/List are in mscorlib on Windows PowerShell 5.1;
                    // do not depend on implicit System.Core references for HashSet.
                    var paths=new Dictionary<string,byte>(StringComparer.OrdinalIgnoreCase);
                    string cursor=Path.GetDirectoryName(profile);
                    while(!String.IsNullOrEmpty(cursor) && cursor.Length>=3) {
                        if(!paths.ContainsKey(cursor)) paths.Add(cursor,0);cursor=Path.GetDirectoryName(cursor);
                    }
                    cursor=state;
                    while(!String.IsNullOrEmpty(cursor) && cursor.Length>=3) {
                        if(!paths.ContainsKey(cursor)) paths.Add(cursor,0);cursor=Path.GetDirectoryName(cursor);
                    }
                    foreach(string path in paths.Keys) result.RelatedWindows+=Windows(path,0,out wrong);
                    result.LockFile=FileState(Path.Combine(profile,"lockfile"));
                    result.ScanComplete=true;
                    return Decide(result,allowed);
                } catch { result.State="UNKNOWN";result.Detail="PROFILE_SCOPE_UNAVAILABLE";return result; }
                finally { if(owned) mutex.ReleaseMutex(); }
            }
        }
    }
}

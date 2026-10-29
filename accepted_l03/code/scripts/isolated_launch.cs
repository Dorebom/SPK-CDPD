// FIX008 adds current native identity and typed creation provenance; FIX007 I/O isolation retained.
// FIX007: long-lived browser launch with an explicit HANDLE_LIST of three NUL
// handles. No pipe pumps, Process.WaitForExit(), tree kill, shell, or elevation.
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Globalization;
namespace CdpdL03.Fix007 {
    public sealed class LaunchedProcess : IDisposable {
        private IntPtr handle;
        public int Id { get; private set; }
        public long StartedFileTime { get; private set; }
        // Issued only after CreateProcessW succeeds. This is launch provenance,
        // NOT a read-back of the process's current command-line memory.
        public string ArgumentVectorSha256 { get; private set; }
        internal void SetArgumentEvidence(string digest) { ArgumentVectorSha256=digest; }
        public NativeIdentitySnapshot ObserveIdentity() {
            if(handle==IntPtr.Zero) throw new ObjectDisposedException("LaunchedProcess");
            return IsolatedLaunch.ObserveHandle(handle,Id,StartedFileTime);
        }
        internal LaunchedProcess(IntPtr value, int pid, long started) { handle=value;Id=pid;StartedFileTime=started; }
        public bool HasExited { get {
            if(handle==IntPtr.Zero) throw new ObjectDisposedException("LaunchedProcess");
            uint state=IsolatedLaunch.WaitForSingleObject(handle,0);
            if(state==0) return true;
            if(state==258) return false;
            throw new InvalidOperationException("PROCESS_STATE_UNAVAILABLE");
        } }
        public void Dispose() { if(handle!=IntPtr.Zero) { IsolatedLaunch.CloseHandle(handle);handle=IntPtr.Zero; } GC.SuppressFinalize(this); }
        ~LaunchedProcess() { Dispose(); }
    }
    // FIX008: one-handle current OS observation. Never serialize this raw object.
    public sealed class NativeIdentitySnapshot {
        public int ProcessId { get; internal set; }
        public long CreatedFileTime { get; internal set; }
        public string ExecutablePath { get; internal set; }
        public string OwnerSid { get; internal set; }
        public int SessionId { get; internal set; }
        public bool Elevated { get; internal set; }
        public bool Running { get; internal set; }
    }
    public static class IsolatedLaunch {
        [StructLayout(LayoutKind.Sequential)] private struct SA { public int Length;public IntPtr Descriptor;public int Inherit; }
        [StructLayout(LayoutKind.Sequential)] private struct SI {
            public int Size;public IntPtr Reserved,Desktop,Title; public uint X,Y,XSize,YSize,XChars,YChars,Fill,Flags;
            public short Show,ReservedBytes;public IntPtr ReservedData,Input,Output,Error;
        }
        [StructLayout(LayoutKind.Sequential)] private struct SIX { public SI Startup;public IntPtr Attributes; }
        [StructLayout(LayoutKind.Sequential)] private struct PI { public IntPtr Process,Thread;public uint Pid,Tid; }
        [StructLayout(LayoutKind.Sequential)] private struct FT { public uint Low,High;public long Value {get{return ((long)High<<32)|Low;}} }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern IntPtr CreateFileW(string n,uint access,uint share,ref SA sa,uint disposition,uint flags,IntPtr template);
        [DllImport("kernel32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool InitializeProcThreadAttributeList(IntPtr p,int count,int flags,ref UIntPtr size);
        [DllImport("kernel32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool UpdateProcThreadAttribute(IntPtr p,uint flags,IntPtr attribute,IntPtr value,UIntPtr size,IntPtr previous,IntPtr returned);
        [DllImport("kernel32.dll")] private static extern void DeleteProcThreadAttributeList(IntPtr p);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool CreateProcessW(string application,StringBuilder command,IntPtr processAttrs,IntPtr threadAttrs,[MarshalAs(UnmanagedType.Bool)] bool inherit,uint flags,IntPtr environment,string cwd,ref SIX startup,out PI process);
        [DllImport("kernel32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool GetProcessTimes(IntPtr process,out FT creation,out FT exit,out FT kernel,out FT user);
        [DllImport("kernel32.dll",SetLastError=true)] internal static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
        [DllImport("kernel32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] internal static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll",SetLastError=true)] private static extern IntPtr OpenProcess(uint access,[MarshalAs(UnmanagedType.Bool)] bool inherit,int pid);
        [DllImport("kernel32.dll",SetLastError=true)] private static extern uint GetProcessId(IntPtr process);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool QueryFullProcessImageNameW(IntPtr process,uint flags,StringBuilder image,ref uint length);
        [DllImport("advapi32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
        [DllImport("advapi32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool GetTokenInformation(IntPtr token,int kind,IntPtr data,uint length,out uint required);
        private static uint TokenWord(IntPtr token,int kind) {
            IntPtr memory=Marshal.AllocHGlobal(4);
            try {
                uint used;
                if(!GetTokenInformation(token,kind,memory,4,out used) || used!=4) throw new InvalidOperationException("NATIVE_TOKEN_UNAVAILABLE");
                return unchecked((uint)Marshal.ReadInt32(memory));
            } finally {Marshal.FreeHGlobal(memory);}
        }
        private static string TokenOwner(IntPtr token) {
            uint bytes;
            bool first=GetTokenInformation(token,1,IntPtr.Zero,0,out bytes); // TokenUser
            if(first || Marshal.GetLastWin32Error()!=122 || bytes<(uint)IntPtr.Size || bytes>65536) throw new InvalidOperationException("NATIVE_OWNER_UNAVAILABLE");
            IntPtr memory=Marshal.AllocHGlobal(checked((int)bytes));
            try {
                uint used;
                if(!GetTokenInformation(token,1,memory,bytes,out used) || used>bytes || used<(uint)IntPtr.Size) throw new InvalidOperationException("NATIVE_OWNER_UNAVAILABLE");
                IntPtr sid=Marshal.ReadIntPtr(memory);
                if(sid==IntPtr.Zero) throw new InvalidOperationException("NATIVE_OWNER_UNAVAILABLE");
                return new SecurityIdentifier(sid).Value;
            } finally {Marshal.FreeHGlobal(memory);}
        }
        internal static NativeIdentitySnapshot ObserveHandle(IntPtr process,int expectedPid,long expectedCreation) {
            if(process==IntPtr.Zero || expectedPid<1 || expectedCreation<1) throw new ArgumentException("NATIVE_IDENTITY_INVALID");
            FT creation,exit,kernel,user;
            if(GetProcessId(process)!=(uint)expectedPid || !GetProcessTimes(process,out creation,out exit,out kernel,out user)) throw new InvalidOperationException("NATIVE_IDENTITY_UNAVAILABLE");
            if(creation.Value!=expectedCreation) throw new InvalidOperationException("NATIVE_IDENTITY_CHANGED");
            uint wait=WaitForSingleObject(process,0);
            if(wait==0) throw new InvalidOperationException("NATIVE_PROCESS_TERMINATED");
            if(wait!=258) throw new InvalidOperationException("NATIVE_LIVENESS_UNAVAILABLE");
            var image=new StringBuilder(32768);uint length=32768;
            if(!QueryFullProcessImageNameW(process,0,image,ref length) || length<1 || length>=32768 || image.Length!=length) throw new InvalidOperationException("NATIVE_IMAGE_UNAVAILABLE");
            IntPtr token=IntPtr.Zero;
            try {
                if(!OpenProcessToken(process,0x0008,out token)) throw new InvalidOperationException("NATIVE_OWNER_UNAVAILABLE"); // TOKEN_QUERY only
                string owner=TokenOwner(token);
                uint session=TokenWord(token,12); // TokenSessionId, read on the same process token
                uint elevated=TokenWord(token,20); // TokenElevation
                if(session>Int32.MaxValue || elevated>1) throw new InvalidOperationException("NATIVE_TOKEN_UNAVAILABLE");
                FT after,afterExit,afterKernel,afterUser;
                if(!GetProcessTimes(process,out after,out afterExit,out afterKernel,out afterUser) || after.Value!=expectedCreation) throw new InvalidOperationException("NATIVE_IDENTITY_CHANGED");
                if(WaitForSingleObject(process,0)!=258) throw new InvalidOperationException("NATIVE_PROCESS_TERMINATED");
                return new NativeIdentitySnapshot { ProcessId=expectedPid,CreatedFileTime=expectedCreation,
                    ExecutablePath=image.ToString(),OwnerSid=owner,SessionId=checked((int)session),Elevated=elevated!=0,Running=true };
            } finally {if(token!=IntPtr.Zero)CloseHandle(token);}
        }
        public static NativeIdentitySnapshot ObserveCurrent(int expectedPid,long expectedCreation) {
            if(Environment.OSVersion.Platform!=PlatformID.Win32NT) throw new PlatformNotSupportedException();
            if(expectedPid<1 || expectedCreation<1) throw new ArgumentException("NATIVE_IDENTITY_INVALID");
            // No debug privilege, memory read, all-access handle, or PID-only adoption.
            IntPtr handle=OpenProcess(0x00100000u|0x00001000u,false,expectedPid); // SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            if(handle==IntPtr.Zero) throw new InvalidOperationException("NATIVE_PROCESS_OPEN_UNAVAILABLE");
            try {return ObserveHandle(handle,expectedPid,expectedCreation);}
            finally {CloseHandle(handle);}
        }
        public static string ObserveRecordedState(int expectedPid,long expectedCreation) {
            if(expectedPid<1 || expectedCreation<1) throw new ArgumentException("NATIVE_IDENTITY_INVALID");
            IntPtr handle=OpenProcess(0x00100000u|0x00001000u,false,expectedPid);
            if(handle==IntPtr.Zero) return "UNKNOWN"; // open failure is NOT proof of exit
            try {
                FT creation,exit,kernel,user;
                if(GetProcessId(handle)!=(uint)expectedPid || !GetProcessTimes(handle,out creation,out exit,out kernel,out user))return "UNKNOWN";
                if(creation.Value!=expectedCreation)return "IDENTITY_CHANGED";
                uint wait=WaitForSingleObject(handle,0);
                if(wait==258)return "RUNNING"; // exit FILETIME is undefined while running
                if(wait!=0)return "UNKNOWN";
                if(!GetProcessTimes(handle,out creation,out exit,out kernel,out user) || creation.Value!=expectedCreation || exit.Value<=0)return "UNKNOWN";
                return "TERMINATED";
            } finally {CloseHandle(handle);}
        }
        public static string CommandDigest(string executable,string[] arguments) {
            if(executable==null || arguments==null || arguments.Length>32) throw new ArgumentException("INVALID_ARGUMENT");
            // Length-prefix framing, explicit UTF-8 and domain. No raw argv is persisted.
            using(var memory=new MemoryStream()) {
                using(var writer=new BinaryWriter(memory,new UTF8Encoding(false,true),true)) {
                    writer.Write(new UTF8Encoding(false,true).GetBytes("cdpd.created-argv/v1\0"));
                    writer.Write(arguments.Length+1);
                    var all=new string[arguments.Length+1];all[0]=executable;Array.Copy(arguments,0,all,1,arguments.Length);
                    foreach(string value in all) {
                        Quote(value); // reject NUL/newline and null exactly as the launch entry does
                        byte[] bytes=new UTF8Encoding(false,true).GetBytes(value);
                        if(bytes.Length>131068) throw new ArgumentException("INVALID_ARGUMENT");
                        writer.Write(bytes.Length);writer.Write(bytes);
                    }
                    writer.Flush();
                }
                using(var sha=SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(memory.ToArray())).Replace("-","").ToLowerInvariant();
            }
        }
        // Exact Windows C-runtime argument quoting. No shell expansion is used.
        public static string Quote(string value) {
            if(value==null || value.IndexOf('\0')>=0 || value.IndexOf('\r')>=0 || value.IndexOf('\n')>=0) throw new ArgumentException("INVALID_ARGUMENT");
            var b=new StringBuilder("\"");int slashes=0;
            foreach(char c in value) {
                if(c=='\\') {slashes++;continue;}
                if(c=='\"') {b.Append('\\',slashes*2+1);b.Append(c);slashes=0;continue;}
                b.Append('\\',slashes);slashes=0;b.Append(c);
            }
            b.Append('\\',slashes*2);b.Append('"');return b.ToString();
        }
        public static LaunchedProcess Start(string executable,string[] arguments) {
            if(Environment.OSVersion.Platform!=PlatformID.Win32NT) throw new PlatformNotSupportedException();
            if(String.IsNullOrEmpty(executable) || !Path.IsPathRooted(executable) || !File.Exists(executable) || arguments==null || arguments.Length>32) throw new ArgumentException("INVALID_ARGUMENT");
            string digest=CommandDigest(executable,arguments);
            var command=new StringBuilder(Quote(executable));
            foreach(string argument in arguments) command.Append(' ').Append(Quote(argument));
            if(command.Length>=32767) throw new ArgumentException("INVALID_ARGUMENT");
            IntPtr attributes=IntPtr.Zero,list=IntPtr.Zero;bool initialized=false;
            var handles=new IntPtr[3];PI pi=new PI();bool transferred=false;
            try {
                var sa=new SA {Length=Marshal.SizeOf(typeof(SA)),Descriptor=IntPtr.Zero,Inherit=1};
                for(int i=0;i<3;i++) {
                    handles[i]=CreateFileW(@"\\.\NUL",i==0 ? 0x80000000u : 0x40000000u,3,ref sa,3,0x80,IntPtr.Zero);
                    if(handles[i]==new IntPtr(-1) || handles[i]==IntPtr.Zero) throw new InvalidOperationException("NUL_HANDLE_UNAVAILABLE");
                }
                UIntPtr size=UIntPtr.Zero;
                InitializeProcThreadAttributeList(IntPtr.Zero,1,0,ref size);
                if(size.ToUInt64()==0 || size.ToUInt64()>65536) throw new InvalidOperationException("HANDLE_LIST_UNAVAILABLE");
                attributes=Marshal.AllocHGlobal(checked((int)size.ToUInt64()));
                if(!InitializeProcThreadAttributeList(attributes,1,0,ref size)) throw new InvalidOperationException("HANDLE_LIST_UNAVAILABLE");
                initialized=true;list=Marshal.AllocHGlobal(3*IntPtr.Size);
                for(int i=0;i<3;i++) Marshal.WriteIntPtr(list,i*IntPtr.Size,handles[i]);
                // PROC_THREAD_ATTRIBUTE_HANDLE_LIST: inherit ONLY the three NUL handles.
                if(!UpdateProcThreadAttribute(attributes,0,new IntPtr(0x00020002),list,new UIntPtr((uint)(3*IntPtr.Size)),IntPtr.Zero,IntPtr.Zero)) throw new InvalidOperationException("HANDLE_LIST_UNAVAILABLE");
                var info=new SIX();info.Startup.Size=Marshal.SizeOf(typeof(SIX));info.Startup.Flags=0x100;
                info.Startup.Input=handles[0];info.Startup.Output=handles[1];info.Startup.Error=handles[2];info.Attributes=attributes;
                // No console window for console fixtures; GUI Chrome remains visible.
                // A separate process group avoids propagating console Ctrl+C into Chrome.
                if(!CreateProcessW(executable,command,IntPtr.Zero,IntPtr.Zero,true,0x00080000u|0x00000200u|0x08000000u,IntPtr.Zero,Path.GetDirectoryName(executable),ref info,out pi)) throw new InvalidOperationException("CREATE_PROCESS_FAILED");
                // Keep the real process handle; no re-open by a potentially recycled PID.
                FT creation,exit,kernel,user;
                if(!GetProcessTimes(pi.Process,out creation,out exit,out kernel,out user)) throw new InvalidOperationException("CREATED_PROCESS_TIME_UNAVAILABLE");
                var result=new LaunchedProcess(pi.Process,checked((int)pi.Pid),creation.Value);result.SetArgumentEvidence(digest);transferred=true;return result;
            } finally {
                if(pi.Thread!=IntPtr.Zero) CloseHandle(pi.Thread);
                if(!transferred && pi.Process!=IntPtr.Zero) CloseHandle(pi.Process); // never terminate Chrome
                if(initialized) DeleteProcThreadAttributeList(attributes);
                if(attributes!=IntPtr.Zero) Marshal.FreeHGlobal(attributes);
                if(list!=IntPtr.Zero) Marshal.FreeHGlobal(list);
                foreach(IntPtr h in handles) if(h!=IntPtr.Zero && h!=new IntPtr(-1)) CloseHandle(h);
            }
        }
    }
}

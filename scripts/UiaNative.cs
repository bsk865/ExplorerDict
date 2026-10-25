using System;
using System.Runtime.InteropServices;

namespace ExplorerDict {
    public static class Native {
        [DllImport("user32.dll")]
        public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")]
        public static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)]
        public static extern int GetClassNameW(IntPtr hwnd, System.Text.StringBuilder name, int size);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
        public static extern uint GetLongPathNameW(string path, System.Text.StringBuilder buffer, uint length);
        [DllImport("kernel32.dll", SetLastError=true)]
        public static extern IntPtr OpenProcess(uint access, bool inherit, uint processId);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        public static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, System.Text.StringBuilder path, ref uint length);
        [DllImport("kernel32.dll")]
        public static extern bool CloseHandle(IntPtr handle);
        public static string GetProcessPath(uint processId) {
            IntPtr process = OpenProcess(0x1000, false, processId);
            if (process == IntPtr.Zero) return "";
            try {
                var path = new System.Text.StringBuilder(32768);
                uint length = (uint)path.Capacity;
                return QueryFullProcessImageNameW(process, 0, path, ref length) ? path.ToString() : "";
            } finally { CloseHandle(process); }
        }
        [DllImport("user32.dll")]
        public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
        [DllImport("oleacc.dll")]
        public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId,
            ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object value);
    }
}

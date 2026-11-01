// Win32 display helpers for the virtual "Extended Screen".
// Loaded by vdisplay.ps1 via Add-Type (Windows PowerShell 5.1 / C# 5 compatible).
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class ExtDisplay
{
    // ---------------------------------------------------------------- GDI ---

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DISPLAY_DEVICE
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DEVMODE
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion;
        public short dmDriverVersion;
        public short dmSize;
        public short dmDriverExtra;
        public int dmFields;
        public int dmPositionX;
        public int dmPositionY;
        public int dmDisplayOrientation;
        public int dmDisplayFixedOutput;
        public short dmColor;
        public short dmDuplex;
        public short dmYResolution;
        public short dmTTOption;
        public short dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel;
        public int dmPelsWidth;
        public int dmPelsHeight;
        public int dmDisplayFlags;
        public int dmDisplayFrequency;
        public int dmICMMethod;
        public int dmICMIntent;
        public int dmMediaType;
        public int dmDitherType;
        public int dmReserved1;
        public int dmReserved2;
        public int dmPanningWidth;
        public int dmPanningHeight;
    }

    const int DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1;
    const int DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4;
    const int ENUM_CURRENT_SETTINGS = -1;
    const int DM_POSITION = 0x20;
    const int DM_PELSWIDTH = 0x80000;
    const int DM_PELSHEIGHT = 0x100000;
    const int DM_DISPLAYFREQUENCY = 0x400000;
    const uint CDS_UPDATEREGISTRY = 0x1;
    const uint CDS_NORESET = 0x10000000;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplayDevices(string lpDevice, uint iDevNum, ref DISPLAY_DEVICE lpDisplayDevice, uint dwFlags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int ChangeDisplaySettingsEx(string lpszDeviceName, ref DEVMODE lpDevMode, IntPtr hwnd, uint dwflags, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "ChangeDisplaySettingsEx")]
    static extern int ApplyPendingDisplaySettings(IntPtr lpszDeviceName, IntPtr lpDevMode, IntPtr hwnd, uint dwflags, IntPtr lParam);

    [DllImport("user32.dll")]
    static extern bool SetProcessDpiAwarenessContext(IntPtr value);

    // Mode/position values must be physical pixels; the main monitor runs at 150 % scaling.
    public static bool MakeDpiAware()
    {
        return SetProcessDpiAwarenessContext(new IntPtr(-4)); // PER_MONITOR_AWARE_V2
    }

    static DEVMODE NewDevMode()
    {
        var dm = new DEVMODE();
        dm.dmDeviceName = new string('\0', 32);
        dm.dmFormName = new string('\0', 32);
        dm.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
        return dm;
    }

    public class Adapter
    {
        public string DeviceName;   // \\.\DISPLAYn
        public string DeviceString; // adapter/driver name
        public bool Attached;
        public bool Primary;
    }

    public static List<Adapter> ListAdapters()
    {
        var list = new List<Adapter>();
        for (uint i = 0; ; i++)
        {
            var dd = new DISPLAY_DEVICE();
            dd.cb = Marshal.SizeOf(dd);
            if (!EnumDisplayDevices(null, i, ref dd, 0)) break;
            list.Add(new Adapter
            {
                DeviceName = dd.DeviceName,
                DeviceString = dd.DeviceString,
                Attached = (dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP) != 0,
                Primary = (dd.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE) != 0
            });
        }
        return list;
    }

    public class Mode
    {
        public int Width, Height, Hz, X, Y;
        public override string ToString() { return Width + "x" + Height + "@" + Hz + " at (" + X + "," + Y + ")"; }
    }

    public static Mode CurrentMode(string deviceName)
    {
        var dm = NewDevMode();
        if (!EnumDisplaySettings(deviceName, ENUM_CURRENT_SETTINGS, ref dm)) return null;
        return new Mode { Width = dm.dmPelsWidth, Height = dm.dmPelsHeight, Hz = dm.dmDisplayFrequency, X = dm.dmPositionX, Y = dm.dmPositionY };
    }

    public static List<Mode> SupportedModes(string deviceName)
    {
        var list = new List<Mode>();
        for (int i = 0; ; i++)
        {
            var dm = NewDevMode();
            if (!EnumDisplaySettings(deviceName, i, ref dm)) break;
            list.Add(new Mode { Width = dm.dmPelsWidth, Height = dm.dmPelsHeight, Hz = dm.dmDisplayFrequency });
        }
        return list;
    }

    // Sets resolution, refresh rate and desktop position (which also attaches
    // the display to the desktop, i.e. "extend"). Returns the DISP_CHANGE code.
    public static int SetMode(string deviceName, int width, int height, int hz, int x, int y)
    {
        var dm = NewDevMode();
        dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY;
        dm.dmPelsWidth = width;
        dm.dmPelsHeight = height;
        dm.dmDisplayFrequency = hz;
        dm.dmPositionX = x;
        dm.dmPositionY = y;
        int rc = ChangeDisplaySettingsEx(deviceName, ref dm, IntPtr.Zero, CDS_UPDATEREGISTRY | CDS_NORESET, IntPtr.Zero);
        if (rc != 0) return rc;
        return ApplyPendingDisplaySettings(IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, 0, IntPtr.Zero);
    }

    // ------------------------------------------------- DisplayConfig (HDR) ---

    [StructLayout(LayoutKind.Sequential)]
    public struct LUID { public uint LowPart; public int HighPart; }

    [StructLayout(LayoutKind.Sequential)]
    public struct DISPLAYCONFIG_PATH_SOURCE_INFO
    {
        public LUID adapterId; public uint id; public uint modeInfoIdx; public uint statusFlags;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct DISPLAYCONFIG_PATH_TARGET_INFO
    {
        public LUID adapterId; public uint id; public uint modeInfoIdx;
        public uint outputTechnology; public uint rotation; public uint scaling;
        public uint refreshNumerator; public uint refreshDenominator;
        public uint scanLineOrdering; public int targetAvailable; public uint statusFlags;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct DISPLAYCONFIG_PATH_INFO
    {
        public DISPLAYCONFIG_PATH_SOURCE_INFO sourceInfo;
        public DISPLAYCONFIG_PATH_TARGET_INFO targetInfo;
        public uint flags;
    }

    // 64 bytes; the 48-byte union is opaque to us.
    [StructLayout(LayoutKind.Sequential)]
    public struct DISPLAYCONFIG_MODE_INFO
    {
        public uint infoType; public uint id; public LUID adapterId;
        public ulong u0, u1, u2, u3, u4, u5;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct DISPLAYCONFIG_DEVICE_INFO_HEADER
    {
        public uint type; public uint size; public LUID adapterId; public uint id;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DISPLAYCONFIG_SOURCE_DEVICE_NAME
    {
        public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string viewGdiDeviceName;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO
    {
        public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
        public uint value; public uint colorEncoding; public uint bitsPerColorChannel;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE
    {
        public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
        public uint value;
    }

    const uint QDC_ONLY_ACTIVE_PATHS = 0x2;
    const uint GET_SOURCE_NAME = 1;
    const uint GET_ADVANCED_COLOR_INFO = 9;
    const uint SET_ADVANCED_COLOR_STATE = 10;

    [DllImport("user32.dll")]
    static extern int GetDisplayConfigBufferSizes(uint flags, out uint numPaths, out uint numModes);

    [DllImport("user32.dll")]
    static extern int QueryDisplayConfig(uint flags, ref uint numPaths, [Out] DISPLAYCONFIG_PATH_INFO[] paths,
                                         ref uint numModes, [Out] DISPLAYCONFIG_MODE_INFO[] modes, IntPtr topologyId);

    [DllImport("user32.dll")]
    static extern int DisplayConfigGetDeviceInfo(ref DISPLAYCONFIG_SOURCE_DEVICE_NAME info);

    [DllImport("user32.dll")]
    static extern int DisplayConfigGetDeviceInfo(ref DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO info);

    [DllImport("user32.dll")]
    static extern int DisplayConfigSetDeviceInfo(ref DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE info);

    static bool FindTarget(string gdiName, out DISPLAYCONFIG_PATH_TARGET_INFO target)
    {
        target = new DISPLAYCONFIG_PATH_TARGET_INFO();
        uint np, nm;
        if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, out np, out nm) != 0) return false;
        var paths = new DISPLAYCONFIG_PATH_INFO[np];
        var modes = new DISPLAYCONFIG_MODE_INFO[nm];
        if (QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ref np, paths, ref nm, modes, IntPtr.Zero) != 0) return false;
        for (int i = 0; i < np; i++)
        {
            var src = new DISPLAYCONFIG_SOURCE_DEVICE_NAME();
            src.header.type = GET_SOURCE_NAME;
            src.header.size = (uint)Marshal.SizeOf(typeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME));
            src.header.adapterId = paths[i].sourceInfo.adapterId;
            src.header.id = paths[i].sourceInfo.id;
            if (DisplayConfigGetDeviceInfo(ref src) != 0) continue;
            if (string.Equals(src.viewGdiDeviceName, gdiName, StringComparison.OrdinalIgnoreCase))
            {
                target = paths[i].targetInfo;
                return true;
            }
        }
        return false;
    }

    // Returns: -1 unknown, 0 SDR, 1 HDR on. "supported" tells whether HDR is possible at all.
    public static int HdrState(string gdiName, out bool supported)
    {
        supported = false;
        DISPLAYCONFIG_PATH_TARGET_INFO t;
        if (!FindTarget(gdiName, out t)) return -1;
        var info = new DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO();
        info.header.type = GET_ADVANCED_COLOR_INFO;
        info.header.size = (uint)Marshal.SizeOf(typeof(DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO));
        info.header.adapterId = t.adapterId;
        info.header.id = t.id;
        if (DisplayConfigGetDeviceInfo(ref info) != 0) return -1;
        supported = (info.value & 0x1) != 0;
        return (info.value & 0x2) != 0 ? 1 : 0;
    }

    public static int SetHdr(string gdiName, bool enable)
    {
        DISPLAYCONFIG_PATH_TARGET_INFO t;
        if (!FindTarget(gdiName, out t)) return -1;
        var s = new DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE();
        s.header.type = SET_ADVANCED_COLOR_STATE;
        s.header.size = (uint)Marshal.SizeOf(typeof(DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE));
        s.header.adapterId = t.adapterId;
        s.header.id = t.id;
        s.value = enable ? 1u : 0u;
        return DisplayConfigSetDeviceInfo(ref s);
    }
}

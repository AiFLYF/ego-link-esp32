r"""列出本机串口，并区分「真实 USB 串口」与「蓝牙等虚拟串口」。

用途：烧录前确认开发板到底是哪个 COM 口。ESP32-S3 开发板通过板载
USB-UART 桥（CH340 / CP210x / FTDI）或原生 USB-CDC 出现，其硬件 ID
以 `USB\` 开头；「蓝牙链接上的标准串行」这类虚拟口不是我们要的。

用法：
    python tools/list_serial_ports.py            # 人类可读
    python tools/list_serial_ports.py --first    # 只打印第一个真实 USB 串口

--------------------------------------------------------------------------
实现笔记（三个真踩过的坑，别改回去）：

1) **不要用 QueryDosDeviceW**。旧版用它列端口名，在 WorkBuddy 沙箱里
   会让进程**静默退出、退出码 127、零输出**（不是异常，抓不到）。
   改用 SetupAPI 枚举「端口(COM 和 LPT)」设备类，沙箱下正常。

2) **ctypes 必须显式声明 SetupAPI 的 restype/argtypes**。
   `SetupDiGetClassDevsW` 返回的 HDEVINFO 是 64 位指针，ctypes 默认按
   `c_int` 处理会**截断句柄**，于是 `SetupDiEnumDeviceInfo` 一路失败、
   枚举出 0 个设备——看起来像「没有串口」，实际是句柄坏了。

3) **`SPDRP_HARDWAREID` 是 0x01，不是 0x08**（0x08 是 SPDRP_CLASSGUID）。
   写错会拿到 `{4d36e978-...}` 这种类 GUID，永远判不出 USB 串口。

另外本机没装 pyserial（`pip install pyserial` 会污染用户环境），
上面这些 API 都是 Windows 自带的，够用。
"""

import ctypes
import sys
from ctypes import wintypes

setupapi = ctypes.windll.setupapi

DIGCF_PRESENT = 0x02

# ---- SetupDiGetDeviceRegistryProperty 的属性号（务必核对）------------------
SPDRP_DEVICEDESC = 0x00
SPDRP_HARDWAREID = 0x01
SPDRP_CLASSGUID = 0x08
SPDRP_MFG = 0x0B
SPDRP_FRIENDLYNAME = 0x0C
SPDRP_LOCATION_INFORMATION = 0x0D

# 「端口(COM 和 LPT)」设备类 GUID
PORTS_CLASS_GUID = "{4d36e978-e325-11ce-bfc1-08002be10318}"

# 常见 USB-UART 桥的 VID → 名称（硬件 ID 里匹配，用来给人看）
KNOWN_USB_VIDS = {
    "VID_1A86": "CH34x (沁恒)",
    "VID_10C4": "CP210x (Silicon Labs)",
    "VID_0403": "FTDI",
    "VID_067B": "PL2303 (Prolific)",
    "VID_303A": "Espressif 原生 USB-CDC",
    "VID_2341": "Arduino",
    "VID_1B4F": "SparkFun",
}


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, s):
        s = s.strip("{}")
        d1, d2, d3, d4a, d4b = s.split("-")
        return cls(
            int(d1, 16), int(d2, 16), int(d3, 16),
            (ctypes.c_ubyte * 8)(int(d4a[0:2], 16), int(d4a[2:4], 16),
                                 *bytes.fromhex(d4b)),
        )


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("ClassGuid", GUID),
        ("DevInst", ctypes.c_ulong),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


# ---- 显式声明签名（坑 2）---------------------------------------------------
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInfo.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]


def _prop(h, info, prop):
    """读一个设备注册表属性；读不到返回空串（不抛异常）。"""
    buf = ctypes.create_unicode_buffer(1024)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        h, ctypes.byref(info), prop, None, buf, ctypes.sizeof(buf), None)
    return buf.value if ok else ""


def list_ports():
    """返回 [{'port','name','hwid','mfg','is_usb'}]，按 COM 号排序。"""
    ports = []
    guid = GUID.from_string(PORTS_CLASS_GUID)
    h = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT)
    if not h or h == wintypes.HANDLE(-1).value:
        return ports

    try:
        info = SP_DEVINFO_DATA()
        info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
        i = 0
        while setupapi.SetupDiEnumDeviceInfo(h, i, ctypes.byref(info)):
            i += 1
            name = _prop(h, info, SPDRP_FRIENDLYNAME)
            if "(COM" not in name:
                continue
            hwid = _prop(h, info, SPDRP_HARDWAREID)
            mfg = _prop(h, info, SPDRP_MFG)
            port = name[name.rindex("(COM"):].strip("()")
            ports.append({
                "port": port,
                "name": name,
                "hwid": hwid,
                "mfg": mfg,
                # 蓝牙虚拟串口走 BTHENUM，真串口走 USB\VID_xxxx&PID_xxxx
                "is_usb": hwid.upper().startswith("USB\\"),
            })
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(h)

    def key(p):
        digits = "".join(c for c in p["port"] if c.isdigit())
        return int(digits) if digits else 9999

    ports.sort(key=key)
    return ports


def describe_vid(hwid):
    up = hwid.upper()
    for vid, label in KNOWN_USB_VIDS.items():
        if vid in up:
            return label
    return ""


def main():
    want_first = "--first" in sys.argv
    ports = list_ports()
    usb = [p for p in ports if p["is_usb"]]

    if want_first:
        print(usb[0]["port"] if usb else "")
        return 0 if usb else 1

    if not ports:
        print("未发现任何串口（连蓝牙虚拟口都没有）。")
        print("请确认开发板已用 USB 数据线接到电脑"
              "（要能供电+通信的线，不是纯充电线）。")
        return 1

    print(f"共发现 {len(ports)} 个串口，其中确认为真实 USB 串口 {len(usb)} 个：\n")
    for p in ports:
        tag = "USB " if p["is_usb"] else "虚拟"
        chip = describe_vid(p["hwid"])
        print(f"  [{tag}] {p['port']:<6} {p['name']}")
        if p["hwid"]:
            print(f"          HWID: {p['hwid']}")
        if chip:
            print(f"          芯片: {chip}")

    if not usb:
        print("\n⚠️ 没有真实 USB 串口 —— 列出的全是蓝牙等虚拟口。")
        print("   结论：开发板没插上，或插的是纯充电线，或缺少 USB-UART 驱动")
        print("   （CH340 / CP210x / FTDI，插上后设备管理器里应出现新的 COM 口）。")
        return 1

    print(f"\n✅ 开发板最可能是：{usb[0]['port']}")
    print(f"   烧录命令：flash_device.bat {usb[0]['port']} 30")
    return 0


if __name__ == "__main__":
    sys.exit(main())

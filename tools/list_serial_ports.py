import ctypes

buf = ctypes.create_unicode_buffer(32768)
n = ctypes.windll.kernel32.QueryDosDeviceW(None, buf, ctypes.sizeof(buf))
raw = buf.raw[: n * 2]
names = [p for p in raw.split("\x00\x00") if p]
coms = sorted(
    [p for p in names if p.startswith("COM")],
    key=lambda x: int(x[3:]) if x[3:].isdigit() else 999,
)
print("Serial ports:", coms if coms else "NONE")

# 额外用 SetupAPI 风格的设备描述（友好名）不可用时至少给端口名
for c in coms:
    print(" -", c)

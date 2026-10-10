#!/usr/bin/env python3
"""从串口收板子推来的摄像头帧 —— 绕开 WiFi 的调试/传输通道。

## 为什么需要它

板子的实时画面走 WiFi：板子 POST 一帧 27KB 到服务端，网页再取。实测只有
**1~2 帧/秒**，而且**分不清瓶颈在摄像头还是在网络**（服务端没起来、板子没配网时
根本测不了）。

USB 口是 **USB-Serial-JTAG**（`CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y`），
带宽远高于 UART，所以可以拿它当一条**独立于 WiFi 的传输/调试通道**：
既能直接量"摄像头到底能出多快"，也能在没有网络时看到真实画面。

## 帧格式（板端 `camera_dump_serial()` 按这个写）

    @@RW1F <seq> <len> <base64>@@      ← 一帧（单行）
    @@RW1R <n> <ms> <fps>@@            ← 本轮汇总

用 base64 而不是裸二进制：**控制台日志和帧数据共用同一个口**，
裸二进制被日志插一行就整帧错位；base64 最多坏一帧，靠下面的 JPEG 头尾校验能识别并跳过。

## 用法

    python tools/serial_cam_rx.py --port COM10 --out .workbuddy-ai/tmp/serial_frames
"""

import argparse
import base64
import os
import re
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("需要 pyserial（用 IDF 的 python 环境跑，或 pip install pyserial）")

# 帧行：@@RW1F <seq> <len> <base64>@@
RE_FRAME = re.compile(rb"@@RW1F (\d+) (\d+) ([A-Za-z0-9+/=]+)@@")
# 汇总行：@@RW1R <n> <ms> <fps>@@
RE_REPORT = re.compile(rb"@@RW1R (\d+) (\d+) ([0-9.]+)@@")


def jpeg_ok(b: bytes) -> bool:
    """头 FF D8、尾 FF D9 —— 和板端自检用的是同一套判据。"""
    return (len(b) >= 4 and b[0] == 0xFF and b[1] == 0xD8
            and b[-2] == 0xFF and b[-1] == 0xD9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="板子串口，如 COM10")
    ap.add_argument("--baud", type=int, default=921600,
                    help="USB-Serial-JTAG 忽略波特率，留着只是为了兼容 UART 桥")
    ap.add_argument("--out", default=".workbuddy-ai/tmp/serial_frames")
    ap.add_argument("--seconds", type=float, default=60.0, help="收多久后退出")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    ser = serial.Serial(args.port, args.baud, timeout=1.0)
    print("监听 %s，输出到 %s（%.0f 秒后退出）" % (args.port, args.out, args.seconds))

    t0 = time.time()
    got = bad = 0
    first_t = None
    last_t = None
    buf = b""

    try:
        while time.time() - t0 < args.seconds:
            chunk = ser.read(65536)
            if not chunk:
                continue
            buf += chunk

            # 汇总行可能夹在帧之间，先扫它
            for m in RE_REPORT.finditer(buf):
                n, ms, fps = m.group(1).decode(), m.group(2).decode(), m.group(3).decode()
                print("\n[板端汇总] %s 帧 / %s ms  →  **%s 帧/秒**" % (n, ms, fps))
            buf = RE_REPORT.sub(b"", buf)

            # 逐帧解析；解析完的行从 buf 里删掉，剩下的留到下一轮（可能被截断）
            last_end = 0
            for m in RE_FRAME.finditer(buf):
                seq, length, b64 = m.group(1), int(m.group(2)), m.group(3)
                last_end = m.end()
                try:
                    data = base64.b64decode(b64, validate=True)
                except Exception:
                    bad += 1
                    continue
                if len(data) != length or not jpeg_ok(data):
                    # 日志插进来会坏掉这一帧 —— 正常现象，跳过并计数
                    bad += 1
                    continue
                now = time.time()
                if first_t is None:
                    first_t = now
                last_t = now
                got += 1
                path = os.path.join(args.out, "s%03d.jpg" % got)
                with open(path, "wb") as f:
                    f.write(data)
                print("  第 %s 帧 → %s（%d 字节）" % (seq.decode(), os.path.basename(path), len(data)))

            if last_end:
                buf = buf[last_end:]
            # buf 别无限涨：没解析出东西时只留尾巴
            if len(buf) > 262144:
                buf = buf[-65536:]

            if got and got % 10 == 0:
                span = last_t - first_t
                if span > 0:
                    print("  … 已收 %d 帧，间隔平均 %.1f 帧/秒" % (got, (got - 1) / span))
    finally:
        ser.close()

    print("\n=== 收工 ===")
    print("  成功 %d 帧，跳过 %d 帧（多半是控制台日志插进来导致的，正常）" % (got, bad))
    if got >= 2 and last_t > first_t:
        print("  **实测帧率 %.2f 帧/秒**（按帧间隔算）" % ((got - 1) / (last_t - first_t)))
    print("  文件在 %s" % args.out)


if __name__ == "__main__":
    main()

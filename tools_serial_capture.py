#!/usr/bin/env python
"""Bounded serial capture for autonomous iteration.

Opens the given COM port, pulses a reset-to-run on the ESP32 (RTS=EN), then
prints every line received for N seconds and exits. Unlike `idf.py monitor`
this terminates, so it can be used non-interactively to sample telemetry.

Usage: python tools_serial_capture.py COM10 30
"""
import sys
import time

try:
    import serial
except ImportError:
    print("ERR: pyserial not available in this python", flush=True)
    sys.exit(2)

port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

try:
    ser = serial.Serial(port, 115200, timeout=0.2)
except Exception as e:  # noqa: BLE001
    print(f"ERR: cannot open {port}: {e}", flush=True)
    sys.exit(3)

# Reset-to-run: GPIO0 high (DTR False) so it boots from flash, pulse EN (RTS).
ser.setDTR(False)
ser.setRTS(True)
time.sleep(0.12)
ser.setRTS(False)

t0 = time.time()
n = 0
while time.time() - t0 < dur:
    raw = ser.readline()
    if not raw:
        continue
    line = raw.decode("utf-8", "replace").rstrip("\r\n")
    if line:
        print(line, flush=True)
        n += 1
ser.close()
print(f"--- capture done: {n} lines in {dur:.0f}s ---", flush=True)

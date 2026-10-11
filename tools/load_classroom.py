#!/usr/bin/env python
"""课堂规模验证：一个班 N 块板子连同一个服务器。

**为什么要单独一个脚本，不塞进 verify_server**：这个项目的第一需求就是
「一个班 20 块板子连同一个服务器」（README「多设备」一节），但 `verify_server.py`
里的多设备那一节只起了 **2 块**。课堂规模**从来没被验证过**。
而它要跑几十秒、并发几十路，放进提交门禁会拖慢每一次回归 ——
所以做成一个能随时手动跑、也会在需要时被点名的工具。

它回答三个问题：

1. **N 块板子能不能同时上报**、全部在册、不掉线？
2. 服务端在 N 路并发下的**延迟**是多少（p50 / p95 / 最慢）？
3. 广播一条指令给全部 N 块，是不是**每块都排到了队**？

用法：

    python tools/load_classroom.py                 # 默认 20 块
    python tools/load_classroom.py --boards 32     # 顶到多设备上限
    python tools/load_classroom.py --seconds 30    # 跑久一点

退出码 0 = 课堂规模达标；非 0 = 有硬指标没过（会打印是哪一条）。

--------------------------------------------------------------------------
实现笔记：

- 假板子是**测试进程里的线程**，不是 N 个 `fake_board.py` 子进程。
  这个沙箱里空 python 启动就要 2~3 秒（手册 T63），20 个进程光起就得一分多钟；
  而且这里要验的是**服务端**在 N 路并发下的表现，不是子进程启动。
  报文体仍用 `verify_server.firmware_body()` 生成 —— 和真板子逐字节一致。
- 所有请求走 `verify_server.OPENER`（绕开代理），服务端子进程用 `CHILD_ENV`。
  这两件事是手册 T63 的教训：沙箱注入的代理会把 127.0.0.1 的请求也劫走。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verify_server as vs          # noqa: E402  —— 复用 firmware_body / CHILD_ENV / OPENER

# 多设备上限（server.py 里的 --max-devices 默认值）。超过就只会有前 N 台进队列，
# 那不算服务端的错，但会让"每块都排到队"这条断言假红 —— 所以提前拦下来。
MAX_DEVICES = 32


class Board(threading.Thread):
    """一块假板子：按 period 往服务器灌遥测，记下每次往返的耗时。"""

    def __init__(self, base, dev_id, period, stop_evt, results):
        super().__init__(daemon=True)
        self.base = base
        self.dev_id = dev_id
        self.period = period
        self.stop_evt = stop_evt
        self.results = results          # 共享：{"ok": n, "fail": n, "lat": [...], "err": {...}}
        self.lock = results["lock"]

    def run(self):
        url = self.base + "/api/telemetry"
        while not self.stop_evt.is_set():
            body = vs.firmware_body([(0.0, 0.0, 1.0)] * 50, device=self.dev_id)
            t0 = time.time()
            try:
                vs.post_raw(url, body, timeout=10)
                dt = time.time() - t0
                with self.lock:
                    self.results["ok"] += 1
                    self.results["lat"].append(dt)
            except Exception as exc:                      # noqa: BLE001
                with self.lock:
                    self.results["fail"] += 1
                    key = type(exc).__name__
                    self.results["err"][key] = self.results["err"].get(key, 0) + 1
            # 睡到下一个周期（减去这次花掉的时间），别让慢请求把节奏拖垮
            rest = self.period - (time.time() - t0)
            if rest > 0:
                self.stop_evt.wait(rest)


def pct(vals, p):
    """百分位（最近秩法）。vals 必须已排序。"""
    if not vals:
        return 0.0
    k = min(len(vals) - 1, max(0, int(round((p / 100.0) * len(vals) + 0.5)) - 1))
    return vals[k]


class SseWatcher(threading.Thread):
    """开一条 SSE 长连接 —— 就是仪表盘走的那条路。

    真实课堂的用法是「N 块板在发 + 老师开着仪表盘看」，**光测 POST 并发是不够的**：
    SSE 每个连接一个线程、每 500ms 推一次**全量快照**（含全部设备列表），
    而且它和遥测抢**同一把 LOCK**（见 server.py 的 `_sse`）。
    这条断言就是看：板子在狂发的时候，仪表盘那条路还活不活、还能不能看见全部板子。
    """

    def __init__(self, base, stop_evt, out):
        super().__init__(daemon=True)
        self.base = base
        self.stop_evt = stop_evt
        self.out = out

    def run(self):
        r = None
        try:
            r = vs.OPENER.open(self.base + "/api/stream", timeout=10)
            while not self.stop_evt.is_set():
                line = r.readline()
                if not line:
                    break
                if not line.startswith(b"data: "):
                    continue
                try:
                    snap = json.loads(line[6:].decode("utf-8"))
                except Exception:                              # noqa: BLE001
                    self.out["bad"] += 1
                    continue
                self.out["frames"] += 1
                self.out["devices"] = len(snap.get("devices") or [])
        except Exception as exc:                               # noqa: BLE001
            self.out["err"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:                              # noqa: BLE001
                    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", type=int, default=20, help="板子数量（默认 20 = 一个班）")
    ap.add_argument("--seconds", type=float, default=15.0, help="持续上报多久")
    ap.add_argument("--period", type=float, default=0.5,
                    help="每块板子的上报周期（秒），默认 0.5 = 板端默认 500ms")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    n = args.boards
    if n > MAX_DEVICES:
        print("!! --boards %d 超过服务端多设备上限 %d，只会有前 %d 台进队列。"
              % (n, MAX_DEVICES, MAX_DEVICES))
        print("   （要跑更多请先调大 server.py 的 --max-devices；这里直接退出，免得给出假结论。）")
        return 2

    port = vs.free_port()
    base = "http://127.0.0.1:%d" % port
    tmp = tempfile.mkdtemp(prefix="rw1-classroom-")
    data_dir = os.path.join(tmp, "data")

    print("=" * 70)
    print("课堂规模验证   板子=%d  周期=%.2fs  时长=%.0fs  端口=%d" % (n, args.period, args.seconds, port))
    print("=" * 70)

    srv = subprocess.Popen([vs.PY, "-u", vs.SERVER_PY, "--port", str(port),
                            "--data-dir", data_dir, "--retain-days", "0"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, env=vs.CHILD_ENV, cwd=vs.ROOT)
    problems = []
    try:
        # 等就绪：服务端启动本身也要 3 秒左右（T63），给足余量
        ready = False
        for _ in range(120):
            try:
                vs.OPENER.open(base + "/api/latest", timeout=2).read()
                ready = True
                break
            except Exception:                              # noqa: BLE001
                time.sleep(0.25)
        if not ready:
            print("!! 服务端起不来，放弃")
            return 2

        # ---- 并发上报 -----------------------------------------------------
        results = {"ok": 0, "fail": 0, "lat": [], "err": {}, "lock": threading.Lock()}
        stop_evt = threading.Event()
        devs = ["class-%02d" % i for i in range(1, n + 1)]
        boards = [Board(base, d, args.period, stop_evt, results) for d in devs]
        t_start = time.time()
        for b in boards:
            b.start()

        # 同时开一条 SSE（仪表盘那条路）—— 板子狂发的时候它得活着
        sse = {"frames": 0, "bad": 0, "devices": 0, "err": ""}
        sse_thread = SseWatcher(base, stop_evt, sse)
        sse_thread.start()

        # 同时给**仪表盘那条路**打点：板子在狂发的时候，网页还能不能正常取数？
        dash_lat = []
        while time.time() - t_start < args.seconds:
            t0 = time.time()
            try:
                vs.OPENER.open(base + "/api/devices", timeout=5).read()
                dash_lat.append(time.time() - t0)
            except Exception:                              # noqa: BLE001
                dash_lat.append(float("inf"))
            time.sleep(0.5)
        stop_evt.set()
        for b in boards:
            b.join(timeout=5)
        elapsed = time.time() - t_start

        with results["lock"]:
            ok, fail = results["ok"], results["fail"]
            lat = sorted(results["lat"])
            err = dict(results["err"])
        total = ok + fail
        expected = int(n * elapsed / args.period)

        print("\n[1] 并发上报")
        print("     成功 %d / 失败 %d（共 %d 次，理论约 %d 次）" % (ok, fail, total, expected))
        print("     实测 %.1f 次/秒（%d 块板 × 1/%.2fs）" % (total / elapsed, n, args.period))
        if lat:
            print("     往返延迟 p50=%.1f ms  p95=%.1f ms  最慢=%.1f ms"
                  % (pct(lat, 50) * 1000, pct(lat, 95) * 1000, lat[-1] * 1000))
        if err:
            print("     失败原因: %s" % err)
        print("     仪表盘 /api/devices 延迟 p50=%.1f ms 最慢=%.1f ms"
              % (pct(sorted(x for x in dash_lat if x != float("inf")), 50) * 1000,
                 max(dash_lat) * 1000))

        # ---- 在册与在线 ---------------------------------------------------
        live = vs.get_json(base + "/api/devices").get("devices", [])
        ids = {d["id"] for d in live}
        missing = [d for d in devs if d not in ids]
        offline = [d["id"] for d in live if not d["online"]]
        print("\n[2] 在册与在线")
        print("     %d/%d 块在册%s" % (len(devs) - len(missing), n,
                                      ("，缺: %s" % missing[:5]) if missing else ""))
        print("     离线: %s" % (offline or "无"))

        # ---- 广播指令 -----------------------------------------------------
        rb = vs.post_json(base + "/api/command",
                          {"name": "led_blink", "params": {"n": 2}, "devices": devs},
                          timeout=20)
        got = rb.get("count")
        queued = 0
        for d in devs:
            cmds = vs.get_json(base + "/api/commands?device=" + urllib.parse.quote(d)).get("commands", [])
            if any(c["name"] == "led_blink" for c in cmds):
                queued += 1
        print("\n[3] 广播指令给全部 %d 块" % n)
        print("     服务端返回 count=%s（batch=%s）" % (got, rb.get("batch")))
        print("     真正排进各自队列的: %d/%d" % (queued, n))

        # ---- 仪表盘 SSE ---------------------------------------------------
        sse_thread.join(timeout=6)
        sse_expected = int(elapsed / 0.5)          # 服务端每 500ms 推一帧
        print("\n[4] 仪表盘 SSE（板子狂发时它还活着吗）")
        if sse["err"]:
            print("     连接出错: %s" % sse["err"])
        else:
            print("     收到 %d 帧（理论约 %d），坏帧 %d" % (sse["frames"], sse_expected, sse["bad"]))
            print("     最后一帧里能看到 %d/%d 块板子" % (sse["devices"], n))

        # ---- 判定 ---------------------------------------------------------
        print("\n" + "=" * 70)
        if fail:
            problems.append("有 %d 次上报失败（%s）" % (fail, err))
        if missing:
            problems.append("有 %d 块板子没在册: %s" % (len(missing), missing[:5]))
        if offline:
            problems.append("有 %d 块板子显示离线: %s" % (len(offline), offline[:5]))
        if lat and pct(lat, 95) > 1.0:
            problems.append("上报往返 p95 = %.0f ms，超过 1 秒" % (pct(lat, 95) * 1000))
        if got != n:
            problems.append("广播返回 count=%s，期望 %d" % (got, n))
        if queued != n:
            problems.append("只有 %d/%d 块真的排到了队" % (queued, n))
        if max(dash_lat) == float("inf"):
            problems.append("板子狂发时 /api/devices 有取不到的时候")
        if sse["err"]:
            problems.append("SSE 长连接出错: %s" % sse["err"])
        elif sse["frames"] < sse_expected * 0.6:
            problems.append("SSE 只收到 %d 帧（理论约 %d）—— 板子狂发时仪表盘被饿死了"
                            % (sse["frames"], sse_expected))
        elif sse["devices"] != n:
            problems.append("SSE 最后一帧只看到 %d/%d 块板子" % (sse["devices"], n))

        if problems:
            print("课堂规模: **不达标**")
            for p in problems:
                print("  ✗ %s" % p)
        else:
            print("课堂规模: **达标**（%d 块板子同时上报，全部在册在线，广播人人有份）" % n)
        print("=" * 70)
        return 1 if problems else 0

    finally:
        srv.kill()
        try:
            srv.wait(timeout=5)
        except Exception:                                  # noqa: BLE001
            pass
        if args.keep:
            print("\n临时目录保留在: %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

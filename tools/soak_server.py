#!/usr/bin/env python
"""长跑稳定性：N 块板连续上报 M 分钟，看内存与延迟会不会随时间劣化。

**为什么单独一个工具**：课堂里这台服务器要连着开一整天（甚至一学期），
而现有门禁最长只跑 20 秒 —— 只能证明"扛得住"，证明不了"扛得住多久"。
这个仓库真的栽过一次"跑满 24 小时才炸"的事故（`purge_old` 被写成私有名，
看护线程抛 AttributeError 整个死掉，短期回归全测不到）。

它回答三个问题：

1. **内存会不会一直涨**？（每 30 秒采一次服务端进程的 RSS）
2. **延迟会不会随时间劣化**？（服务端自报 `ms` 的 p95，按时间分桶）
3. **有没有请求开始失败**？（错误计数）

用法：

    python tools/soak_server.py                      # 20 板 × 10 分钟
    python tools/soak_server.py --minutes 30
    python tools/soak_server.py --boards 32 --minutes 5

退出码 0 = 没看出劣化；非 0 = 有硬指标没过（会打印是哪一条）。

--------------------------------------------------------------------------
实现笔记：

- RSS 走 `tasklist /FI "PID eq N" /FO CSV /NH`，**必须在 Python 里调**：
  从 Git Bash 直接敲会被路径转换吃掉 `/FI`（变成 `D:/.../FI`）。
  输出是 OEM 代码页，用 `mbcs` 解码，别用 utf-8。
- 只在 Windows 上读 RSS；其它平台降级成"不采内存"，其余判据照跑。
- 判据刻意保守：短跑几分钟本来就看不出长期趋势，所以只在**明显**的
  信号上判失败（失败请求 / RSS 涨过阈值 / p95 翻倍）。否则会天天假红。
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verify_server as vs          # noqa: E402  —— 复用 firmware_body / CHILD_ENV / OPENER

MAX_DEVICES = 32
SAMPLE_S = 30.0          # 采样间隔
RSS_GROWTH_LIMIT_MB = 40  # 整轮涨这么多就认为可疑（Python 分配器会抖动，留足余量）


def rss_mb(pid):
    """读进程常驻内存（MB）。读不到返回 None（非 Windows / 进程已退出）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
            capture_output=True, timeout=10).stdout.decode("mbcs", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"([\d,]+)\s*K"', out)
    return int(m.group(1).replace(",", "")) / 1024.0 if m else None


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[k]


class Poster(threading.Thread):
    """一块假板子：按周期上报，把服务端自报的 ms 记进共享列表。"""

    def __init__(self, base, dev_id, period, stop_evt, acc, lock):
        super().__init__(daemon=True)
        self.base, self.dev_id, self.period = base, dev_id, period
        self.stop_evt, self.acc, self.lock = stop_evt, acc, lock
        self.body = vs.firmware_body([(0.0, 0.0, 1.0)] * 50, device=dev_id)

    def run(self):
        url = self.base + "/api/telemetry"
        while not self.stop_evt.is_set():
            t0 = time.time()
            try:
                r = vs.post_raw(url, self.body, timeout=10)
                ms = r.get("ms") if isinstance(r, dict) else None
                with self.lock:
                    self.acc["ok"] += 1
                    if isinstance(ms, int):
                        self.acc["ms"].append((time.time(), ms))
            except Exception as exc:                       # noqa: BLE001
                with self.lock:
                    self.acc["fail"] += 1
                    key = type(exc).__name__
                    self.acc["err"][key] = self.acc["err"].get(key, 0) + 1
            rest = self.period - (time.time() - t0)
            if rest > 0:
                self.stop_evt.wait(rest)


def main():
    ap = argparse.ArgumentParser(description="长跑稳定性：内存与延迟会不会随时间劣化")
    ap.add_argument("--boards", type=int, default=20)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--period", type=float, default=0.5)
    ap.add_argument("--sample", type=float, default=SAMPLE_S)
    args = ap.parse_args()

    if args.boards > MAX_DEVICES:
        print("!! --boards %d 超过服务端上限 %d" % (args.boards, MAX_DEVICES))
        return 2

    port = vs.free_port()
    base = "http://127.0.0.1:%d" % port
    tmp = tempfile.mkdtemp(prefix="rw1-soak-")
    print("=" * 70)
    print("长跑稳定性  板子=%d  周期=%.2fs  时长=%.1f 分钟  端口=%d"
          % (args.boards, args.period, args.minutes, port))
    print("=" * 70)

    srv = subprocess.Popen([vs.PY, "-u", vs.SERVER_PY, "--port", str(port),
                            "--data-dir", os.path.join(tmp, "data"),
                            "--retain-days", "0"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=vs.CHILD_ENV, cwd=vs.ROOT)
    try:
        for _ in range(120):
            try:
                vs.OPENER.open(base + "/api/devices", timeout=2).read()
                break
            except Exception:                              # noqa: BLE001
                time.sleep(0.25)
        else:
            print("!! 服务端起不来")
            return 2

        acc = {"ok": 0, "fail": 0, "ms": [], "err": {}}
        lock = threading.Lock()
        stop_evt = threading.Event()
        boards = [Poster(base, "soak-%02d" % i, args.period, stop_evt, acc, lock)
                  for i in range(1, args.boards + 1)]
        for b in boards:
            b.start()

        samples = []          # (秒, RSS MB 或 None)
        t_start = time.time()
        deadline = t_start + args.minutes * 60
        print("\n  时间     RSS(MB)   窗口 p95(ms)   累计请求")
        print("  " + "-" * 46)
        while time.time() < deadline:
            stop_evt.wait(min(args.sample, max(0.0, deadline - time.time())))
            if time.time() >= deadline:
                break
            el = time.time() - t_start
            with lock:
                recent = [ms for (ts, ms) in acc["ms"] if ts > time.time() - args.sample]
                ok = acc["ok"]
            mb = rss_mb(srv.pid)
            samples.append((el, mb))
            print("  %5.0fs   %8s   %10.1f   %10d"
                  % (el, ("%.1f" % mb) if mb is not None else "n/a", pct(recent, 95), ok))

        stop_evt.set()
        for b in boards:
            b.join(timeout=3)
        with lock:
            ok, fail, err = acc["ok"], acc["fail"], dict(acc["err"])
            all_ms = [ms for (_ts, ms) in acc["ms"]]

        # ---- 分桶看延迟趋势（前 1/3 vs 后 1/3）----------------------------
        third = max(1, len(all_ms) // 3)
        early = pct(all_ms[:third], 95)
        late = pct(all_ms[-third:], 95)

        print("\n" + "=" * 70)
        problems = []
        print("成功 %d / 失败 %d；服务端处理 p95：开头 %.1f ms → 结尾 %.1f ms"
              % (ok, fail, early, late))
        if fail:
            problems.append("有 %d 次上报失败（%s）" % (fail, err))
        if late > max(50.0, early * 2):
            problems.append("结尾 p95(%.1f ms) 是开头(%.1f ms)的两倍以上 —— 有劣化迹象"
                            % (late, early))

        rss_vals = [mb for (_el, mb) in samples if mb is not None]
        if len(rss_vals) >= 2:
            growth = rss_vals[-1] - rss_vals[0]
            print("RSS：%.1f MB → %.1f MB（涨 %.1f MB）" % (rss_vals[0], rss_vals[-1], growth))
            if growth > RSS_GROWTH_LIMIT_MB:
                problems.append("RSS 涨了 %.1f MB，超过 %d MB —— 可能有泄漏"
                                % (growth, RSS_GROWTH_LIMIT_MB))
        else:
            print("RSS：采不到（非 Windows 或进程已退出），跳过内存判据")

        if problems:
            print("稳定性: **不达标**")
            for p in problems:
                print("  ✗ %s" % p)
        else:
            print("稳定性: **没看出劣化**（%d 块板跑了 %.1f 分钟）"
                  % (args.boards, args.minutes))
        print("=" * 70)
        return 1 if problems else 0
    finally:
        srv.kill()
        try:
            srv.wait(timeout=5)
        except Exception:                                  # noqa: BLE001
            pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

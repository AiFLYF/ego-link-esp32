#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
假开发板 —— 不用硬件就能跑通整条链路 / 回归测试服务端。

它模拟 ESP32-S3-EYE 的行为：以 SAMPLE_HZ 采样 IMU，每 --period 毫秒把这一批
样本 POST 到 /api/telemetry（协议与固件 transport.c 完全一致），并按需触发
ask（等价于按一下 BOOT 键）。

用途：
  1. 没接板子时调仪表盘 / 调服务端逻辑
  2. 回归测试：--scenario walk 之后检查服务端有没有真的数出步数

用法：
    python tools/fake_board.py                                  # 默认 idle，跑 20s
    python tools/fake_board.py --scenario walk --seconds 30
    python tools/fake_board.py --scenario fall --ask-at 5
    python tools/fake_board.py --url http://192.168.1.20:8000 --scenario shake
    python tools/fake_board.py --scenario walk --expect-steps 8  # 带断言
    python tools/fake_board.py --device 第三组-07 --scenario tilt   # 多板：带设备名
    python tools/fake_board.py --scenario idle --no-cmd          # 故意不执行指令

多板（课堂 20 块板）场景：同时起多个实例、各给一个 --device 名字即可。
不带 --device 时**不发该字段**，等价于老固件 → 服务端会归到默认设备。

场景：
    idle    静置水平
    tilt    持续向右侧倾斜
    walk    1.8Hz 步行（含 |a| 起伏）
    shake   5Hz 剧烈晃动
    fall    第 6 秒起 250ms 自由落体
    mixed   静置 → 步行 → 晃动 → 跌落，依次切换
"""

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SAMPLE_HZ = 100.0

# 板子直连局域网里的服务器，不走任何 HTTP 代理。系统里装了 Clash / v2ray 之类的
# 话，http_proxy 会把 127.0.0.1 和 192.168.x.x 的请求也劫走（表现为 502），
# 所以这里显式装一个空 ProxyHandler 绕开环境变量。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------
# 场景发生器：返回屏幕坐标系下的 (x, y, z)，单位 g
# --------------------------------------------------------------------------
def scen_idle(t, ph):
    return (0.0, 0.0, 1.0)


def scen_tilt(t, ph):
    return (0.72, 0.0, 0.70)


def scen_walk(t, ph):
    s = math.sin(2 * math.pi * 1.8 * t)
    return (0.10 * s, 0.05 * math.cos(2 * math.pi * 1.8 * t), 1.0 + 0.55 * s)


def scen_shake(t, ph):
    s = math.sin(2 * math.pi * 5.0 * t)
    return (0.9 * s, 0.4 * math.cos(2 * math.pi * 5.0 * t), 1.0 + 0.9 * s)


def scen_fall(t, ph):
    if 6.0 <= t < 6.25:
        return (0.03, -0.02, 0.02)          # 失重
    if 6.25 <= t < 6.35:
        return (0.4, 0.3, 1.6)              # 落地冲击
    return (0.0, 0.0, 1.0)


def scen_mixed(t, ph):
    if t < 5:
        return scen_idle(t, ph)
    if t < 15:
        return scen_walk(t - 5, ph)
    if t < 21:
        return scen_shake(t - 15, ph)
    if 21 <= t < 21.25:
        return (0.03, -0.02, 0.02)
    return scen_idle(t, ph)


SCENARIOS = {
    "idle": scen_idle,
    "tilt": scen_tilt,
    "walk": scen_walk,
    "shake": scen_shake,
    "fall": scen_fall,
    "mixed": scen_mixed,
}


def post(url, payload, timeout=8.0):
    req = urllib.request.Request(
        url.rstrip("/") + "/api/telemetry",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="假开发板：不用硬件跑通整条链路")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--scenario", default="idle", choices=sorted(SCENARIOS))
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--period", type=float, default=0.5,
                    help="上报周期（秒），对应固件的 RW1_TELEMETRY_PERIOD_MS")
    ap.add_argument("--sample-hz", type=float, default=SAMPLE_HZ,
                    help="本地采样率（Hz），对应固件的 RW1_SAMPLE_PERIOD_MS")
    ap.add_argument("--source", default="SC7A20")
    ap.add_argument("--orient", type=int, default=None,
                    help="模拟上报方向档位 oN（0..15）。不带则**不发该字段**，"
                         "等价于不支持 oN 的老固件")
    ap.add_argument("--device", default=None,
                    help="设备名（对应固件的 device 字段）。不带则**省略该字段**，"
                         "模拟不带 device 的老固件；多板场景下每块板给一个不同的名字")
    ap.add_argument("--noise", type=float, default=0.012, help="传感器噪声幅度（g）")
    ap.add_argument("--ask-at", type=float, action="append", default=[],
                    help="在第 N 秒发一次 ask（等价于按 BOOT），可重复")
    ap.add_argument("--expect-steps", type=int, default=None,
                    help="结束时断言服务端窗口内步数 >= 该值")
    ap.add_argument("--capture-n", type=int, default=20,
                    help="执行 capture_once 时累积多少个样本（对应固件的 CAPTURE_N）")
    ap.add_argument("--no-cmd", action="store_true",
                    help="收到指令故意不执行（用于验证服务端的超时判定）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    gen = SCENARIOS[args.scenario]
    rnd = random.Random(20260918)          # 固定种子，便于复现
    per_batch = max(1, int(round(args.sample_hz * args.period)))
    batches = max(1, int(round(args.seconds / args.period)))
    ask_marks = set(int(round(a / args.period)) for a in args.ask_at)

    print("假开发板 → %s  场景=%s  %d 批 × %d 样本 @ %.0fHz"
          % (args.url, args.scenario, batches, per_batch, args.sample_hz))
    print("-" * 66)

    t0 = time.time()
    ok = fail = 0
    last_activity = None
    replies = []
    pending_cmd = None          # 已收到、待下一帧执行的指令 id
    cmds_received = 0
    results_sent = 0

    for b in range(batches):
        # 让模拟时间跟上真实时间，服务端靠"到达间隔/样本数"推算采样率
        target = t0 + (b + 1) * args.period
        sleep = target - time.time()
        if sleep > 0:
            time.sleep(sleep)

        batch = []
        base_t = b * args.period
        for i in range(per_batch):
            t = base_t + i / args.sample_hz
            x, y, z = gen(t, b)
            batch.append([
                round(x + rnd.gauss(0, args.noise), 4),
                round(y + rnd.gauss(0, args.noise), 4),
                round(z + rnd.gauss(0, args.noise), 4),
            ])

        payload = {
            "x": batch[-1][0], "y": batch[-1][1], "z": batch[-1][2],
            "source": args.source,
            "batch": batch,
            "ask": b in ask_marks,
        }
        if args.device:
            payload["device"] = args.device
        if args.orient is not None:
            payload["o"] = args.orient
        if b in ask_marks:
            payload["q"] = "我现在的运动状态怎么样？"

        # 模拟板端执行远程指令：上一帧收到 cmd，就用**本帧**的样本算结果，本帧带回。
        # 与固件时序一致——cmd 挂在第 N 帧响应上，第 N+1 帧请求里带 result。
        if pending_cmd is not None:
            cid, cname, cparams = pending_cmd
            res = {"id": cid, "ok": True}
            if cname == "capture_once":
                n = min(args.capture_n, len(batch))
                head = batch[:n]
                mx = sum(p[0] for p in head) / n
                my = sum(p[1] for p in head) / n
                mz = sum(p[2] for p in head) / n
                mags = [math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2) for p in head]
                mm = sum(mags) / n
                std = math.sqrt(sum((m - mm) ** 2 for m in mags) / n)
                res.update({"ms": round(n / args.sample_hz * 1000, 1), "n": n,
                            "x": round(mx, 4), "y": round(my, 4), "z": round(mz, 4),
                            "std": round(std, 4)})
                note = "x=%+.3f y=%+.3f z=%+.3f (%d 样本, σ=%.4f)" % (mx, my, mz, n, std)
            else:
                # led_blink / led_set：板端立刻完成，没有测量值
                res.update({"ms": 1.0, "n": 0})
                note = json.dumps(cparams, ensure_ascii=False)
            payload["result"] = res
            print("  t=%5.1fs  执行指令 %s(%s) → %s"
                  % (time.time() - t0, cname, cid, note))
            pending_cmd = None
            results_sent += 1

        try:
            out = post(args.url, payload)
            ok += 1
            act = out.get("activity", "?")
            if act != last_activity:
                print("  t=%5.1fs  活动: %s" % (time.time() - t0, act))
                last_activity = act
            if out.get("reply"):
                replies.append(out["reply"])
            if out.get("pending"):
                print("  t=%5.1fs  AI 生成中…" % (time.time() - t0))
            cmd = out.get("cmd")
            if cmd:
                cmds_received += 1
                if args.no_cmd:
                    print("  t=%5.1fs  收到指令 %s 但按 --no-cmd 故意忽略（用于测超时）"
                          % (time.time() - t0, cmd.get("id")))
                else:
                    print("  t=%5.1fs  收到指令 %s（%s），下一帧执行"
                          % (time.time() - t0, cmd.get("id"), cmd.get("name")))
                    pending_cmd = (cmd["id"], cmd.get("name", "?"), cmd.get("params") or {})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            fail += 1
            if fail <= 3:
                print("  POST 失败: %s" % exc)

    print("-" * 66)
    print("上报 %d 成功 / %d 失败；收到指令 %d 条，回传结果 %d 条"
          % (ok, fail, cmds_received, results_sent))
    if replies:
        print("最后一次 AI 回复：%s" % replies[-1][:120])

    # ---- 断言 ----
    rc = 0
    if fail:
        print("FAIL: 有 %d 次上报失败" % fail)
        rc = 1
    if args.expect_steps is not None:
        try:
            # 带 --device 时必须查那一台：不带参数会落到"最近上报过的设备"，
            # 多板同时在跑时可能查到别的板子上，断言就失去意义了。
            url = args.url.rstrip("/") + "/api/latest"
            if args.device:
                url += "?device=" + urllib.parse.quote(args.device, safe="")
            with OPENER.open(url, timeout=5) as r:
                snap = json.loads(r.read().decode("utf-8"))
            steps = snap.get("step_count", 0)
            print("服务端窗口内步数 = %d（期望 >= %d）" % (steps, args.expect_steps))
            if steps < args.expect_steps:
                print("FAIL: 步数不足")
                rc = 1
            else:
                print("PASS: 步数达标")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("FAIL: 读取 /api/latest 失败: %s" % exc)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

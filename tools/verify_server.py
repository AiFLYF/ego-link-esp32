#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务端回归测试 —— 不需要开发板，也不需要真实大模型。

自己拉起一个 server.py（临时端口 + 临时数据目录），用 tools/fake_board.py 的
场景发生器灌数据，然后逐项断言：分类是否正确、步数/跌落/晃动是否真的检测出来、
落盘文件是否写对、大模型是否真的不阻塞板端、旧协议是否还能用。

用法：
    python tools/verify_server.py
    python tools/verify_server.py --keep      # 保留临时目录，方便看日志
退出码 0 = 全部通过。
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_PY = os.path.join(ROOT, "server", "server.py")
PY = sys.executable

# 沙箱/本机可能有全局 http_proxy，会把 127.0.0.1 的请求也劫走
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  — " + detail) if detail else ""))
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get_json(url, timeout=6):
    with OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url, payload, timeout=8):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_raw(url, raw, timeout=8):
    req = urllib.request.Request(url, data=raw.encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def firmware_body(samples, source="SC7A20", ask=False, question=None):
    """逐字节复刻 device/main/transport.c build_body() 的输出。

    固件是**手写** JSON（刻意不用 cJSON，因为 cJSON 用 %1.15g 打印 double，
    float 0.012f 会输出 "0.0120000001634057"，50 样本批次要 2.7KB）。所以
    C 那边的格式串和服务端解析器之间是一份隐式契约——这里把它钉死：
    任何一边改了格式，这个用例都会红。
    """
    pts = ",".join("[%.3f,%.3f,%.3f]" % s for s in samples)
    body = ('{"batch":[%s],"x":%.3f,"y":%.3f,"z":%.3f,"source":"%s","ask":%s'
            % (pts, samples[-1][0], samples[-1][1], samples[-1][2],
               source, "true" if ask else "false"))
    if ask:
        body += ',"q":"%s"' % question
    return body + "}"


# --------------------------------------------------------------------------
# 假大模型：故意慢，用来证明板端不会被它拖住
# --------------------------------------------------------------------------
class MockLLM(BaseHTTPRequestHandler):
    delay = 6.0
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        MockLLM.calls.append(time.time())
        time.sleep(MockLLM.delay)
        body = json.dumps({"choices": [{"message": {"content": "这是慢速假大模型的回答。"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------
# 用假开发板灌一段场景数据
# --------------------------------------------------------------------------
def feed(port, scenario, seconds, ask_at=None, expect_steps=None):
    cmd = [PY, os.path.join(HERE, "fake_board.py"),
           "--url", "http://127.0.0.1:%d" % port,
           "--scenario", scenario, "--seconds", str(seconds), "--quiet"]
    for a in (ask_at or []):
        cmd += ["--ask-at", str(a)]
    if expect_steps is not None:
        cmd += ["--expect-steps", str(expect_steps)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def latest(port):
    return get_json("http://127.0.0.1:%d/api/latest" % port)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="rw1verify-")
    data_dir = os.path.join(tmp, "data")
    port = free_port()
    llm_port = free_port()
    env = dict(os.environ)
    # 让 server 子进程访问 127.0.0.1 上的假大模型时绕开全局代理
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"

    print("=" * 70)
    print("服务端回归测试   端口=%d  数据目录=%s" % (port, data_dir))
    print("=" * 70)

    srv = subprocess.Popen([PY, "-u", SERVER_PY, "--port", str(port),
                            "--data-dir", data_dir, "--retain-days", "0"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, env=env, cwd=ROOT)
    mock = ThreadingHTTPServer(("127.0.0.1", llm_port), MockLLM)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    rc = 0
    try:
        # 等服务器起来
        for _ in range(50):
            try:
                get_json("http://127.0.0.1:%d/api/latest" % port, timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            print("服务器没起来")
            return 1

        # ---- 1. 静置 ----------------------------------------------------
        print("\n[1] 静置")
        feed(port, "idle", 6)
        act = latest(port)["activity"]
        check("静置·水平", act == "静置·水平", "实际: %s" % act)

        # ---- 2. 倾斜方向（屏幕坐标系）------------------------------------
        print("\n[2] 向右倾斜")
        feed(port, "tilt", 6)
        act = latest(port)["activity"]
        check("静置·向右倾斜", act == "静置·向右倾斜", "实际: %s" % act)

        # ---- 3. 计步（修复前物理上做不到）--------------------------------
        print("\n[3] 步行 1.8Hz / 14 秒")
        rc2, out = feed(port, "walk", 14, expect_steps=8)
        snap = latest(port)
        check("检测到步行而非晃动", "步行" in snap["activity"], "实际: %s" % snap["activity"])
        check("窗口内步数 >= 8", snap["step_count"] >= 8, "实际: %d" % snap["step_count"])
        check("fake_board 自检通过", rc2 == 0)

        # ---- 4. 晃动 ----------------------------------------------------
        print("\n[4] 晃动 5Hz / 6 秒")
        feed(port, "shake", 6)
        snap = latest(port)
        check("判定为剧烈晃动", snap["activity"] == "剧烈晃动", "实际: %s" % snap["activity"])
        check("晃动计数为窗口内计数（非累计）", 0 < snap["shake_count"] <= 20,
              "实际: %d" % snap["shake_count"])

        # ---- 5. 跌落（250ms 失重）---------------------------------------
        print("\n[5] 跌落（第 6 秒 250ms 失重）")
        seen = []
        proc = subprocess.Popen(
            [PY, os.path.join(HERE, "fake_board.py"), "--url", "http://127.0.0.1:%d" % port,
             "--scenario", "fall", "--seconds", "9", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            try:
                seen.append(latest(port)["activity"])
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.3)
        check("检测到疑似跌落", any("跌落" in a for a in seen),
              "采到 %d 个状态" % len(seen))

        # ---- 6. 持久化 ---------------------------------------------------
        print("\n[6] JSONL 落盘")
        tel = [f for f in os.listdir(data_dir) if f.startswith("telemetry-")]
        evt = [f for f in os.listdir(data_dir) if f.startswith("events-")]
        check("生成 telemetry-*.jsonl", len(tel) == 1, "实际: %s" % tel)
        check("生成 events-*.jsonl", len(evt) == 1, "实际: %s" % evt)
        if tel:
            lines = open(os.path.join(data_dir, tel[0]), encoding="utf-8").read().splitlines()
            rec = json.loads(lines[0])
            check("遥测文件里确实是遥测记录", rec.get("k") == "t", "首行 k=%s" % rec.get("k"))
            check("遥测含样本数组且非空", isinstance(rec.get("s"), list) and len(rec["s"]) > 0)
            check("遥测已降采样到 ~10Hz", len(rec["s"]) <= 12, "每批 %d 个点" % len(rec["s"]))
            check("遥测行数 >= 50", len(lines) >= 50, "实际 %d 行" % len(lines))
        if evt:
            kinds = set()
            for ln in open(os.path.join(data_dir, evt[0]), encoding="utf-8"):
                kinds.add(json.loads(ln).get("k"))
            check("事件文件里只有事件记录", kinds == {"e"}, "实际: %s" % kinds)

        # ---- 7. 大模型不阻塞板端（P0 修复的核心）-------------------------
        print("\n[7] 慢速大模型（%d 秒）不阻塞板端" % MockLLM.delay)
        srv.kill()
        srv.wait()
        env2 = dict(env)
        env2["RW1_LLM_API_KEY"] = "sk-test"
        env2["RW1_LLM_BASE_URL"] = "http://127.0.0.1:%d/v1" % llm_port
        env2["RW1_LLM_MODEL"] = "mock"
        srv = subprocess.Popen([PY, "-u", SERVER_PY, "--port", str(port),
                                "--data-dir", data_dir, "--retain-days", "0"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env=env2, cwd=ROOT)
        for _ in range(50):
            try:
                get_json("http://127.0.0.1:%d/api/latest" % port, timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)

        t0 = time.time()
        out = post_json("http://127.0.0.1:%d/api/telemetry" % port,
                        {"x": 0.0, "y": 0.0, "z": 1.0, "source": "SC7A20",
                         "ask": True, "q": "现在怎么样？",
                         "batch": [[0.0, 0.0, 1.0]] * 50})
        dt = time.time() - t0
        check("ask 帧 1 秒内返回", dt < 1.0, "实际 %.2fs" % dt)
        check("返回 pending 标志", out.get("pending") is True, "实际: %s" % out.get("pending"))
        check("立刻给出占位文案", "思考" in out.get("reply", ""), "实际: %s" % out.get("reply"))

        deadline = time.time() + MockLLM.delay + 8
        got = None
        while time.time() < deadline:
            snap = latest(port)
            if "慢速假大模型" in snap.get("ai_reply", ""):
                got = snap
                break
            time.sleep(0.5)
        check("后台线程把大模型结果写回", got is not None)
        if got:
            check("pending 已复位", got.get("ai_pending") is False)
            check("AI 来源标记为大模型", got.get("ai_mode") == "大模型",
                  "实际: %s" % got.get("ai_mode"))

        # 板端连续上报时能顺带拿到大模型结果
        out2 = post_json("http://127.0.0.1:%d/api/telemetry" % port,
                         {"x": 0.0, "y": 0.0, "z": 1.0, "source": "SC7A20",
                          "batch": [[0.0, 0.0, 1.0]] * 50})
        check("后续普通帧带回最新回复", "慢速假大模型" in out2.get("reply", ""),
              "实际: %s" % out2.get("reply", "")[:40])

        # ---- 8. 旧协议向后兼容 -------------------------------------------
        print("\n[8] 旧协议（单帧 x/y/z，无 batch）")
        out3 = post_json("http://127.0.0.1:%d/api/telemetry" % port,
                         {"x": 0.1, "y": 0.2, "z": 1.0, "source": "legacy"})
        check("单帧载荷仍可用", out3.get("ok") is True, "实际: %s" % out3)

        # ---- 9. 畸形载荷 --------------------------------------------------
        print("\n[9] 畸形载荷")
        for bad, label in [({}, "空对象"), ({"x": "abc", "y": 1, "z": 1}, "非数字"),
                           ({"batch": [["a", "b", "c"]]}, "batch 全是垃圾")]:
            try:
                post_json("http://127.0.0.1:%d/api/telemetry" % port, bad)
                check("%s 被拒绝" % label, False, "竟然接受了")
            except urllib.error.HTTPError as e:
                check("%s 被拒绝" % label, e.code == 400, "HTTP %d" % e.code)
            except Exception as e:  # noqa: BLE001
                check("%s 被拒绝" % label, False, str(e))

        # ---- 10. 采样率自校准 ---------------------------------------------
        print("\n[10] 采样率推算")
        snap = latest(port)
        hz = snap.get("sample_hz", 0)
        check("采样率推算合理（50~150Hz）", 50 <= hz <= 150, "实际: %s Hz" % hz)

        # ---- 11. 固件字节格式契约 -----------------------------------------
        # 上面各节用的是 json.dumps 生成的载荷，这一节用**逐字节复刻**固件
        # build_body() 的字符串，确保 C 与服务端的格式契约没有悄悄漂移。
        print("\n[11] 固件手写 JSON 的格式契约（transport.c build_body）")
        raw = firmware_body([(0.0, 0.0, 1.0)] * 50)
        out4 = post_raw("http://127.0.0.1:%d/api/telemetry" % port, raw)
        check("50 样本固件帧被接受", out4.get("ok") is True,
              "帧长 %d 字节" % len(raw))
        check("50 样本固件帧分类正确", out4.get("activity") == "静置·水平",
              "实际: %s" % out4.get("activity"))

        raw_ask = firmware_body([(0.7, 0.0, 0.71)] * 50, ask=True,
                                question="我现在的运动状态怎么样？")
        out5 = post_raw("http://127.0.0.1:%d/api/telemetry" % port, raw_ask)
        check("带 ask+q 的固件帧被接受", out5.get("ok") is True)
        check("ask 帧方向判定正确", out5.get("activity") == "静置·向右倾斜",
              "实际: %s" % out5.get("activity"))

        raw_neg = firmware_body([(-0.012, 0.998, 0.031)] * 50)
        out6 = post_raw("http://127.0.0.1:%d/api/telemetry" % port, raw_neg)
        check("负值/三位小数解析正确",
              out6.get("ok") is True and "右" in out6.get("activity", ""),
              "实际: %s" % out6.get("activity"))

        raw_src = firmware_body([(0.0, 0.0, 1.0)] * 50, source="QMA7981")
        out7 = post_raw("http://127.0.0.1:%d/api/telemetry" % port, raw_src)
        check("source 字段被正确读取", latest(port).get("source") == "QMA7981",
              "实际: %s" % latest(port).get("source"))

    finally:
        for p in (srv,):
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        mock.shutdown()
        if args.keep:
            print("\n临时目录保留在: %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 70)
    print("结果: %d/%d 通过" % (passed, total))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAIL: %s  %s" % (name, detail))
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

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
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_PY = os.path.join(ROOT, "server", "server.py")
PY = sys.executable

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 沙箱/本机可能有全局 http_proxy，会把 127.0.0.1 的请求也劫走
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ⚠️ **不只是测试进程自己**（上面那个 OPENER）：**每个子进程也必须**拿到这份环境。
# `fake_board.py` 是往 127.0.0.1 发遥测的 —— 一旦它继承了环境里的代理，
# POST 全被代理劫走返回 502，板子就"永远注册不上"：第 12/13/14 节整片红，
# 第 5 节跌落的采样数忽多忽少（有多少 POST 侥幸绕过去，就采到多少个状态）。
#
# 2026-10-10 查实：8 处 subprocess 调用里有 **6 处漏传 env=**，全都继承了代理。
# 这就是手册 T59（假板子起不来）和 T62（跌落偶发失败）的**同一个真因** ——
# 它们根本不是"偶发"，是"有多少子进程被代理吃掉"的必然结果。
CHILD_ENV = dict(os.environ)
CHILD_ENV["no_proxy"] = CHILD_ENV["NO_PROXY"] = "127.0.0.1,localhost"
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    CHILD_ENV.pop(_k, None)

# 等板子"在册"要等多久（× 0.25 秒）。**别用 10 秒** ——
# 实测这个沙箱里**空的 `python -c pass` 就要 2.0~2.7 秒**（进程创建被套了一层壳），
# `fake_board.py` 起来要 3 秒左右，起来之后还要等第一个遥测周期才会上报。
# 原来只给 10 秒，余量太薄，机器一忙就"板子没上报" —— 被误判成产品 bug（手册 T59）。
BOARD_WAIT_STEPS = 160          # 160 × 0.25s = 40 秒

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    try:
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  — " + detail) if detail else ""))
    except UnicodeEncodeError:
        safe_detail = detail.encode("ascii", errors="backslashreplace").decode("ascii")
        safe_name = name.encode("ascii", errors="backslashreplace").decode("ascii")
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", safe_name, ("  — " + safe_detail) if detail else ""))
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


def post_bytes(url, data, timeout=25):
    """POST 原始字节（语音上传用：PCM 里必然有 0x00，不能走字符串那条路）。
    响应仍按 JSON 解析。"""
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/octet-stream"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _expect_400(url, payload):
    """POST 一个应当被拒绝的载荷，返回 True 表示确实拿到了 HTTP 400。"""
    try:
        post_json(url, payload)
        return False
    except urllib.error.HTTPError as e:
        return e.code == 400
    except Exception:  # noqa: BLE001
        return False


def _wait_cmd(port, cid, timeout, device=None):
    """等一条指令走到终态，返回它的记录（超时则返回最后看到的样子）。

    `device` 建议显式给：不给就走"最近上报的那台"，而多节测试里前几节的假板子
    可能刚好在那一刻掉线，"最近上报"会切到别人 —— 于是查不到自己的命令。
    （2026-09-23 真踩到：手工复现完全正常、套件里稳定失败，就是这条竞态。）
    """
    deadline = time.time() + timeout
    rec = None
    q = ("?device=" + urllib.parse.quote(device, safe="")) if device else ""
    while time.time() < deadline:
        cmds = get_json("http://127.0.0.1:%d/api/commands%s" % (port, q)).get("commands", [])
        rec = next((c for c in cmds if c["id"] == cid), None)
        if rec and rec["state"] in ("done", "failed", "timeout"):
            return rec
        time.sleep(0.3)
    return rec


def _c_json_escape(s):
    """逐字节复刻 transport.c 的 json_escape_append()。

    固件只转义这五个字符，其余（含非 ASCII）原样输出 UTF-8 字节 —— 设备名是
    用户在配网页手填的，一个裸引号就能把整帧 JSON 打坏，所以这个转义是硬契约。
    """
    out = []
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


def firmware_body(samples, source="SC7A20", ask=False, question=None, device="rw1-AB12",
                  orient=None):
    """逐字节复刻 device/main/transport.c build_body() 的输出。

    固件是**手写** JSON（刻意不用 cJSON，因为 cJSON 用 %1.15g 打印 double，
    float 0.012f 会输出 "0.0120000001634057"，50 样本批次要 2.7KB）。所以
    C 那边的格式串和服务端解析器之间是一份隐式契约——这里把它钉死：
    任何一边改了格式，这个用例都会红。

    字段顺序也照抄：device 在最前（服务端拿到第一件事就是按它分片）。
    """
    pts = ",".join("[%.3f,%.3f,%.3f]" % s for s in samples)
    # 方向档位字段：板端每帧都带（`"o":N`）。orient=None 时**故意不发**，
    # 用来模拟不带这个字段的老固件。
    o_part = "" if orient is None else (',"o":%d' % orient)
    body = ('{"device":"%s"%s,"batch":[%s],"x":%.3f,"y":%.3f,"z":%.3f,"source":"%s","ask":%s'
            % (_c_json_escape(device), o_part, pts, samples[-1][0], samples[-1][1],
               samples[-1][2], source, "true" if ask else "false"))
    if ask:
        body += ',"q":"%s"' % question
    return body + "}"


# --------------------------------------------------------------------------
# 假大模型：故意慢，用来证明板端不会被它拖住
# --------------------------------------------------------------------------
class MockLLM(BaseHTTPRequestHandler):
    delay = 6.0
    asr_delay = 0.0    # 识别也支持人为放慢 —— 用来测"迟到的识别结果被丢弃"（第 6 周）
    calls = []
    seen = []          # 收到的请求体（用来验证"多轮历史真的带进了请求"）
    asr_text = "板子现在怎么样"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        path = self.path.split("?")[0]

        # 语音识别：走同一个假上游，但**格式完全不同**（{"text": ...}）。
        # 不区分路径的话，ASR 会拿到大模型的 choices 结构、解析出空文本。
        if path.endswith("/audio/transcriptions"):
            MockLLM.calls.append(time.time())
            if MockLLM.asr_delay:
                time.sleep(MockLLM.asr_delay)
            body = json.dumps({"text": MockLLM.asr_text}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            MockLLM.seen.append(json.loads(raw.decode("utf-8")))
        except ValueError:
            MockLLM.seen.append({})
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
    p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", env=CHILD_ENV)
    return p.returncode, p.stdout + p.stderr


def feed_observe(port, scenario, seconds, device=None, with_proc=False, expect_steps=None):
    """跑一段假板子场景，**边跑边采样** `activity`，返回观察到的活动词列表。

    为什么不能跑完再读一次：`activity` 是"随最后一批数据变化"的瞬时字段。
    场景结束之后再读，读到的是**收尾那一批**的分类结果 —— 晃动场景的尾部
    幅度已经衰减，很容易被判成"运动/步行"。2026-10-30 实测：同一份代码
    连跑三次，第三次就报了「判定为剧烈晃动 → 实际: 运动/步行(约16步)」，
    而前两次都是绿的。根因是观测时机，不是分类器。

    同一天「步行」那条也栽在同一个坑上：它跑完 14 秒只读一次快照，
    而 1.8 Hz 步行在 0.6 秒短窗里只有约 1 个周期，零穿越率本来就在
    1.2 Hz 判据附近抖 —— 收尾那一帧撞上边界就报「运动 (峰值 1.6g)」。
    实测（100 ms 采样跑满 14 秒）：**20 段全是"运动/步行"，没有一次抖动**，
    所以产品是对的，错的是"只看一眼"的观测方式。

    与第 [5] 节的轮询同理：**采样率必须快于被测现象的可见时长**。这里用
    100 ms（场景持续 6 秒 → 约 60 个采样点），抖动就不再决定结果。

    `with_proc=True` 时额外返回 `(seen, returncode, output)`，给需要同时
    校验假板子自检（`--expect-steps`）的场景用。
    """
    cmd = [PY, os.path.join(HERE, "fake_board.py"),
           "--url", "http://127.0.0.1:%d" % port,
           "--scenario", scenario, "--seconds", str(seconds), "--quiet"]
    if expect_steps is not None:
        cmd += ["--expect-steps", str(expect_steps)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         encoding="utf-8", errors="replace", env=CHILD_ENV)
    seen = []
    while p.poll() is None:
        try:
            seen.append(latest(port, device)["activity"])
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.1)
    if with_proc:
        out = p.stdout.read() if p.stdout is not None else ""
        return seen, p.returncode, out
    return seen


def latest(port, device=None):
    """取快照。**多板场景下一定要带上 device** ——
    不带就是"最近上报的那台"，同时有别的板子在跑时，谁后上报就查谁，
    断言会随上报时序随机失败（2026-09-26 踩到：命令明明发给了 r12_dev，
    却去查另一台的事件流，报"命令事件进了事件流"失败，单独复跑又全绿）。"""
    q = "?device=" + urllib.parse.quote(device) if device else ""
    return get_json("http://127.0.0.1:%d/api/latest%s" % (port, q))


def events_of(port, device=None):
    """从 **SSE** 读一帧，拿该设备当前的事件流。

    2026-10-12 起事件流是 SSE 的独通道：`/api/latest`（网页 10Hz 轮询）为省
    "锁内序列化全量历史"的开销不再带 events（服务端 `snapshot(with_events=
    False)`）。这是**有意的契约分工**（命令历史同样只走 SSE 与 /api/commands），
    所以断言改的是**数据来源**，不是把字段加回轮询通道去迁就旧写法。
    SSE 首帧即时（服务端先发再 sleep），读一帧即拿到当时的完整 deque（≤200 条）。
    """
    q = "?device=" + urllib.parse.quote(device) if device else ""
    try:
        with OPENER.open("http://127.0.0.1:%d/api/stream%s" % (port, q), timeout=8) as r:
            line = r.readline().decode("utf-8", "replace")
        if not line.startswith("data: "):
            return []
        return json.loads(line[6:]).get("events", [])
    except Exception as exc:  # noqa: BLE001 —— 读失败按空处理，让断言把真相报出来
        print("    [警告] SSE 读事件流失败: %s" % exc)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="rw1verify-")
    data_dir = os.path.join(tmp, "data")
    port = free_port()
    llm_port = free_port()
    # 子进程统一用 CHILD_ENV（已绕开代理，见文件顶部的说明）——
    # 别再各自 `dict(os.environ)`：那正是 T59/T62 的成因。
    env = CHILD_ENV

    print("=" * 70)
    print("服务端回归测试   端口=%d  数据目录=%s" % (port, data_dir))
    print("=" * 70)

    srv = subprocess.Popen([PY, "-u", SERVER_PY, "--port", str(port),
                            "--data-dir", data_dir, "--retain-days", "0",
                            "--cmd-timeout", "5"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           encoding="utf-8", errors="replace", env=env, cwd=ROOT)
    mock = ThreadingHTTPServer(("127.0.0.1", llm_port), MockLLM)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    rc = 0
    nlboard = None          # 第 4 周的自然语言测试里才起；提前定义，finally 才能安全收尾
    srv2 = None             # 第 25 节（访问密钥门）的第二个实例；同上
    TOKEN = "classroom-secret-42"
    # ⚠️ free_port() 是"bind(0) 再立刻释放"，而 **Windows 的临时端口是游标式
    # 分配** —— 主实例刚占过一个端口，第二次极可能把同一个端口再发回来。
    # 撞了的话第 25 节的请求全会打到主实例（没有门）上去，三个 401 断言
    # 会集体误报成"门没生效"。所以这里重抽，并在第 25 节再加一道
    # "新实例的设备列表必须是空的"实测判别（见下文）。
    port2 = free_port()
    while port2 == port:
        port2 = free_port()
    # ⚠️ 变量名不能叫 env2：第 21 节（假大模型）已经有一个同名 env2，
    # 会在 try 块里把这里覆盖掉 —— 第一版就是这么踩的（srv2 拿到的是
    # 只有 LLM 密钥的环境，令牌门"没生效"，三个 401 断言集体误报）。
    token_env = dict(CHILD_ENV)  # 照抄 T63 的纪律：别各自 dict(os.environ)
    token_env["RW1_API_TOKEN"] = TOKEN
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

        # ---- 0. 静态自检：看护线程每天要调的方法，现在就必须存在 -------------
        # 2026-10-25 事故：JsonlLogger 的清理方法曾叫 _purge_old，而看护线程调的
        # 是 LOGGER.purge_old() —— 服务端连跑 24 小时后那里抛 AttributeError，
        # **看护线程整个死掉**（设备离线判定/指令超时/日志清理全部静默停摆）。
        # 短期回归测不到它（谁也不会把测试跑满 24 小时），所以这里用 importlib
        # 把 server.py 当模块加载（import 只定义、不执行 main()），点名
        # 「长期路径上要调的东西，现在就存在」——T69「工具写好了 ≠ 生效」的变体：
        # 方法改了名，调用点未必跟上。
        print("\n[0] 静态自检")
        import importlib.util
        spec = importlib.util.spec_from_file_location("rw1_server_static", SERVER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        check("看护线程每日调用的 JsonlLogger.purge_old 存在（24h 看护死亡的静态门禁）",
              callable(getattr(mod.JsonlLogger, "purge_old", None)),
              "实际: %r" % getattr(mod.JsonlLogger, "purge_old", None))

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
        # 边跑边采，别只看收尾那一帧（见 feed_observe 的注释：1.8 Hz 步行在
        # 0.6 秒短窗里只有约 1 个周期，零穿越率本来就在判据附近抖）。
        seen_walk, rc2, out = feed_observe(port, "walk", 14, with_proc=True,
                                           expect_steps=8)
        snap = latest(port)
        check("检测到步行而非晃动", any("步行" in a for a in seen_walk),
              "采到 %d 个状态，出现过: %s"
              % (len(seen_walk), sorted({a for a in seen_walk if a})))
        check("窗口内步数 >= 8", snap["step_count"] >= 8, "实际: %d" % snap["step_count"])
        _tail = (out or "").strip().splitlines()
        check("fake_board 自检通过", rc2 == 0,
              "rc=%d%s" % (rc2, ("  |  " + _tail[-1]) if _tail else ""))

        # ---- 4. 晃动 ----------------------------------------------------
        print("\n[4] 晃动 5Hz / 6 秒")
        # 边跑边采（见 feed_observe 的注释）：只看"这 6 秒里出现过剧烈晃动"，
        # 不看"收尾那一瞬间是什么" —— 后者由最后一批数据的相位决定，是抛硬币。
        seen_shake = feed_observe(port, "shake", 6)
        snap = latest(port)
        check("判定为剧烈晃动", "剧烈晃动" in seen_shake,
              "采到 %d 个状态，出现过: %s"
              % (len(seen_shake), sorted(set(seen_shake))))
        check("晃动计数为窗口内计数（非累计）", 0 < snap["shake_count"] <= 20,
              "实际: %d" % snap["shake_count"])

        # ---- 5. 跌落（250ms 失重）---------------------------------------
        print("\n[5] 跌落（第 6 秒 250ms 失重）")
        seen = []
        proc = subprocess.Popen(
            [PY, os.path.join(HERE, "fake_board.py"), "--url", "http://127.0.0.1:%d" % port,
             "--scenario", "fall", "--seconds", "9", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=CHILD_ENV)
        while proc.poll() is None:
            try:
                seen.append(latest(port)["activity"])
            except (urllib.error.URLError, OSError, ValueError):
                pass
            # ⚠️ 轮询必须**快于被测现象的可见时长**，否则这条断言本身就成了抛硬币。
            # 「疑似跌落」只在上报周期内可见（约 0.35~0.5 s：失重 250 ms + 下一批
            # 覆盖它之前的间隔）。原来这里 sleep(0.3)，加上请求本身约 0.1 s 的往返，
            # 实际采样间隔 ≈ 0.4 s，正好比可见窗长 —— 2026-10-30 实测同一天里
            # 连跑三次挂了一次，而用 50 ms 间隔复测时"跌落"命中 7 次（检测没问题）。
            # 所以这里改 0.1 s：抓住现象，而不是降低断言标准。
            time.sleep(0.1)
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
            ev_kinds = []
            for ln in open(os.path.join(data_dir, evt[0]), encoding="utf-8"):
                rec = json.loads(ln)
                kinds.add(rec.get("k"))
                ev_kinds.append(rec.get("kind"))
            check("事件文件里只有事件记录", kinds == {"e"}, "实际: %s" % kinds)
            # 跌落的**确定性判据**：事件是 append-only 的，不像 activity 字段
            # 只在那 0.5 秒里可见。上一节按 0.1s 轮询抓瞬时状态（验证的是板端
            # 屏幕会看到什么），这里再钉一次"事件真的落了盘" —— 两者互补：
            # 前者管"看得见"，后者管"留得住"，且不受机器忙闲影响。
            # （2026-10-30 采纳协作者的复核意见：轮询瞬时状态本质是碰运气。）
            deadline = time.time() + 2.0
            while "fall" not in ev_kinds and time.time() < deadline:
                time.sleep(0.1)
                ev_kinds = [json.loads(ln).get("kind")
                            for ln in open(os.path.join(data_dir, evt[0]), encoding="utf-8")]
            check("跌落事件已落盘（确定性判据，不依赖轮询时机）",
                  "fall" in ev_kinds, "实际事件类型: %s" % sorted(set(ev_kinds)))

        # ---- 7. 大模型不阻塞板端（P0 修复的核心）-------------------------
        print("\n[7] 慢速大模型（%d 秒）不阻塞板端" % MockLLM.delay)
        srv.kill()
        srv.wait()
        env2 = dict(CHILD_ENV)
        env2["RW1_LLM_API_KEY"] = "sk-test"
        env2["RW1_LLM_BASE_URL"] = "http://127.0.0.1:%d/v1" % llm_port
        env2["RW1_LLM_MODEL"] = "mock"
        # 语音识别指向**同一个假上游**（它按路径区分返回格式）。
        # 不配的话，语音那一段只能测到"没配 ASR 的回退"，测不到真实链路。
        env2["RW1_ASR_API_KEY"] = "sk-test"
        env2["RW1_ASR_BASE_URL"] = "http://127.0.0.1:%d/v1" % llm_port
        env2["RW1_ASR_MODEL"] = "mock-asr"
        srv = subprocess.Popen([PY, "-u", SERVER_PY, "--port", str(port),
                                "--data-dir", data_dir, "--retain-days", "0",
                                "--cmd-timeout", "5"],
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

        # 先把 8 秒窗口灌满「向右倾斜」，再发 ask 帧。
        # 注意：不灌的话窗口里还留着上一节的 (0,0,1) 样本，均值被稀释，
        # 方向会被判成「水平」。修 P1-6（晚到帧不再污染时间轴）之前，
        # 被夸大的 dt 会把旧样本挤出窗口，这条断言是**靠那个 bug 才通过**的。
        for _ in range(4):
            post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                     firmware_body([(0.7, 0.0, 0.71)] * 50))
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

        # ---- 12. 远程指令往返（第 2 周）------------------------------------
        print("\n[12] 远程指令 capture_once 往返")
        check("非法指令名被拒绝", _expect_400(
            "http://127.0.0.1:%d/api/command" % port, {"name": "rm -rf /"}))

        r12_dev = "往返-01"        # 固定设备名：不再依赖"最近上报的那台"（见 _wait_cmd 注释）
        board = subprocess.Popen(
            [PY, os.path.join(HERE, "fake_board.py"),
             "--url", "http://127.0.0.1:%d" % port, "--device", r12_dev,
             "--scenario", "tilt", "--seconds", "14", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=CHILD_ENV)
        # **等它真的在册**再下发，别用固定 sleep：板子还没报上第一帧时，
        # 服务端的 pick_device 会回退到"最近上报的那台"，命令就发给了别人 ——
        # 响应里 device_online 还是 True（那是别人的），断言却全在等自己的命令，
        # 表现为"稳定失败但手工复现完全正常"（2026-09-23 踩过）。
        for _ in range(BOARD_WAIT_STEPS):
            live = [d["id"] for d in get_json(
                "http://127.0.0.1:%d/api/devices" % port).get("devices", [])]
            if r12_dev in live:
                break
            time.sleep(0.25)
        else:
            check("测试用的板子已上报（%s 在册）" % r12_dev, False,
                  "实际在册: %s" % live)

        r = post_json("http://127.0.0.1:%d/api/command" % port,
                      {"name": "capture_once", "device": r12_dev})
        check("命令确实发给了指定设备（没被回退给别人）",
              r.get("device") == r12_dev, "实际: %s" % r.get("device"))
        cid = r.get("id")
        check("下发指令返回 request_id", bool(cid), "实际: %s" % cid)
        check("下发响应带设备在线状态", r.get("device_online") is True,
              "实际: %s" % r.get("device_online"))

        rec = _wait_cmd(port, cid, 14, device=r12_dev)
        board.wait(timeout=25)

        # 用服务端记录的状态迁移历史断言，**不再靠轮询捕捉中间态**：
        # sent 只在下发帧和回传帧之间存活约一个遥测周期（0.5 s），
        # 用 0.3 s 轮询去抓它本质是竞态断言，会因为采样时机随机失败（P0-1）。
        states = [s for s, _ in (rec or {}).get("history", [])]
        check("指令最终走到 done", rec is not None and rec["state"] == "done",
              "实际: %s（历史 %s）" % (rec and rec["state"], "→".join(states)))
        check("历史里完整记录了 queued→sent→done",
              states[:3] == ["queued", "sent", "done"], "实际: %s" % states)
        if rec and rec.get("result"):
            res = rec["result"]
            check("回传里带着同一个 request_id", res.get("id") == cid)
            check("回传了样本数", (res.get("n") or 0) >= 1, "n=%s" % res.get("n"))
            check("回传了耗时", (res.get("ms") or 0) > 0, "ms=%s" % res.get("ms"))
            check("回传的 z 分量接近 0.70（tilt 场景）",
                  abs((res.get("z") or 0) - 0.70) < 0.15, "z=%s" % res.get("z"))
            check("回传了标准差", (res.get("std") or -1) >= 0, "std=%s" % res.get("std"))
        else:
            check("回传里带着同一个 request_id", False, "没有 result")
        check("命令事件进了事件流",
              any("capture_once" in (e.get("text") or "")
                  for e in events_of(port, r12_dev)))

        # ---- 13. 指令超时看护 ---------------------------------------------
        print("\n[13] 指令超时（板子故意不执行）")
        r13_dev = "聋板-01"        # 同第 12 节：固定设备名，别依赖"最近上报的那台"
        deaf = subprocess.Popen(
            [PY, os.path.join(HERE, "fake_board.py"),
             "--url", "http://127.0.0.1:%d" % port, "--device", r13_dev,
             "--scenario", "idle", "--seconds", "14", "--no-cmd", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=CHILD_ENV)
        for _ in range(BOARD_WAIT_STEPS):
            live = [d["id"] for d in get_json(
                "http://127.0.0.1:%d/api/devices" % port).get("devices", [])]
            if r13_dev in live:
                break
            time.sleep(0.25)
        else:
            # 没有这个 else 的话，板子没起来时这里会**静默通过**，
            # 失败拖到后面变成"命令没走到 timeout — 实际: None"这种看不懂的报错。
            # 一个明确的"板子没上报"比五条莫名其妙的断言有用得多（手册 T59）。
            check("测试用的板子已上报（%s 在册）" % r13_dev, False,
                  "实际在册: %s" % live)
        r2 = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "capture_once", "device": r13_dev})
        cid2 = r2.get("id")
        rec2, st2 = None, None
        q13 = "?device=" + urllib.parse.quote(r13_dev, safe="")
        deadline = time.time() + 14
        while time.time() < deadline:
            cmds = get_json("http://127.0.0.1:%d/api/commands%s" % (port, q13)).get("commands", [])
            rec2 = next((c for c in cmds if c["id"] == cid2), None)
            st2 = rec2 and rec2["state"]
            if st2 == "timeout":
                break
            time.sleep(0.4)
        # **测完立刻停掉它**：这块板子故意不执行命令，而它还在上报 ——
        # 下一节如果不带设备名，命令就会被发给它，然后永远等不到 done
        # （2026-09-23 就是这么把下一节测挂的）。
        try:
            deaf.kill()
        except OSError:
            pass
        deaf.wait(timeout=25)
        check("板子不执行时被判超时", st2 == "timeout", "实际: %s" % st2)
        check("超时事件进了事件流",
              any("超时" in (e.get("text") or "") for e in events_of(port)))

        # ---- 15. 远端物理反馈指令（第 3 周）--------------------------------
        print("\n[14] 物理反馈指令 led_blink / led_set")
        r14_dev = "灯板-01"        # 同上：固定设备名，别依赖"最近上报的那台"
        board2 = subprocess.Popen(
            [PY, os.path.join(HERE, "fake_board.py"),
             "--url", "http://127.0.0.1:%d" % port, "--device", r14_dev,
             "--scenario", "idle", "--seconds", "20", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=CHILD_ENV)
        for _ in range(BOARD_WAIT_STEPS):
            live = [d["id"] for d in get_json(
                "http://127.0.0.1:%d/api/devices" % port).get("devices", [])]
            if r14_dev in live:
                break
            time.sleep(0.25)
        else:
            check("测试用的板子已上报（%s 在册）" % r14_dev, False,
                  "实际在册: %s" % live)

        r3 = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_blink", "params": {"n": 3, "on_ms": 80, "off_ms": 80},
                        "device": r14_dev})
        rec3 = _wait_cmd(port, r3.get("id"), 14, device=r14_dev)
        check("led_blink 走到 done", rec3 is not None and rec3["state"] == "done",
              "实际: %s" % (rec3 and rec3["state"]))
        check("led_blink 参数被保留", (rec3 or {}).get("params", {}).get("n") == 3,
              "实际: %s" % (rec3 or {}).get("params"))
        check("led_blink 不回传测量值（只有 ok/ms）",
              "x" not in ((rec3 or {}).get("result") or {}),
              "实际: %s" % ((rec3 or {}).get("result")))

        r4 = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_set", "params": {"on": True}})
        rec4 = _wait_cmd(port, r4.get("id"), 14)
        check("led_set 走到 done", rec4 is not None and rec4["state"] == "done",
              "实际: %s" % (rec4 and rec4["state"]))
        check("led_set 的 on 参数被保留", (rec4 or {}).get("params", {}).get("on") is True,
              "实际: %s" % (rec4 or {}).get("params"))

        # 越界参数必须被夹住（外部输入不信任）
        r5 = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_blink",
                        "params": {"n": 999, "on_ms": 1, "off_ms": 999999}})
        rec5 = _wait_cmd(port, r5.get("id"), 14)
        p5 = (rec5 or {}).get("params", {})
        check("越界参数被夹到安全范围",
              p5.get("n") == 12 and p5.get("on_ms") == 20 and p5.get("off_ms") == 5000,
              "实际: %s" % p5)

        # pattern 参数：认的值保留，不认的静默丢掉（不报错、不下发脏数据）
        r6 = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_blink", "params": {"pattern": "alert"}})
        rec6 = _wait_cmd(port, r6.get("id"), 14)
        check("合法 pattern=alert 被保留",
              (rec6 or {}).get("params", {}).get("pattern") == "alert",
              "实际: %s" % (rec6 or {}).get("params"))
        r7 = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_blink", "params": {"pattern": "rm -rf /"}})
        rec7 = _wait_cmd(port, r7.get("id"), 14)
        check("非法 pattern 被静默丢掉",
              "pattern" not in (rec7 or {}).get("params", {}),
              "实际: %s" % (rec7 or {}).get("params"))

        board2.wait(timeout=30)

        # ---- 16. 跌落 → 远端物理告警闭环（第 3 周）--------------------------
        print("\n[15] 跌落自动触发 LED 物理告警")
        t_mark = time.time()
        # 连灌几帧失重数据（|a|≈0.02g）。dt 由"批大小/到达间隔"自校准，
        # 所以要多帧快速连发，采样窗口里才会真的有连续失重样本。
        for _ in range(6):
            post_json("http://127.0.0.1:%d/api/telemetry" % port,
                      {"batch": [[0.02, -0.01, 0.01]] * 20,
                       "x": 0.02, "y": -0.01, "z": 0.01, "source": "SC7A20"})
            time.sleep(0.03)
        time.sleep(0.5)

        cmds = get_json("http://127.0.0.1:%d/api/commands" % port).get("commands", [])
        auto = [c for c in cmds if c["name"] == "led_blink" and c["created"] >= t_mark]
        check("判定跌落后自动排队了 led_blink", len(auto) > 0,
              "实际: 新增 %d 条" % len(auto))
        if auto:
            check("自动告警带 alert 语义图案",
                  auto[0].get("params", {}).get("pattern") == "alert",
                  "实际: %s" % auto[0].get("params"))
        check("自动告警进了事件流",
              any("跌落" in (e.get("text") or "") and "LED 告警" in (e.get("text") or "")
                  for e in events_of(port)))

        # ---- 17. 命令历史有上限（放最后，因为它会堆一队列指令）--------------
        print("\n[16] 命令历史不无限增长")
        for _ in range(25):
            post_json("http://127.0.0.1:%d/api/command" % port, {"name": "capture_once"})
        n = len(get_json("http://127.0.0.1:%d/api/commands" % port).get("commands", []))
        check("命令条数被限制在 20 条以内", n <= 20, "实际: %d 条" % n)

        # ---- 摄像头：推帧 → 取帧 → 拍照留档 → 删除 ---------------------------
        # 放在多设备那节**之前**：多设备节会改"最近上报过的设备"，
        # 而前面若干节靠那个回退语义工作（那节自己的注释也写明了要放最后）。
        print("\n[18] 摄像头：推帧 / 取帧 / 拍照留档 / 删除 / 安全")
        camdev = "cam-01"
        # 造一个**最小但合法**的 JPEG（头 FF D8、尾 FF D9，中间随便填）——
        # 服务端只校验魔数，不解析图像，所以不必真造一张图
        frame = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * 600 + bytes([0xFF, 0xD9])
        frame2 = bytes([0xFF, 0xD8, 0xFF, 0xE1]) + b"\x01" * 900 + bytes([0xFF, 0xD9])

        def cam_post(path, body, ctype="image/jpeg"):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                         data=body, method="POST")
            req.add_header("Content-Type", ctype)
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()      # 4xx 也是有效结果（下面要断言它）

        def cam_get(path):
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d%s" % (port, path), timeout=8) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        st, body = cam_post("/api/frame?device=" + camdev, frame)
        check("推一帧返回 200", st == 200, "实际: %s" % st)
        st, got = cam_get("/api/frame?device=" + camdev)
        check("取回的帧与推上去的**逐字节一致**", st == 200 and got == frame,
              "状态 %s / %d 字节" % (st, len(got)))

        st, body = cam_post("/api/frame?device=" + camdev + "&save=1", frame2)
        saved = json.loads(body.decode()).get("saved") if st == 200 else None
        check("带 save=1 会留档", st == 200 and bool(saved), "实际: %s / %s" % (st, saved))

        shots = get_json("http://127.0.0.1:%d/api/shots?device=%s" % (port, camdev))
        check("照片列表里有它", len(shots.get("shots", [])) >= 1,
              "实际: %d 张" % len(shots.get("shots", [])))
        if shots.get("shots"):
            st, back = cam_get("/api/shots/%s/%s" % (camdev, shots["shots"][0]["name"]))
            check("取回的照片内容一致", st == 200 and back == frame2,
                  "状态 %s / %d 字节" % (st, len(back)))

        # 安全：非 JPEG 拒收；路径穿越读不到东西
        st, _ = cam_post("/api/frame?device=" + camdev, b"NOTJPEG!!")
        check("非 JPEG 的帧被拒（400）", st == 400, "实际: %s" % st)
        st, _ = cam_get("/api/shots/" + camdev + "/..%2F..%2Fserver.py")
        check("照片路径穿越被拒（404）", st == 404, "实际: %s" % st)

        # 删除
        if shots.get("shots"):
            st, _ = cam_post("/api/shots/delete",
                             json.dumps({"device": camdev,
                                         "name": shots["shots"][0]["name"]}).encode(),
                             "application/json")
            check("删除照片返回 200", st == 200, "实际: %s" % st)
            left = get_json("http://127.0.0.1:%d/api/shots?device=%s" % (port, camdev))
            check("删完列表里没有了", len(left.get("shots", [])) == len(shots["shots"]) - 1,
                  "实际: %d 张" % len(left.get("shots", [])))

        # ---- 18. 多设备（PROPOSAL §4.1 / 优先级 ③）---------------------------
        # 这一节必须放在最后：它往服务端注册了新设备，会改变"最近上报过的设备"
        # 是哪一台，而前面所有小节都是靠那个回退语义（不带 ?device=）工作的。
        print("\n[17] 多设备：按 device 分片 + 定向下发 + 名字清洗")
        dev_a = "第三组-07"          # 中文名（课堂里学生多半这么起名）
        dev_b = "rw1 A1B2"           # 带空格，顺带验证空白压缩
        quote_ = lambda s: urllib.parse.quote(s, safe="")

        def api(path, dev=None):
            url = "http://127.0.0.1:%d%s" % (port, path)
            if dev is not None:
                url += ("&" if "?" in path else "?") + "device=" + quote_(dev)
            return get_json(url)

        # A 向右倾斜、B 向上倾斜（屏幕坐标系 +y 向下，所以 y<0 是"上"）
        for _ in range(6):
            post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                     firmware_body([(0.72, 0.0, 0.70)] * 50, device=dev_a))
            post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                     firmware_body([(0.0, -0.99, 0.10)] * 50, device="  " + dev_b + "  "))
            time.sleep(0.03)

        a = api("/api/latest", dev_a)
        b = api("/api/latest", dev_b)
        check("A 台的活动独立（向右倾斜）", a["activity"] == "静置·向右倾斜",
              "实际: %s" % a["activity"])
        check("B 台的活动独立（向上倾斜）", b["activity"] == "静置·向上倾斜",
              "实际: %s" % b["activity"])
        check("设备名首尾空白被压缩", b.get("device") == dev_b, "实际: %r" % b.get("device"))

        devs = api("/api/devices").get("devices", [])
        ids = [d["id"] for d in devs]
        check("设备列表里两台都在", dev_a in ids and dev_b in ids, "实际: %s" % ids)
        check("设备列表带在线状态与最后上报时间",
              bool(devs) and all(("online" in d and "age" in d and "posts_ok" in d)
                                 for d in devs), "实际: %s" % (devs[:1],))
        # 正确的判据不是"'-' 不在列表里"——前面各节不带 device 的帧本来就会落到
        # 默认设备 '-' 上，它是**真的上报过**的。要钉的是"从没上报过的占位设备
        # 不该被列出来"（否则单板场景下页面会多出一个空的 '- ' 条目）。
        check("设备列表里每一项都真的上报过（无空占位）",
              bool(devs) and all(d["posts_ok"] > 0 for d in devs),
              "实际: %s" % [(d["id"], d["posts_ok"]) for d in devs])

        # 定向下发：只发给 A，B 的队列必须干净
        ra = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_blink", "device": dev_a})
        check("下发响应回带目标设备", ra.get("device") == dev_a,
              "实际: %s" % ra.get("device"))
        ca = api("/api/commands", dev_a).get("commands", [])
        cb = api("/api/commands", dev_b).get("commands", [])
        check("命令只进了 A 的队列",
              any(c["id"] == ra["id"] for c in ca) and not any(c["id"] == ra["id"] for c in cb),
              "A=%d 条 / B=%d 条" % (len(ca), len(cb)))

        # 未知设备名 → 回退到"最近上报过的那台"，而不是 404 让页面白屏
        fb = api("/api/latest", "这个设备不存在")
        check("未知设备名回退到最近上报的设备", fb.get("device") in (dev_a, dev_b),
              "实际: %s" % fb.get("device"))

        # 设备名是**用户输入**（配网页手填），必须能安全穿过手写 JSON：
        # 引号/反斜杠由固件端 json_escape_append() 转义，服务端要能原样还原。
        weird = 'a"b\\c'
        post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                 firmware_body([(0.0, 0.0, 1.0)] * 20, device=weird))
        ids2 = [d["id"] for d in api("/api/devices")["devices"]]
        check("带引号/反斜杠的设备名能安全往返", weird in ids2, "实际: %s" % ids2)

        # 控制字符必须剥掉：\n 会把 SSE 的"一行一个 data:"分帧打断
        post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                 firmware_body([(0.0, 0.0, 1.0)] * 20, device="bad\tname\nx"))
        ids3 = [d["id"] for d in api("/api/devices")["devices"]]
        check("设备名里的控制字符被剥掉", "badnamex" in ids3, "实际: %s" % ids3)

        # 过长的设备名要截断（否则畸形负载能把内存顶爆）
        post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                 firmware_body([(0.0, 0.0, 1.0)] * 20, device="y" * 40))
        ids4 = [d["id"] for d in api("/api/devices")["devices"]]
        check("过长的设备名被截到 32 字符", ("y" * 32 in ids4) and ("y" * 33 not in ids4),
              "实际: %s" % [i for i in ids4 if i.startswith("y")])

        # SSE 也要按设备过滤，并带上设备列表（前端靠它画设备卡片）
        try:
            with OPENER.open("http://127.0.0.1:%d/api/stream?device=%s"
                             % (port, quote_(dev_a)), timeout=6) as r:
                line = r.readline().decode("utf-8", "replace")
            sse_ok = line.startswith("data: ") and dev_b in line
        except Exception as exc:  # noqa: BLE001
            line, sse_ok = "读取失败: %s" % exc, False
        check("SSE 首帧带设备列表", sse_ok, "实际: %s" % line[:90])

        # 旧固件（不带 device 字段）仍然落在默认设备上，不报错
        post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                 firmware_body([(0.0, 0.0, 1.0)] * 20).replace('"device":"rw1-AB12",', ""))
        check("去掉 device 字段的旧固件仍被接受（归到默认设备）",
              api("/api/latest").get("device") == "-",
              "实际: %s" % api("/api/latest").get("device"))

        # ---- 19. 方向档位 oN：板端上报 + 网页点选改档 ----------------------
        # 真机标定方向时用户得靠长按 BOOT 盲按 N 次，就是因为网页看不到档位。
        # 现在板端每帧上报 o，网页显示并可直接点选改。
        print("\n[18] 方向档位 oN 上报与远程设置")
        for _ in range(3):
            post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                     firmware_body([(0.0, 0.0, 1.0)] * 50, device=dev_a,
                                   orient=7))
            time.sleep(0.03)
        got = [d for d in api("/api/devices")["devices"] if d["id"] == dev_a]
        check("设备列表里带上了板端上报的 oN",
              bool(got) and got[0].get("orient") == 7,
              "实际: %s" % (got[0].get("orient") if got else None))

        # 用**从没报过 o 的全新设备**验"老固件"这条路 —— 注意不能拿已经报过 7 的
        # dev_a 来验：后面不带 o 的帧**不该清掉已知值**（板子最后报的档位仍然有效），
        # 那是正确行为，一开始我按"会变 None"写，反而测错了。
        old_dev = "老固件-01"
        post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                 firmware_body([(0.0, 0.0, 1.0)] * 20, device=old_dev))
        got = [d for d in api("/api/devices")["devices"] if d["id"] == old_dev]
        check("老固件（从没报过 o）→ orient 为 None，且不报错",
              bool(got) and got[0].get("orient") is None,
              "实际: %s" % (got[0].get("orient") if got else None))

        # 越界的 o 不该被采纳（畸形载荷别把网页搞乱）
        bad_dev = "越界-01"
        post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                 firmware_body([(0.0, 0.0, 1.0)] * 20, device=bad_dev, orient=99))
        got = [d for d in api("/api/devices")["devices"] if d["id"] == bad_dev]
        check("越界的 o 被忽略（不采纳、也不报错）",
              bool(got) and got[0].get("orient") is None,
              "实际: %s" % (got[0].get("orient") if got else None))

        # 缺字段**不该清掉**已知值：板子最后报的档位仍然有效
        post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                 firmware_body([(0.0, 0.0, 1.0)] * 20, device=dev_a))
        got = [d for d in api("/api/devices")["devices"] if d["id"] == dev_a]
        check("后续帧不带 o 时保留上一次的档位",
              bool(got) and got[0].get("orient") == 7,
              "实际: %s" % (got[0].get("orient") if got else None))

        r = post_json("http://127.0.0.1:%d/api/command" % port,
                      {"name": "set_orient", "params": {"o": 3}, "device": dev_a})
        check("set_orient 在指令白名单里（不再 400）",
              r.get("ok") is True and r.get("device") == dev_a,
              "实际: %s" % r)
        # ⚠️ 这条是"漏测"补的：sanitize_params 原来对 set_orient 返回 {}，
        # 于是网页选好档位、请求体里也带着 o，**服务端却把它丢了**，
        # 板子只会收到"改成 o0"。只验请求体是发现不了的，必须验队列里存的。
        cmds_o = api("/api/commands", dev_a).get("commands", [])
        rec_o = next((c for c in cmds_o if c["id"] == r.get("id")), None)
        check("set_orient 的档位参数真的进了队列（不被 sanitize 丢掉）",
              bool(rec_o) and rec_o.get("params", {}).get("o") == 3,
              "实际: %s" % (rec_o.get("params") if rec_o else None))

        # set_config：网页"板子设置"卡片走的就是它（WiFi / 服务器地址 / 上报周期）
        rc = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "set_config",
                        "params": {"ssid": "新WiFi", "pass": "pw123456",
                                   "url": "http://10.0.0.5:8000", "period_ms": 200},
                        "device": dev_a})
        check("set_config 在指令白名单里",
              rc.get("ok") is True and rc.get("device") == dev_a,
              "实际: %s" % rc)
        cmds_c = api("/api/commands", dev_a).get("commands", [])
        rec_c = next((c for c in cmds_c if c["id"] == rc.get("id")), None)
        check("set_config 的参数原样进队列（ssid/url/period_ms 都在）",
              bool(rec_c) and rec_c.get("params", {}).get("ssid") == "新WiFi"
              and rec_c.get("params", {}).get("period_ms") == 200
              and rec_c.get("params", {}).get("url") == "http://10.0.0.5:8000",
              "实际: %s" % (rec_c.get("params") if rec_c else None))

        # ---- 20. 多选批量下发 ---------------------------------------------
        print("\n[19] 多选/全选批量下发")
        for _ in range(4):
            post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                     firmware_body([(0.0, 0.0, 1.0)] * 50, device=dev_a))
            post_raw("http://127.0.0.1:%d/api/telemetry" % port,
                     firmware_body([(0.0, 0.0, 1.0)] * 50, device=dev_b))
            time.sleep(0.03)

        rb = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_blink", "params": {"n": 2},
                        "devices": [dev_a, dev_b]})
        check("批量下发返回 batch 与每台的结果",
              rb.get("ok") is True and rb.get("batch") is True
              and rb.get("count") == 2 and len(rb.get("results", [])) == 2,
              "实际: %s" % rb)
        targets = sorted(x["device"] for x in rb.get("results", []))
        check("两台各收到一条命令", targets == sorted([dev_a, dev_b]),
              "实际: %s" % targets)
        ca = api("/api/commands", dev_a).get("commands", [])
        cb = api("/api/commands", dev_b).get("commands", [])
        ids_b = [x["id"] for x in rb.get("results", [])]
        check("命令确实分别进了两台各自的队列",
              all(any(c["id"] == i for c in ca) for i in ids_b[:1])
              and all(any(c["id"] == i for c in cb) for i in ids_b[1:]),
              "A=%s B=%s" % ([c["id"] for c in ca][:2], [c["id"] for c in cb][:2]))

        # 列表里有重复项时要去重：同一台收到两条一模一样的命令是纯噪音
        rd = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_set", "params": {"on": True},
                        "devices": [dev_a, dev_a, dev_a]})
        check("重复的设备名被去重（只下发一次）",
              rd.get("batch") is not True and rd.get("device") == dev_a,
              "实际: %s" % rd)

        # 单台时的响应格式必须**一字未改**，否则老页面会坏
        rs = post_json("http://127.0.0.1:%d/api/command" % port,
                       {"name": "led_blink", "device": dev_a})
        check("单台的响应格式与以前一致（ok/id/device/device_online）",
              set(rs.keys()) >= {"ok", "id", "device", "device_online"}
              and "batch" not in rs,
              "实际 keys: %s" % sorted(rs.keys()))

        # ---- 22. 语音链路（第 4 周）----------------------------------------
        print("\n[20] 语音：音频 → 识别 → 大模型")
        vdev = "voice-01"
        vurl = "http://127.0.0.1:%d/api/audio?device=%s" % (port, vdev)

        # 边界：空 / 过短 / 超长都要被拒。**关键是客户端能读到 400**，
        # 而不是拿到连接被重置（服务端不读 body 就回绝时会发生，见手册 T58）。
        for payload, label in ((b"", "空载荷"), (b"\x00" * 32, "过短"),
                               (b"\x00" * (600 * 1024), "超长")):
            try:
                post_bytes(vurl, payload)
                check("音频%s被拒" % label, False, "竟然接受了")
            except urllib.error.HTTPError as e:
                check("音频%s被拒（400 可读）" % label, e.code == 400, "HTTP %d" % e.code)
            except Exception as e:  # noqa: BLE001
                check("音频%s被拒" % label, False, "%s: %s" % (type(e).__name__, e))

        out = post_bytes(vurl, b"\x01\x02" * 16000)     # 1 秒假 PCM
        check("音频被接收", out.get("ok") is True and out.get("bytes") == 32000,
              "实际: %s" % out)

        deadline = time.time() + MockLLM.delay + 12
        snap = {}
        while time.time() < deadline:
            snap = latest(port, vdev)
            if "慢速假大模型" in snap.get("ai_reply", ""):
                break
            time.sleep(0.5)
        check("识别出的文字落进了状态", snap.get("voice_text") == MockLLM.asr_text,
              "实际: %r" % snap.get("voice_text"))
        check("识别结果被当成提问交给了大模型",
              "慢速假大模型" in snap.get("ai_reply", ""),
              "实际: %r" % (snap.get("ai_reply") or "")[:40])
        check("语音状态回到 idle", snap.get("voice_state") == "idle",
              "实际: %s" % snap.get("voice_state"))
        check("没有残留错误", not snap.get("voice_err"), "实际: %r" % snap.get("voice_err"))

        # 计时（第 6 周）：课程要求语音任务能"计时"。板端屏幕只有回复正文，
        # 看不出"是识别慢还是模型慢"，所以三段耗时都要有数、而且要合理。
        check("语音任务有识别耗时（voice_ms > 0）", (snap.get("voice_ms") or 0) > 0,
              "实际: %s" % snap.get("voice_ms"))
        check("语音任务有生成耗时（ai_ms ≈ 假模型 %.0f 秒）" % MockLLM.delay,
              (snap.get("ai_ms") or 0) >= MockLLM.delay * 1000 * 0.5,
              "实际: %s ms" % snap.get("ai_ms"))
        check("语音任务有端到端耗时，且不小于生成那一段（total_ms ≥ ai_ms > 0）",
              0 < (snap.get("ai_ms") or 0) <= (snap.get("total_ms") or 0),
              "实际: total=%s ai=%s" % (snap.get("total_ms"), snap.get("ai_ms")))

        # 澄清：第二次提问应该把上一轮问答带进大模型请求
        before = len(MockLLM.seen)
        post_bytes(vurl, b"\x01\x02" * 16000)
        deadline = time.time() + MockLLM.delay + 12
        while time.time() < deadline and len(MockLLM.seen) <= before:
            time.sleep(0.5)
        roles = [m.get("role") for m in (MockLLM.seen[-1].get("messages") or [])]
        check("追问带上了上一轮问答（澄清）",
              roles.count("assistant") >= 1 and roles.count("user") >= 2, str(roles))

        # 停止
        rc = post_json("http://127.0.0.1:%d/api/ask/cancel?device=%s" % (port, vdev), {})
        check("取消接口可用（停止）", rc.get("ok") is True, "实际: %s" % rc)

        # ---- 24. 自然语言请求（第 4 周）-------------------------------------
        print("\n[21] 自然语言请求：说话 → 指令（第 4 周）")
        nl_dev = "nl-01"
        nbase = "http://127.0.0.1:%d" % port
        # **必须真的起一块板子**：自然语言下发的指令得有人执行，"结果回显到屏幕"
        # 那一条才有东西可断言。不跑板子的话指令只会停在 queued 直到超时。
        nlboard = subprocess.Popen(
            [PY, os.path.join(HERE, "fake_board.py"),
             "--url", nbase, "--device", nl_dev, "--scenario", "idle",
             "--period", "0.25", "--seconds", "60", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", env=CHILD_ENV)
        for _ in range(BOARD_WAIT_STEPS):
            live = [d["id"] for d in get_json(nbase + "/api/devices").get("devices", [])]
            if nl_dev in live:
                break
            time.sleep(0.25)
        else:
            check("测试用的板子已上报（%s 在册）" % nl_dev, False,
                  "实际在册: %s" % live)

        def ask_nl(text):
            """把一句话当成"板端提问"发上去 —— 板端就是这么发的（ask=1 + q=文字）。"""
            return post_raw(nbase + "/api/telemetry",
                            firmware_body([(0.0, 0.0, 1.0)] * 5, ask=True,
                                          question=text, device=nl_dev))

        def cmds_of():
            return get_json(nbase + "/api/commands?device=" + urllib.parse.quote(nl_dev)) \
                   .get("commands", [])

        names_ok = set(get_json(nbase + "/api/commands").get("names", []))
        # 每句话 → 期望翻成的指令与参数。**故意用不同说法**：学生不会照着按钮文案念。
        nl_cases = (
            ("拍张照",          "cam_capture",  {}),
            ("采集一次",        "capture_once", {}),
            ("开一下实时画面",   "cam_stream",   {"on": True}),
            ("把实时画面关掉",   "cam_stream",   {"on": False}),
            ("卡里有什么文件",   "sd_ls",        {}),
            ("闪五下",          "led_blink",    {"n": 5}),
            ("把灯关掉",        "led_set",      {"on": False}),
            ("切到第3档",       "set_orient",   {"o": 3}),
        )
        misses, spoken_ok = [], True
        for text, want, params in nl_cases:
            prev = (cmds_of() or [{}])[0].get("id")
            out = ask_nl(text)
            time.sleep(0.3)
            rec = (cmds_of() or [{}])[0]
            if (rec.get("name") != want or rec.get("id") == prev
                    or want not in names_ok):
                misses.append("%s→%s" % (text, rec.get("name")))
                if rec.get("id"):
                    _wait_cmd(port, rec["id"], 8, device=nl_dev)
                continue
            for k, v in params.items():
                if (rec.get("params") or {}).get(k) != v:
                    misses.append("%s 参数 %s=%r" % (text, k, v))
            # 回话要短、口语化（"好，让板子拍一张照…"），不是大模型那种长回答
            if not (out.get("reply") or "").startswith("好"):
                spoken_ok = False
                misses.append("%s 回话=%r" % (text, out.get("reply")))
            # **等这条真的走完再发下一条**：板子一次只执行一条，队列堆起来后
            # 后面的会撞上 5 秒超时（queued 以 created 计时），回显被"超时"覆盖 ——
            # 那不是产品缺陷，是测试自己在制造拥塞。
            _wait_cmd(port, rec["id"], 8, device=nl_dev)
        check("8 句自然语言都翻成了对应指令（名字 + 参数）", not misses,
              "问题: %s" % misses)
        check("板端立刻收到'好的…'而不是去等大模型", spoken_ok)
        check("自然语言下发的指令标了 source=nl（网页能看出是语音来的）",
              all(c.get("source") == "nl" for c in cmds_of()[:4]),
              "实际: %s" % [c.get("source") for c in cmds_of()[:4]])

        # 课程第 4 周的**当堂验证项**：同一份旧数据，「查看上次数据」和「重新采集」
        # 必须走不同路径 —— 前者不产生新指令（走查询、保留旧时间），后者要等新结果。
        prev_id = (cmds_of() or [{}])[0].get("id")
        ask_nl("上次采集的数据是多少")
        time.sleep(0.4)
        check("「查看上次数据」不被当成新采集请求（没有新指令产生）",
              (cmds_of() or [{}])[0].get("id") == prev_id,
              "实际: 最新指令 %s → %s" % (prev_id, (cmds_of() or [{}])[0].get("id")))
        ask_nl("重新采集一次")
        time.sleep(0.4)
        rec_fresh = (cmds_of() or [{}])[0]
        check("「重新采集」确实产生一条新的采集指令",
              rec_fresh.get("name") == "capture_once" and rec_fresh.get("source") == "nl"
              and rec_fresh.get("id") != prev_id,
              "实际: %s / %s" % (rec_fresh.get("name"), rec_fresh.get("source")))

        # 危险动作：**必须**只回话、不下发 —— 语音识别错一个字就可能把卡格了
        n_before = len(cmds_of())
        out = ask_nl("格式化存储卡")
        time.sleep(0.4)
        check("危险动作（格式化）被拒：不下发指令，只回一句指路",
              len(cmds_of()) == n_before and "危险" in (out.get("reply") or ""),
              "实际: %r / 指令数 %d→%d" % (out.get("reply"), n_before, len(cmds_of())))

        # 普通问题不能被意图规则"抢"走 —— 该问大模型的还是问大模型。
        # **放最后**：它会拉起一次 6 秒的假大模型生成，放前面会干扰后面的回显断言。
        before_calls = len(MockLLM.seen)
        out = ask_nl("今天天气怎么样")
        time.sleep(0.5)
        check("普通问题不被误判成设备请求（仍走大模型）",
              len(MockLLM.seen) > before_calls or "天气" in (out.get("reply") or ""),
              "实际: %r" % out.get("reply"))

        # 执行完 → 结果写回板端屏幕（用户是对着板子说的，眼睛也在板子上）
        ask_nl("采集一次")
        time.sleep(0.4)
        cid_nl = (cmds_of() or [{}])[0].get("id")
        done_nl = _wait_cmd(port, cid_nl, 15, device=nl_dev)
        time.sleep(0.8)                     # 等回显落到 ai_reply
        snap_nl = latest(port, nl_dev)
        check("指令执行完，结果回显到板端屏幕",
              done_nl is not None and done_nl["state"] == "done"
              and "采好了" in (snap_nl.get("ai_reply") or ""),
              "实际: %s / %r" % (done_nl and done_nl["state"], snap_nl.get("ai_reply")))
        check("自然语言请求进了事件流（说了什么 → 什么指令）",
              any("自然语言请求" in (e.get("text") or "")
                  for e in events_of(port, nl_dev)))

        # 新指令必须**作废**还在后台跑的那次生成 —— 否则几秒后旧回答会盖掉指令回显，
        # 用户看到的是"刚才那个问题的答案"，而他明明已经改口了。
        ask_nl("闪三下")
        time.sleep(0.4)
        cid_cancel = (cmds_of() or [{}])[0].get("id")
        _wait_cmd(port, cid_cancel, 8, device=nl_dev)
        time.sleep(MockLLM.delay + 2)       # 等那次被作废的生成真的回来
        check("新指令作废了上一轮还在生成的大模型回答（回显不被旧回答盖掉）",
              "闪好了" in (latest(port, nl_dev).get("ai_reply") or ""),
              "实际: %r" % (latest(port, nl_dev).get("ai_reply")))

        # ---- 25. 文字入口（第 5 周）-----------------------------------------
        # 课程第 5 周要求「用同一句任务对照文字和语音入口」。对照的前提是两条入口
        # 真的走同一套处理 —— 所以这里断言：同一句话，打字和说话**结果一致**。
        print("\n[22] 文字入口：打字下达任务（第 5 周）")
        q = "?device=" + urllib.parse.quote(nl_dev)
        out = post_json(nbase + "/api/ask" + q, {"text": "闪三下"})
        check("文字入口可用，且立刻回一句", out.get("ok") is True and bool(out.get("reply")),
              "实际: %s" % out)
        time.sleep(0.4)
        rec_txt = (cmds_of() or [{}])[0]
        check("打字和说话解析成同一条指令（同一套 request_ai）",
              rec_txt.get("name") == "led_blink" and rec_txt.get("source") == "nl",
              "实际: %s / %s" % (rec_txt.get("name"), rec_txt.get("source")))
        check("空文字被拒（400）", _expect_400(nbase + "/api/ask", {"text": "   "}))

        # ---- 26. 迟到结果隔离（第 6 周）-------------------------------------
        # 课程第 6 周：「让语音任务可以澄清与停止（含停止播报、**迟到结果隔离**、计时）」。
        #
        # 典型场景：用户说「拍张照」，板子还在拍，他已经改口了（按了停止）。
        # 那条指令的结果回来时属于**上一轮**，不能再往屏幕上写 ——
        # 否则用户会看到一个自己已经不要了的旧结果，把当前的回答盖掉。
        print("\n[23] 迟到结果隔离：旧结果不许盖掉新回答（第 6 周）")
        # 专门起一块**故意不执行指令**的板子（--no-cmd）：指令会一直停在队列里，
        # 直到被判超时 —— 那条"超时"就是我们要的**迟到结果**。
        # 这样不用"抢在板子取走命令之前取消"（那种窗口只有几毫秒，是在赌运气），
        # 场景是确定性的：无论机器快慢，指令都不会提前完成。
        slow_dev = "nl-slow"
        slowboard = subprocess.Popen(
            [PY, os.path.join(HERE, "fake_board.py"),
             "--url", nbase, "--device", slow_dev, "--scenario", "idle",
             "--period", "0.25", "--seconds", "60", "--no-cmd", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=CHILD_ENV)
        for _ in range(BOARD_WAIT_STEPS):
            live = [d["id"] for d in get_json(nbase + "/api/devices").get("devices", [])]
            if slow_dev in live:
                break
            time.sleep(0.25)
        else:
            check("测试用的板子已上报（%s 在册）" % slow_dev, False,
                  "实际在册: %s" % live)

        def ask_on(dev_id, text):
            return post_raw(nbase + "/api/telemetry",
                            firmware_body([(0.0, 0.0, 1.0)] * 5, ask=True,
                                          question=text, device=dev_id))

        def cmds_on(dev_id):
            return get_json(nbase + "/api/commands?device=" + urllib.parse.quote(dev_id)) \
                   .get("commands", [])

        ask_on(slow_dev, "拍张照")
        time.sleep(0.4)
        cid_late = (cmds_on(slow_dev) or [{}])[0].get("id")
        # 用户改口了：按停止。此刻这条指令**还没回话**（板子故意不执行），
        # 所以"停止"必须把它也认成有事可停。
        rc_stop = post_json(nbase + "/api/ask/cancel?device=" + urllib.parse.quote(slow_dev), {})
        check("停止对「还没回话的自然语言指令」也生效（不是只停大模型）",
              rc_stop.get("ok") is True and rc_stop.get("stopped") is True,
              "实际: %s" % rc_stop)
        check("按下停止时屏幕立刻变成「已停止」",
              (latest(port, slow_dev).get("ai_reply") or "").strip() == "已停止",
              "实际: %r" % latest(port, slow_dev).get("ai_reply"))

        # 板子终于把结果回传了 —— 可这一轮早就结束了。这条走的是**主路径**
        # （apply_command_result），不是超时那条。body 用 firmware_body 再拼上
        # result 字段，跟真板子发回来的形状一致。
        raw_body = firmware_body([(0.0, 0.0, 1.0)] * 5, device=slow_dev)
        assert raw_body.endswith("}"), raw_body[-40:]
        post_raw(nbase + "/api/telemetry",
                 raw_body[:-1] + ',"result":{"id":"%s","ok":true,"note":"拍好了"}}' % cid_late)
        time.sleep(0.8)
        rec_late = _wait_cmd(port, cid_late, 8, device=slow_dev)
        reply_late = (latest(port, slow_dev).get("ai_reply") or "").strip()
        check("那条指令确实执行完了（迟到的结果真的产生了）",
              rec_late is not None and rec_late["state"] == "done",
              "实际: %s" % (rec_late and rec_late["state"]))
        check("迟到的结果被丢弃，屏幕仍停在「已停止」",
              reply_late == "已停止", "实际: %r" % reply_late)
        check("丢弃这件事在事件流里留了痕（不是静默吞掉）",
              any("来晚了" in (e.get("text") or "")
                  for e in events_of(port, slow_dev)),
              "实际: %s" % [e.get("text") for e in events_of(port, slow_dev)[:3]])
        try:
            slowboard.kill()
            slowboard.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

        # ---- 27. 迟到的语音识别同样要丢弃（第 6 周）-------------------------
        # 识别要几秒（云端更久）。这期间用户完全可能在网页上问了别的问题 ——
        # 那段识别结果属于上一轮，不能再拿它去调模型，否则会用一个旧问题顶掉新答案。
        print("\n[24] 迟到的语音识别被丢弃（第 6 周）")
        MockLLM.asr_delay = 2.5             # 把识别放慢，制造"识别期间用户改口"
        try:
            post_bytes(vurl, b"\x01\x02" * 16000)
            time.sleep(0.3)
            # 识别还在跑：这时在**同一台设备**上问一句 —— 回合令牌就变了
            post_json(nbase + "/api/ask?device=" + urllib.parse.quote(vdev),
                      {"text": "今天天气怎么样"})
            time.sleep(3.5)                 # 等那段迟到的识别回来
            snap_late_v = latest(port, vdev)
            check("迟到的识别结果被丢弃（没拿旧问题去调模型）",
                  "已忽略" in (snap_late_v.get("voice_err") or ""),
                  "实际: %r" % snap_late_v.get("voice_err"))
            check("丢弃后语音状态复位，板端不会卡在「识别中」",
                  snap_late_v.get("voice_state") == "idle",
                  "实际: %s" % snap_late_v.get("voice_state"))
        finally:
            MockLLM.asr_delay = 0.0

        try:
            nlboard.kill()
            nlboard.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

        # ---- 25. 访问密钥门（#51 遗留项二，2026-10-12）---------------------
        # 威胁模型：教室里任何连上同一 WiFi 的人。破坏性动作（sd_format）与
        # 花钱的动作（问大模型）**只能从网页来** —— 板子只会上报遥测/帧/音频，
        # 命令永远是服务端下发下去的。所以门网页写入口**不需要动固件**：
        # 这里起第二个实例（RW1_API_TOKEN 已设）逐条验，主实例（无密钥）
        # 的 128 条断言同时就是"默认关闭、行为不变"的证据。
        print("\n[25] 访问密钥门（配了 RW1_API_TOKEN 的第二个实例）")
        srv2 = subprocess.Popen([PY, "-u", SERVER_PY, "--port", str(port2),
                                 "--data-dir", data_dir + "2", "--retain-days", "0"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", env=token_env, cwd=ROOT)
        up = False
        for _ in range(50):
            try:
                get_json("http://127.0.0.1:%d/api/latest" % port2, timeout=1)
                up = True
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        # ⚠️ check() 的第三个参数在 PASS 时也会打印，所以别把"失败提示语"
        # 写在那里 —— 否则日志里一片 [PASS] 跟着"起不来（看日志）"，
        # 下一个值班的会被自己人吓到（2026-10-14 实测踩到过）。
        # 成功/失败各有各的真实观察值。
        check("带密钥的服务端起得来", up,
              ("实际: 端口 %d 应答正常" % port2) if up
              else "实际: %d 端口 %d 次探测都没应答，起不来（ srv2 日志见 --keep 保留的临时目录）" % (port2, 50))

        # 判别"这真的是一个新进程"：全新实例没有任何板子上报过，
        # 设备列表必然为空；若端口和主实例撞了，这里会看到主实例的一堆设备。
        # 没有这道判别，端口相撞会让下面三个 401 断言集体误报。
        fresh = False
        if up:
            try:
                fresh = not get_json("http://127.0.0.1:%d/api/devices" % port2,
                                     timeout=3).get("devices")
            except (urllib.error.URLError, OSError):
                fresh = False
        check("第二个实例是全新进程（端口没和主实例撞上）", fresh,
              ("实际: 端口 %d 上是全新实例，设备列表为空" % port2) if fresh
              else "实际: %d 端口上应答的服务端带着历史设备，端口撞了" % port2)

        def post_tok(path, payload, token=None, use_query=False):
            url = "http://127.0.0.1:%d%s" % (port2, path)
            if token and use_query:
                url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         # use_query 时**只**走查询参数，不发头 ——
                         # 否则头先通过，就证明不了查询参数这条路是通的
                         "X-RW1-Token": ("" if use_query else (token or ""))})
            try:
                with OPENER.open(req, timeout=6) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    return e.code, json.loads(raw)
                except ValueError:
                    return e.code, {"raw": raw[:120]}

        # 只有在"确实是带密钥的新实例"时，下面的 401 断言才有意义
        if up and fresh:
            st, body = post_tok("/api/command", {"name": "capture_once"})
            check("没带密钥 → 401", st == 401, "实际: %s %s" % (st, body))
            st, body = post_tok("/api/command", {"name": "capture_once"}, "wrong-key")
            check("带错密钥 → 401", st == 401, "实际: %s" % st)
            st, body = post_tok("/api/command", {"name": "capture_once"}, TOKEN)
            check("带对密钥 → 正常下发", st == 200 and body.get("ok"),
                  "实际: %s %s" % (st, body))
            st, body = post_tok("/api/command", {"name": "capture_once"}, TOKEN,
                                use_query=True)
            check("?token= 查询参数同样认（给不方便加头的客户端）",
                  st == 200 and body.get("ok"), "实际: %s" % st)
            st, body = post_tok("/api/ask", {"text": "你好"})
            check("提问入口同样要密钥（花钱的动作）", st == 401, "实际: %s" % st)
            # 板子上报**不要**密钥：老固件没有密钥可带，门了就是把全班挡在门外
            try:
                with OPENER.open(urllib.request.Request(
                        "http://127.0.0.1:%d/api/telemetry" % port2,
                        data=firmware_body([(0.0, 0.0, 1.0)] * 20,
                                           device="tok-01").encode("utf-8"),
                        headers={"Content-Type": "application/json"}), timeout=8) as r:
                    tcode = r.status
            except urllib.error.HTTPError as e:
                tcode = e.code
            check("板子上报不带密钥也收（后向兼容）", tcode == 200,
                  "实际: HTTP %s" % tcode)
        # 主实例（没配密钥）：行为必须与以前逐字节一样
        mbody = post_json("http://127.0.0.1:%d/api/command" % port,
                          {"name": "capture_once"})
        check("没配密钥的实例完全不设防（与历史行为一致）",
              mbody.get("ok") is True, "实际: %s" % mbody)

        # ---- 26. 外部数据健壮性（2026-10-30 加固）-------------------------
        # 几条都是"一条外部输入把服务端打出一个异常"的路径。它们都不可能来自
        # 本仓库的固件（固件有 sanitize3、有 clamp），但教室里任何一台机器都能发。
        #
        # 修之前的表现（实测）：`{"batch":[[NaN,NaN,NaN]]}` → analyze 里
        # `int(nan)` 抛 ValueError，而且抛在 _send() **之前** → 客户端收到的是
        # "连接被掐断"而不是 500；NaN 留在窗口里，接下来 8 秒内每一帧正常数据
        # 都会在同一个 int(nan) 上再抛一次；/api/latest 再把 NaN 序列化出去，
        # 浏览器 JSON.parse 抛错 → 整页停在旧数据上。
        print("\n[26] 畸形载荷不再打穿数据流（2026-10-30 加固）")
        pdev = "rob-01"
        tbase = "http://127.0.0.1:%d/api/telemetry?device=%s" % (
            port, urllib.parse.quote(pdev))

        def _reject_json_const(name):
            raise ValueError("非法 JSON 常量: %s" % name)

        nan_body = post_raw(
            tbase,
            '{"device":"%s","batch":[[NaN,NaN,NaN],[0,0,1],[0,0,1]]}' % pdev)
        check("含 NaN 的批次仍被接受（坏点丢弃、好点照常分析）",
              nan_body.get("ok") is True, "实际: %s" % nan_body)

        raw_latest = OPENER.open(
            "http://127.0.0.1:%d/api/latest?device=%s" % (port, pdev),
            timeout=6).read().decode("utf-8")
        try:
            json.loads(raw_latest, parse_constant=_reject_json_const)
            strict_ok = True
        except ValueError as exc:
            strict_ok = False
            print("   严格 JSON 解析失败: %s" % exc)
        check("快照里不再出现 NaN/Infinity（浏览器 JSON.parse 能过）", strict_ok)

        check("整批都是 NaN 时被拒（一条可用样本都没有 → 400）",
              _expect_400(tbase, {"device": pdev,
                                  "batch": [[float("nan")] * 3]}))

        inf_cmd = post_json("http://127.0.0.1:%d/api/command" % port,
                            {"name": "led_blink", "device": pdev,
                             "params": {"n": "inf"}})
        check("params 里的 \"inf\" 不再打出 500（回退默认值）",
              inf_cmd.get("ok") is True, "实际: %s" % inf_cmd)

        post_json("http://127.0.0.1:%d/api/command" % port,
                  {"name": "set_config", "device": pdev, "params": {"period_ms": 50}})
        cmds_out = get_json("http://127.0.0.1:%d/api/commands?device=%s"
                            % (port, urllib.parse.quote(pdev)))
        per_sent = ((cmds_out.get("commands") or [{}])[0].get("params") or {}).get("period_ms")
        check("上报周期下界被夹到 100ms（50ms=20Hz 会打爆连接数）",
              per_sent == 100, "实际: %s" % per_sent)

        # 照片响应不能同时带 no-store 和 max-age（实测过：浏览器按最严的来，
        # 于是永远不缓存，每翻一次列表重下一遍）
        jpeg = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"
        OPENER.open(urllib.request.Request(
            "http://127.0.0.1:%d/api/frame?device=%s&save=1"
            % (port, urllib.parse.quote(pdev)),
            data=jpeg, headers={"Content-Type": "image/jpeg"}), timeout=10).read()
        shots = (get_json("http://127.0.0.1:%d/api/shots?device=%s"
                          % (port, urllib.parse.quote(pdev))).get("shots") or [])
        if shots:
            with OPENER.open("http://127.0.0.1:%d/api/shots/%s/%s"
                             % (port, urllib.parse.quote(pdev), shots[0]["name"]),
                             timeout=10) as rr:
                cc = rr.headers.get_all("Cache-Control") or []
            check("照片响应只有一个 Cache-Control（no-store 与 max-age 不再打架）",
                  len(cc) == 1, "实际: %s" % cc)
        else:
            check("照片接口可用（save=1 之后能在列表里查到）", False, "列表为空")

        # ---- 27. 跨站写请求与花钱接口的限流（2026-10-30）-------------------
        # CORS 只挡"读响应"，挡不住"请求被执行"。没有这道检查时，老师/同学
        # 浏览任意网页，那个网页一条 fetch 就能下发 sd_format（格掉学生的卡）。
        print("\n[27] 跨站写请求被挡 · 花钱接口有冷却（2026-10-30）")

        def _cmd_with(headers, payload):
            h = {"Content-Type": "application/json"}
            h.update(headers)
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/command" % port,
                data=json.dumps(payload).encode("utf-8"), headers=h)
            try:
                with OPENER.open(req, timeout=8) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        check("别的网站发来的写请求被拒（403）",
              _cmd_with({"Origin": "http://evil.example"}, {"name": "capture_once"}) == 403)
        check("同源写请求照常放行（Origin == Host）",
              _cmd_with({"Origin": "http://127.0.0.1:%d" % port},
                        {"name": "capture_once"}) == 200)
        check("不带 Origin 的脚本调用照常放行（curl / 板端）",
              _cmd_with({}, {"name": "capture_once"}) == 200)

        adev = "rob-audio"
        aurl = "http://127.0.0.1:%d/api/audio?device=%s" % (
            port, urllib.parse.quote(adev))
        first_ok = post_bytes(aurl, b"\x01\x02" * 800).get("ok") is True
        try:
            post_bytes(aurl, b"\x01\x02" * 800)
            second_code = 200
        except urllib.error.HTTPError as e:
            second_code = e.code
        check("同一台板 1.5 秒内第二次录音被限流（429）",
              first_ok and second_code == 429, "实际: first=%s second=%s"
              % (first_ok, second_code))

        # ---- 28. SSE 瘦身后的两个出口（2026-10-30）-------------------------
        # SSE 每帧只发"前端真会画出来的那些"（40 条事件 / 8 条命令），
        # 单页面带宽从 228 KB/s 降到约 59 KB/s。历史要全量就得有另一个出口，
        # 这条断言钉住它存在、且 limit 真的生效。
        print("\n[28] 事件历史出口（SSE 瘦身后的全量通道）")
        ev_all = get_json("http://127.0.0.1:%d/api/events?device=%s"
                          % (port, urllib.parse.quote(pdev)))
        check("/api/events 可按设备取事件",
              ev_all.get("device") == pdev and isinstance(ev_all.get("events"), list),
              "实际: device=%s, n=%s" % (ev_all.get("device"),
                                         len(ev_all.get("events") or [])))
        ev_1 = get_json("http://127.0.0.1:%d/api/events?device=%s&limit=1"
                        % (port, urllib.parse.quote(pdev)))
        check("/api/events 的 limit 生效（只要 1 条）",
              len(ev_1.get("events") or []) <= 1, "实际: %d 条"
              % len(ev_1.get("events") or []))

        # ---- 29. 写接口不许"猜"目标设备（2026-10-30）-----------------------
        # 把设备名拼错一个字，原来会退回到"最近上报过的那台" —— 于是 sd_format
        # 发给了另一块板，页面上还显示"已下发"。读写语义必须分开：读接口容错，
        # 写接口一字不差。
        print("\n[29] 写指令给不存在的设备名 → 拒绝，且一条都不发")

        def _cmd_count(dev_id):
            got = get_json("http://127.0.0.1:%d/api/commands?device=%s"
                           % (port, urllib.parse.quote(dev_id)))
            return len(got.get("commands") or [])

        before_n = _cmd_count(pdev)
        try:
            post_json("http://127.0.0.1:%d/api/command" % port,
                      {"name": "sd_format", "device": "rob-0l"})   # 故意拼错
            typo_code = 200
        except urllib.error.HTTPError as e:
            typo_code = e.code
        check("拼错的设备名 → 404（不再静默发给另一块板）", typo_code == 404,
              "实际: HTTP %s" % typo_code)
        check("被拒之后一条命令都没建（全有或全无）",
              _cmd_count(pdev) == before_n,
              "实际: %d → %d" % (before_n, _cmd_count(pdev)))
        # 不带 device 的老调用方仍要能下发（单板场景的兼容路径）
        legacy = post_json("http://127.0.0.1:%d/api/command" % port,
                           {"name": "capture_once"})
        check("不带 device 的老调用仍能下发（兼容单板场景）",
              legacy.get("ok") is True, "实际: %s" % legacy)

    finally:
        for p in (srv, srv2, nlboard):
            if p is None:
                continue
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

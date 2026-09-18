#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 交互课 · 第 1 周任务 —— PC 服务器（无需 VPS，自己的电脑即服务器）

职责：
  1. 接收 ESP32-S3-EYE 上报的 IMU 遥测数据（HTTP POST /api/telemetry）
  2. 对运动数据做实时分析（本地规则"AI"；配置了大模型 API 时自动升级为真实 LLM 回复）
  3. 向开发板返回交互结果（当前姿态/活动 + AI 回复文本），开发板在屏幕上显示
  4. 提供网页仪表盘（SSE 实时推送），在浏览器里看到板子的实时姿态、事件流与 AI 对话
  5. 把遥测与事件落到 server/data/*.jsonl（重启不丢，可回放/分析）

仅依赖 Python 标准库，直接运行：
    python server.py                        # 默认监听 0.0.0.0:8000，数据写 server/data/
    python server.py --port 9000
    python server.py --no-log-telemetry     # 只记事件，不记原始波形

可选：接入 OpenAI 兼容大模型（不配也能完整跑通流程）
    set RW1_LLM_API_KEY=sk-xxx
    set RW1_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    set RW1_LLM_MODEL=qwen-turbo

线程模型：
    ThreadingHTTPServer —— 每个 HTTP 连接一个线程；
    大模型调用跑在独立后台线程里，HTTP 响应立刻返回，所以板端永远不会因为
    等大模型而超时（这是修复前最严重的问题：板端 3s 超时 vs 服务端 8s 等待）。
"""

import argparse
import datetime
import json
import math
import os
import queue
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------
MAX_WINDOW = 1200         # 滑动窗口样本数（板端 100Hz 上报时 ≈ 12 秒）
WINDOW_S = 8.0            # 统计/计步/曲线用的时间窗（秒）
MOTION_WINDOW_S = 0.6     # 瞬时运动分类（晃动/跌落）用的短窗（秒）

DEVICE_TIMEOUT = 5.0      # 超过该秒数没有新遥测则视为离线
LLM_TIMEOUT = 25.0        # 大模型请求超时（后台线程里等，不影响板端）

MAX_BATCH = 400           # 单次 POST 最多接受的样本数（防止畸形/恶意负载）
BOARD_REPLY_MAX = 512     # 回传板端的回复字节上限（UTF-8 安全截断）

STEP_MIN_G = 0.25         # 计步：高出窗口均值的幅度阈值（g）
STEP_REFRACTORY_S = 0.30  # 计步：两步之间的最小间隔（秒）
MOTION_STD = 0.06         # "算得上在动"的短窗标准差阈值（g）
SHAKE_ZCR = 3.5           # 晃动判定：|a| 起伏频率高于该值（Hz）。步行 1.5~2.5Hz，
                          # 晃动 3~8Hz —— 只靠幅度分不开两者（走路也有 0.5g 起伏），
                          # 必须靠频率。
WALK_ZCR_MIN = 1.2        # 步行判定：起伏频率下限（Hz）
SHAKE_MIN_GAP_S = 0.6     # 晃动事件的最小间隔（秒），避免一次晃动记成多次
FREEFALL_G = 0.35         # 失重判定：合加速度阈值（g）
FREEFALL_MIN_S = 0.05     # 失重判定：至少持续这么久才算疑似跌落（秒）

ACTIVITY_IDLE = "等待数据…"

# --------------------------------------------------------------------------
# 全局状态（GIL + 一把锁保护即可，数据量极小）
# --------------------------------------------------------------------------
LOCK = threading.Lock()
SAMPLES = deque(maxlen=MAX_WINDOW)        # [(ts, x, y, z)]，x/y 为屏幕坐标
SHAKE_TIMES = deque(maxlen=256)           # 晃动事件时间戳（窗口内计数用）
EVENTS = deque(maxlen=200)                # 事件流（网页显示）
STATE = {
    "device_online": False,
    "last_post": 0.0,
    "source": "-",
    "latest": None,                        # 最近一帧原始数据
    "activity": ACTIVITY_IDLE,
    "step_count": 0,
    "shake_count": 0,
    "ai_reply": "",
    "ai_pending": False,
    "ai_mode": "规则AI",
    "sample_hz": 0.0,                      # 实测采样率（由批量大小与到达间隔推算）
    "boot_time": time.time(),
}

LOGGER = None                              # 由 main() 注入的 JsonlLogger


def push_event(kind, text):
    EVENTS.appendleft({"ts": time.time(), "kind": kind, "text": text})
    if LOGGER is not None:
        LOGGER.event(kind, text)


def clip_utf8(text, limit):
    """把字符串按 UTF-8 字节数截断，绝不切断多字节字符。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit]
    while cut:
        try:
            return cut.decode("utf-8") + "…"
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "…"


# --------------------------------------------------------------------------
# 落盘：JSONL 追加写，独立线程消费队列，绝不阻塞 HTTP 处理
# --------------------------------------------------------------------------
class JsonlLogger:
    """把遥测与事件追加到 <root>/telemetry-YYYY-MM-DD.jsonl 与 events-*.jsonl。

    写盘在后台线程里做，业务线程只往队列里丢一条记录，所以磁盘慢/满都不会
    拖慢遥测响应。队列满时丢最旧的（宁可丢日志也不丢实时性）。
    """

    def __init__(self, root, retain_days=7, log_telemetry=True, log_hz=10.0):
        self.root = root
        self.retain_days = retain_days
        self.log_telemetry = log_telemetry
        self.log_hz = max(1.0, float(log_hz))
        self.q = queue.Queue(maxsize=20000)
        self.dropped = 0
        self.written = 0
        self._fhs = {}                 # path -> 文件句柄（遥测与事件是两个文件，
                                       # 必须各持一个句柄，否则会互相串写）
        os.makedirs(root, exist_ok=True)
        self._purge_old()
        threading.Thread(target=self._run, name="jsonl", daemon=True).start()

    # ---- 生产者 ----------------------------------------------------------
    def telemetry(self, ts, source, pts, dt):
        if not self.log_telemetry or not pts:
            return
        hz = 1.0 / dt if dt > 0 else 0.0
        step = max(1, int(round(hz / self.log_hz))) if hz else 1
        thin = pts[::step]
        self._put({
            "k": "t",
            "ts": round(ts, 3),
            "src": source,
            "hz": round(hz, 1),
            "s": [[round(x, 3), round(y, 3), round(z, 3)] for (x, y, z) in thin],
        })

    def event(self, kind, text):
        self._put({"k": "e", "ts": round(time.time(), 3), "kind": kind, "text": text})

    def _put(self, rec):
        try:
            self.q.put_nowait(rec)
        except queue.Full:
            self.dropped += 1

    # ---- 消费者 ----------------------------------------------------------
    def _run(self):
        while True:
            rec = self.q.get()
            if rec is None:
                break
            try:
                self._write(rec)
            except OSError:
                pass          # 磁盘满/权限问题不该拖垮服务器

    def _path(self, prefix, ts):
        day = datetime.date.fromtimestamp(ts).isoformat()
        return os.path.join(self.root, "%s-%s.jsonl" % (prefix, day))

    def _write(self, rec):
        day = datetime.date.fromtimestamp(rec["ts"]).isoformat()
        path = self._path("telemetry" if rec["k"] == "t" else "events", rec["ts"])
        fh = self._fhs.get(path)
        if fh is None:
            # 跨天时把昨天的句柄关掉，别让长跑的服务攒一堆打开的 fd
            for old in [p for p in self._fhs if day not in p]:
                try:
                    self._fhs.pop(old).close()
                except OSError:
                    pass
            fh = open(path, "a", encoding="utf-8")
            self._fhs[path] = fh
        fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        self.written += 1

    def _purge_old(self):
        """启动时删掉超过保留期的日志，避免一个学期把磁盘写满。"""
        if self.retain_days <= 0:
            return
        cutoff = time.time() - self.retain_days * 86400
        for name in os.listdir(self.root):
            if not (name.startswith("telemetry-") or name.startswith("events-")):
                continue
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    print("   [日志] 已清理过期文件 %s" % name)
            except OSError:
                pass

    def stats(self):
        files = []
        total = 0
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".jsonl"):
                continue
            size = os.path.getsize(os.path.join(self.root, name))
            files.append({"name": name, "bytes": size})
            total += size
        return {"dir": self.root, "files": files, "bytes": total,
                "written": self.written, "dropped": self.dropped}


# --------------------------------------------------------------------------
# 运动分析：本地规则"AI"
#
# 坐标契约：板端上报的 x/y 已经是**屏幕坐标系**（+x 右、+y 下），板端在发送前
# 应用了自己的 NVS 方向校准（accel_input 的 orientation）。服务端不再做任何
# 轴向假设，两端对"上/下/左/右"的定义因此天然一致。
# --------------------------------------------------------------------------
TILT_DIRS = ["上", "右上", "右", "右下", "下", "左下", "左", "左上"]


def tilt_direction(x, y):
    """把屏幕坐标系的重力方向 (x 向右, y 向下) 映射成 8 方位。"""
    if math.hypot(x, y) < 0.25:
        return None
    deg = math.degrees(math.atan2(y, x)) % 360          # 0°=右, 90°=下
    idx = int(((deg + 22.5) % 360) // 45)               # 0=右
    order = [2, 3, 4, 5, 6, 7, 0, 1]                    # 右,右下,下,左下,左,左上,上,右上 → 方位表
    return TILT_DIRS[order[idx]]


def count_steps(win, mags, mean):
    """对 |a| 序列做局部极大值检测（带不应期），返回窗口内步数。

    用局部极大值而不是"高于均值就算一步"，否则一段持续的高幅运动会
    被每一个采样点都数成一步（100Hz 下会夸张到上百步）。
    """
    if len(win) < 3:
        return 0
    thr = mean + STEP_MIN_G
    steps = 0
    last_t = None
    for i in range(1, len(win) - 1):
        m = mags[i]
        if m <= thr or m < mags[i - 1] or m < mags[i + 1]:
            continue
        t = win[i][0]
        if last_t is None or (t - last_t) >= STEP_REFRACTORY_S:
            steps += 1
            last_t = t
    return steps


def max_freefall_run(mags, dt):
    """返回 mags 中最长的连续失重样本数，用来判跌落。"""
    need = max(2, int(round(FREEFALL_MIN_S / dt))) if dt > 0 else 2
    best = run = 0
    for m in mags:
        if m < FREEFALL_G:
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    return best if best >= need else 0


def zero_cross_rate(mags, dt):
    """估算 |a| 起伏的主频率（Hz）：去均值后数符号穿越，再折算成周期数。

    这是把"步行"和"晃动"分开的关键——两者幅度可以一样大，频率却差一倍以上。
    """
    if len(mags) < 4 or dt <= 0:
        return 0.0
    mean = sum(mags) / len(mags)
    dead = 0.02                     # 死区，避免噪声在均值附近反复穿越
    sign = 0
    crossings = 0
    for m in mags:
        d = m - mean
        if d > dead:
            s = 1
        elif d < -dead:
            s = -1
        else:
            continue
        if sign != 0 and s != sign:
            crossings += 1
        sign = s
    span = (len(mags) - 1) * dt
    return (crossings / span / 2.0) if span > 0 else 0.0


def analyze(now, pts, dt):
    """把一批样本并入滑动窗口，返回 (活动标签, 事件列表)。"""
    events = []
    prev_activity = STATE["activity"]
    prev_fall = STATE.get("fall_active", False)

    n = len(pts)
    for i, (x, y, z) in enumerate(pts):
        SAMPLES.append((now - (n - 1 - i) * dt, x, y, z))

    win = [s for s in SAMPLES if now - s[0] <= WINDOW_S]
    if not win:
        return ACTIVITY_IDLE, events

    mags = [math.sqrt(ax * ax + ay * ay + az * az) for (_, ax, ay, az) in win]
    mean = sum(mags) / len(mags)
    std_all = math.sqrt(sum((m - mean) ** 2 for m in mags) / len(mags))

    # ---- 瞬时运动：用短窗，长窗会把一次 0.2s 的晃动稀释到测不出来 ----
    recent = [s for s in win if now - s[0] <= MOTION_WINDOW_S]
    r_mags = [math.sqrt(ax * ax + ay * ay + az * az) for (_, ax, ay, az) in recent] or mags[-1:]
    r_mean = sum(r_mags) / len(r_mags)
    r_std = math.sqrt(sum((m - r_mean) ** 2 for m in r_mags) / len(r_mags))
    r_peak = max(r_mags)

    steps = count_steps(win, mags, mean)
    STATE["step_count"] = steps

    zcr = zero_cross_rate(r_mags, dt)          # 起伏频率，用来区分步行与晃动

    # ---- 晃动：窗口内计数（修复前是开机累计值，文案却说"最近 8 秒内"）----
    shaking = r_std > MOTION_STD and zcr > SHAKE_ZCR
    if shaking:
        if not SHAKE_TIMES or (now - SHAKE_TIMES[-1]) >= SHAKE_MIN_GAP_S:
            SHAKE_TIMES.append(now)
            events.append(("shake", "检测到晃动/敲击（%.1f Hz）" % zcr))
    while SHAKE_TIMES and (now - SHAKE_TIMES[0]) > WINDOW_S:
        SHAKE_TIMES.popleft()
    STATE["shake_count"] = len(SHAKE_TIMES)

    # ---- 跌落：短窗内的连续失重 ----
    fall_run = max_freefall_run(r_mags, dt)
    fall_active = fall_run > 0
    if fall_active and not prev_fall:
        events.append(("fall", "检测到疑似跌落（失重 %.0f ms）" % (fall_run * dt * 1000)))
    STATE["fall_active"] = fall_active

    # ---- 分类 ----
    if fall_active:
        activity = "疑似跌落(失重)!"
    elif shaking:
        activity = "剧烈晃动"
    elif r_std > MOTION_STD and zcr >= WALK_ZCR_MIN:
        activity = "运动/步行 (峰值 %.1fg, 约%d步)" % (r_peak, steps)
    elif r_std > MOTION_STD:
        activity = "运动 (峰值 %.1fg)" % r_peak
    else:
        tail = [s for s in win if now - s[0] <= 1.0] or win[-1:]
        gx = sum(s[1] for s in tail) / len(tail)
        gy = sum(s[2] for s in tail) / len(tail)
        d = tilt_direction(gx, gy)
        activity = ("静置·向%s倾斜" % d) if d else "静置·水平"

    if activity != prev_activity:
        events.append(("activity", "活动变化：%s → %s" % (prev_activity, activity)))
    return activity, events


def ai_summary():
    """基于统计窗口生成中文摘要（本地模板 / LLM 均可复用）。"""
    now = time.time()
    with LOCK:
        n = len(SAMPLES)
        steps = STATE["step_count"]
        shakes = STATE["shake_count"]
        act = STATE["activity"]
        src = STATE["source"]
        last = STATE["latest"] or (0, 0, 0, 0)
    if n == 0:
        return "我还没有收到任何传感器数据，请检查开发板是否连上了 WiFi 和服务器。"
    x, y, z = last[1], last[2], last[3]
    g_mag = math.sqrt(x * x + y * y + z * z)
    base = (
        "你好！我是开发板的小助手。当前传感器(%s)读数 "
        "X=%+.2fg Y=%+.2fg Z=%+.2fg，合加速度 %.2fg，"
        "正在进行的动作是「%s」。最近 8 秒内约计步 %d 次、晃动 %d 次。"
        % (src, x, y, z, g_mag, act, steps, shakes)
    )
    hint = tilt_direction(x, y)
    if act.startswith("静置") and hint:
        base += " 你现在把板子向%s侧倾斜。" % hint
    elif act.startswith("静置"):
        base += " 板子基本放平了，试试把它向某个方向倾斜吧。"
    return base


def ask_llm(question):
    """可选：调用 OpenAI 兼容大模型。未配置/失败返回 None → 回退本地模板。"""
    key = os.environ.get("RW1_LLM_API_KEY")
    if not key:
        return None
    base = os.environ.get("RW1_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("RW1_LLM_MODEL", "gpt-4o-mini")
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "你是嵌入式课堂助手。回答要简短（120 字以内），口语化，用中文，"
                        "不要用 Markdown 标题或列表。"},
            {"role": "user", "content": "开发板传感器情况：%s\n同学想问：%s" % (ai_summary(), question)},
        ],
        "max_tokens": 300,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 —— 任何失败都回退本地模板
        push_event("warn", "大模型调用失败(%s)，已用本地AI回复" % exc.__class__.__name__)
        return None


def _llm_worker(question):
    """后台线程：调大模型，完成后把结果写回 STATE。

    注意 ai_summary() 内部会取 LOCK，必须在进入 with LOCK 之前调用，否则自锁。
    """
    reply = ask_llm(question)
    mode = "大模型" if reply else "规则AI"
    if not reply:
        reply = ai_summary()
    with LOCK:
        STATE["ai_pending"] = False
        STATE["ai_reply"] = reply
        STATE["ai_mode"] = mode
    push_event("ai" if mode == "大模型" else "warn",
               "%s回复：%s" % (mode, reply[:60]))


def request_ai(question):
    """处理一次板端提问，**立即**返回文本；大模型在后台生成，稍后自动生效。

    板端每个遥测周期都会带回最新的 ai_reply，所以后台结果下一帧就会显示，
    不需要板端等待，也就彻底消除了"板端 3s 超时 vs 服务端 8s 等待"的死结。
    """
    if os.environ.get("RW1_LLM_API_KEY"):
        with LOCK:
            if STATE["ai_pending"]:
                return STATE["ai_reply"] or "我还在想上一个问题，稍等一下…"
            STATE["ai_pending"] = True
            STATE["ai_reply"] = "正在思考…"
            STATE["ai_mode"] = "大模型"
        push_event("ask", "板端提问「%s」→ 已转交大模型" % question)
        threading.Thread(target=_llm_worker, args=(question,), daemon=True,
                         name="llm").start()
        return "正在思考…"

    reply = ai_summary()
    with LOCK:
        STATE["ai_reply"] = reply
        STATE["ai_mode"] = "规则AI"
    push_event("ask", "板端提问「%s」→ %s" % (question, reply[:40]))
    return reply


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
def decimate(seq, maxn):
    """把序列降采样到最多 maxn 个点，用于给仪表盘画曲线。"""
    if len(seq) <= maxn:
        return list(seq)
    step = len(seq) / float(maxn)
    return [seq[min(len(seq) - 1, int(i * step))] for i in range(maxn)]


class Handler(BaseHTTPRequestHandler):
    server_version = "RW1/1.1"
    protocol_version = "HTTP/1.1"     # 让板端的 esp_http_client 能复用连接

    def log_message(self, fmt, *args):   # 安静一点
        pass

    # ---- helpers ---------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        # 每个请求一连接就关：板端本来就是一次 POST 一个 client，浏览器这边请求也
        # 很少。省掉 keep-alive 的状态机，行为对两端都最好预测。
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        try:
            # errors="replace": 即使个别字节异常也不影响数字字段解析
            return json.loads(self.rfile.read(n).decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- routing ---------------------------------------------------------
    def do_OPTIONS(self):                       # CORS 预检
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif path == "/api/latest":
            with LOCK:
                snap = dict(STATE)
                snap["samples"] = [[round(t, 2), x, y, z]
                                   for (t, x, y, z) in decimate(SAMPLES, 240)]
                snap["events"] = list(EVENTS)
            self._send(200, json.dumps(snap, ensure_ascii=False))
        elif path == "/api/logs":
            self._send(200, json.dumps(LOGGER.stats() if LOGGER else {}, ensure_ascii=False))
        elif path == "/api/stream":
            self._sse()
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/telemetry":
            self._telemetry()
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    # ---- telemetry (板 → 服务器 → 板) --------------------------------------
    def _telemetry(self):
        msg = self._read_json()
        now = time.time()

        pts = self._parse_points(msg)
        if not pts:
            self._send(400, json.dumps({"ok": False, "error": "bad payload"}))
            return

        with LOCK:
            prev_post = STATE["last_post"]
            was_online = STATE["device_online"]
            # 采样间隔由"本批样本数 / 两批到达的间隔"自校准，不依赖板端上报
            if prev_post > 0 and len(pts) > 1:
                dt = (now - prev_post) / float(len(pts))
                dt = min(0.6, max(0.002, dt))
            else:
                dt = 0.01
            STATE["sample_hz"] = round(1.0 / dt, 1)

            STATE["device_online"] = True
            STATE["last_post"] = now
            STATE["source"] = str(msg.get("source", "-"))[:24]
            STATE["latest"] = (now, pts[-1][0], pts[-1][1], pts[-1][2])
            if not was_online:
                push_event("info", "开发板已连接")

            activity, events = analyze(now, pts, dt)
            STATE["activity"] = activity
            for kind, text in events:
                push_event(kind, text)
            src = STATE["source"]

        if LOGGER is not None:
            LOGGER.telemetry(now, src, pts, dt)

        reply = ""
        if msg.get("ask"):                  # 板子 BOOT 键 → 请求一次 AI 交互
            question = str(msg.get("q", "我现在的状态怎么样？"))[:200]
            reply = request_ai(question)    # 立刻返回；大模型走后台线程

        with LOCK:
            final_reply = reply or STATE["ai_reply"]
            pending = STATE["ai_pending"]

        out = {
            "ok": True,
            "activity": activity,
            "reply": clip_utf8(final_reply, BOARD_REPLY_MAX),
            "pending": pending,
        }
        self._send(200, json.dumps(out, ensure_ascii=False))

    @staticmethod
    def _parse_points(msg):
        """支持两种载荷：新协议 batch=[[x,y,z],...]，以及单帧 x/y/z（向后兼容）。"""
        pts = []
        batch = msg.get("batch")
        if isinstance(batch, list):
            for item in batch[:MAX_BATCH]:
                try:
                    pts.append((float(item[0]), float(item[1]), float(item[2])))
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
        if not pts:
            try:
                pts.append((float(msg["x"]), float(msg["y"]), float(msg["z"])))
            except (KeyError, TypeError, ValueError):
                return []
        return pts

    # ---- SSE (服务器 → 浏览器) ---------------------------------------------
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                with LOCK:
                    snap = dict(STATE)
                    snap["events"] = list(EVENTS)
                    snap["sample"] = list(SAMPLES[-1]) if SAMPLES else None
                snap["now"] = time.time()
                self.wfile.write(b"data: " + json.dumps(snap, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # 简化：用默认线程池即可，SSE 长连接靠 ThreadingHTTPServer 每连接一线程


# --------------------------------------------------------------------------
# 网页仪表盘
# --------------------------------------------------------------------------
DASHBOARD_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI交互课 · 第1周 · 实时仪表盘</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0}
body{background:#0d1117;color:#e6edf3;font:14px/1.5 "Microsoft YaHei",system-ui,sans-serif;padding:18px}
h1{font-size:18px;margin-bottom:4px}
.sub{color:#8b949e;font-size:12px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:260px 1fr;gap:14px;max-width:1000px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px}
.on{background:#1f6feb33;color:#79c0ff}.off{background:#f8514933;color:#f85149}
.warn{background:#d2992233;color:#e3b341}
canvas{width:100%;height:200px;display:block}
.row{display:flex;gap:10px;align-items:baseline;margin-top:8px;flex-wrap:wrap}
.big{font-size:20px;font-weight:700;color:#7ee787}
.lbl{color:#8b949e;font-size:12px}
#reply{margin-top:8px;padding:8px;background:#0d1117;border-radius:8px;min-height:40px;color:#e3b341;font-size:13px;white-space:pre-wrap}
#feed{max-height:300px;overflow-y:auto;font-size:12px}
#feed div{padding:3px 0;border-bottom:1px dashed #21262d}
.t{color:#8b949e;margin-right:6px}
.gbars{display:flex;gap:8px;margin-top:10px}
.gbar{flex:1;text-align:center}
.gbar i{display:block;height:8px;background:#30363d;border-radius:4px;margin-top:4px;position:relative}
.gbar i b{position:absolute;top:0;height:100%;border-radius:4px;background:#1f6feb}
footer{margin-top:12px;color:#8b949e;font-size:11px}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>AI交互课 · 第1周 · 开发板 ⇄ 电脑服务器</h1>
<div class="sub">ESP32-S3-EYE（板载IMU，100Hz采样/500ms批量上报）→ HTTP → 本机Python服务器 → 规则/大模型AI分析 → 回传开发板显示 + 本页实时推送 + JSONL落盘</div>
<div class="grid">
  <div class="card">
    <div class="row"><span class="lbl">设备状态</span><span id="dev" class="badge off">离线</span>
      <span id="src" class="lbl"></span></div>
    <div class="row"><span class="lbl">当前活动</span></div>
    <div class="big" id="act">…</div>
    <div class="row"><span class="lbl">AI来源</span><span id="mode" class="lbl"></span>
      <span class="lbl">采样率</span><span id="hz" class="lbl">-</span></div>
    <div class="row"><span class="lbl">步数/8s</span><span id="steps">0</span>
      <span class="lbl">晃动/8s</span><span id="shakes">0</span></div>
    <div class="gbars">
      <div class="gbar"><span class="lbl">X</span><i><b id="bx"></b></i><span id="vx"></span></div>
      <div class="gbar"><span class="lbl">Y</span><i><b id="by"></b></i><span id="vy"></span></div>
      <div class="gbar"><span class="lbl">Z</span><i><b id="bz"></b></i><span id="vz"></span></div>
    </div>
    <div id="reply">按板子 BOOT 键即可向服务器AI提问。</div>
  </div>
  <div class="card">
    <canvas id="cv"></canvas>
    <div class="lbl" style="margin-top:6px">上=倾斜示意图（球随重力滚动，屏幕坐标系）；下方为最近8秒 |a| 曲线</div>
  </div>
</div>
<div class="card" style="max-width:1000px;margin-top:14px">
  <span class="lbl">事件流</span>
  <div id="feed"></div>
</div>
<footer>服务器: <span id="host"></span> · <span id="logdir"></span> · 页面仅用标准库SSE推送，无需外网</footer>
<script>
document.getElementById('host').textContent=location.host;
const cv=document.getElementById('cv'),ctx=cv.getContext('2d');
let last=null;
function fit(){cv.width=cv.clientWidth*devicePixelRatio;cv.height=cv.clientHeight*devicePixelRatio}
addEventListener('resize',fit);fit();
function bar(id,v,vl){const e=document.getElementById(id);e.style.width=(Math.min(Math.abs(v),2)/4+0.5)*100+'%';
 e.style.left=v<0?(50-Math.min(Math.abs(v),2)/4*50)+'%':'50%';
 document.getElementById(vl).textContent=v.toFixed(2)+'g'}
function draw(s){
 ctx.clearRect(0,0,cv.width,cv.height);
 const W=cv.width,H=cv.height,top=H*0.52;
 // 倾斜球
 const cx=W/2,cy=top/2,r=Math.min(cx,cy)-10;
 ctx.strokeStyle='#30363d';ctx.beginPath();ctx.arc(cx,cy,r,0,7);ctx.stroke();
 if(s&&s.sample){const[,x,y]=s.sample;
  ctx.fillStyle='#7ee787';ctx.beginPath();
  ctx.arc(cx+Math.max(-1,Math.min(1,x))*r*0.8,cy+Math.max(-1,Math.min(1,y))*r*0.8,10*devicePixelRatio,0,7);ctx.fill();}
 // |a| 曲线
 const sp=s&&s.samples||[];
 ctx.strokeStyle='#1f6feb';ctx.beginPath();
 const n=sp.length;
 for(let i=0;i<n;i++){const[,x,y,z]=sp[i];const m=Math.sqrt(x*x+y*y+z*z);
  const px=W*0.05+i/Math.max(n-1,1)*W*0.9,py=H-(m/3)*(H-top)-6;
  i?ctx.lineTo(px,py):ctx.moveTo(px,py);}
 ctx.stroke();
 ctx.fillStyle='#8b949e';ctx.font=12*devicePixelRatio+'px sans-serif';
 ctx.fillText('|a| g',6*devicePixelRatio,H-8);}
let evts='';
function es(){
 const src=new EventSource('/api/stream');
 src.onmessage=e=>{const s=JSON.parse(e.data);last=s;
  document.getElementById('dev').className='badge '+(s.device_online?'on':'off');
  document.getElementById('dev').textContent=s.device_online?'在线':'离线';
  document.getElementById('src').textContent=s.source||'';
  document.getElementById('act').textContent=s.activity||'…';
  document.getElementById('mode').textContent=s.ai_mode+(s.ai_pending?' · 生成中…':'');
  document.getElementById('hz').textContent=s.sample_hz?s.sample_hz+' Hz':'-';
  document.getElementById('steps').textContent=s.step_count;
  document.getElementById('shakes').textContent=s.shake_count;
  if(s.latest){bar('bx',s.latest[1],'vx');bar('by',s.latest[2],'vy');bar('bz',s.latest[3],'vz')}
  if(s.ai_reply)document.getElementById('reply').textContent='AI：'+s.ai_reply;
  const f=document.getElementById('feed');
  if(s.events.length){const h=s.events.slice(0,30).map(ev=>
    `<div><span class="t">${new Date(ev.ts*1000).toLocaleTimeString()}</span>${ev.kind} · ${ev.text}</div>`).join('');
   if(h!==evts){evts=h;f.innerHTML=h}}
  draw(s)};
 src.onerror=()=>{src.close();setTimeout(es,2000)};
}
fetch('/api/logs').then(r=>r.json()).then(l=>{
  if(l&&l.dir)document.getElementById('logdir').textContent='落盘: '+l.dir});
// SSE 里不带 samples 全量，定时拉一次用于曲线
function pull(){fetch('/api/latest').then(r=>r.json()).then(s=>{if(last)s.ai_reply=last.ai_reply;last=s;draw(s)})}
pull();setInterval(pull,2000);
es();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="AI交互课第1周 · PC服务器")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                    help="JSONL 日志目录（默认 server/data）")
    ap.add_argument("--retain-days", type=int, default=7,
                    help="启动时清理多少天前的日志，0 表示不清理")
    ap.add_argument("--log-hz", type=float, default=10.0,
                    help="原始波形落盘的降采样频率（Hz），默认 10")
    ap.add_argument("--no-log-telemetry", action="store_true",
                    help="只落盘事件，不落盘原始波形")
    args = ap.parse_args()

    global LOGGER
    LOGGER = JsonlLogger(args.data_dir, retain_days=args.retain_days,
                         log_telemetry=not args.no_log_telemetry, log_hz=args.log_hz)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    # 后台心跳：设备超时判定离线
    def watchdog():
        while True:
            time.sleep(1.0)
            with LOCK:
                online = (time.time() - STATE["last_post"]) < DEVICE_TIMEOUT
                if STATE["device_online"] and not online:
                    push_event("info", "开发板连接超时，已标记离线")
                STATE["device_online"] = online
    threading.Thread(target=watchdog, daemon=True).start()

    llm = "大模型已配置 (%s)" % os.environ.get("RW1_LLM_MODEL", "?") \
        if os.environ.get("RW1_LLM_API_KEY") else "本地规则AI（可配 RW1_LLM_API_KEY 升级）"
    print("=" * 66)
    print(" AI交互课 第1周 · PC 服务器已启动")
    print("   仪表盘:  http://localhost:%d/" % args.port)
    print("   遥测:    POST http://<本机IP>:%d/api/telemetry" % args.port)
    print("   AI模式:  %s" % llm)
    print("   落盘:    %s%s" % (args.data_dir,
          "（仅事件）" if args.no_log_telemetry else "（波形 %.0fHz + 事件，保留 %d 天）"
          % (args.log_hz, args.retain_days)))
    print("=" * 66)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()

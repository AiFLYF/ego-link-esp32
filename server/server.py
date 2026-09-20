#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 交互课 · PC 服务器（无需 VPS，自己的电脑即服务器）

职责：
  1. 接收 ESP32-S3-EYE 上报的 IMU 遥测数据（HTTP POST /api/telemetry）
  2. 对运动数据做实时分析（本地规则"AI"；配置了大模型 API 时自动升级为真实 LLM 回复）
  3. 向开发板返回交互结果（当前姿态/活动 + AI 回复文本），开发板在屏幕上显示
  4. 提供网页仪表盘（SSE 实时推送），在浏览器里看到板子的实时姿态、事件流与 AI 对话
  5. 把遥测与事件落到 server/data/*.jsonl（重启不丢，可回放/分析）
  6. **远程指令通道**：网页下发「采集一次」→ 搭下一帧遥测的响应下发 → 板子执行后
     按 request_id 回传结果 → 网页显示 queued/sent/done 全过程

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

# ---- 远程命令（Web 下发「采集一次」，按 request_id 反馈结果）----
CMD_TIMEOUT_S = 10.0      # 命令下发后多久没收到结果就判超时
CMD_MAX_HISTORY = 20      # 保留最近多少条命令供网页显示
CMD_NAMES = ("capture_once",)   # 支持的指令白名单（不认的名字直接 400）

ACTIVITY_IDLE = "等待数据…"

# --------------------------------------------------------------------------
# 全局状态（GIL + 一把锁保护即可，数据量极小）
# --------------------------------------------------------------------------
LOCK = threading.Lock()
SAMPLES = deque(maxlen=MAX_WINDOW)        # [(ts, x, y, z)]，x/y 为屏幕坐标
SHAKE_TIMES = deque(maxlen=256)           # 晃动事件时间戳（窗口内计数用）
EVENTS = deque(maxlen=200)                # 事件流（网页显示）
COMMANDS = {}                             # request_id -> 命令记录
COMMAND_QUEUE = deque()                   # 待下发的 request_id（FIFO）
CMD_SEQ = 0                               # 生成可读 request_id 的递增序号
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
# 远程命令：Web 下发 → 板端执行 → 按 request_id 回传结果
#
# 板子是纯客户端（没有长连接、不能主动收推送），所以用**搭车**的方式：
# 命令挂在下一帧遥测的响应里下发，板子执行完在**再下一帧**的请求体里带回结果。
# 一次往返 = 2 个遥测周期（默认 1 秒），网页上能看到 queued → sent → done 全过程。
# --------------------------------------------------------------------------
def new_command(name, params=None):
    """建一条待下发的命令，返回 request_id。"""
    global CMD_SEQ
    with LOCK:
        CMD_SEQ += 1
        cid = "c-%d-%d" % (int(time.time()), CMD_SEQ)
        COMMANDS[cid] = {
            "id": cid,
            "name": name,
            "params": params or {},
            "state": "queued",      # queued / sent / done / failed / timeout
            "created": time.time(),
            "sent": None,
            "done": None,
            "result": None,
        }
        COMMAND_QUEUE.append(cid)
        # 只保留最近 CMD_MAX_HISTORY 条，避免长跑时无限增长
        while len(COMMANDS) > CMD_MAX_HISTORY:
            oldest = min(COMMANDS, key=lambda k: COMMANDS[k]["created"])
            COMMANDS.pop(oldest, None)
            try:
                COMMAND_QUEUE.remove(oldest)
            except ValueError:
                pass
    return cid


def take_command_for_board():
    """取一条待下发的命令并标记为 sent。**调用方必须已持有 LOCK。**

    返回给板端的 {"id","name","params"}，或 None。一次只发一条，
    板子也一次只执行一条，语义简单、不会乱序。
    """
    while COMMAND_QUEUE:
        cid = COMMAND_QUEUE.popleft()
        rec = COMMANDS.get(cid)
        if rec is None or rec["state"] != "queued":
            continue
        rec["state"] = "sent"
        rec["sent"] = time.time()
        return {"id": cid, "name": rec["name"], "params": rec["params"]}
    return None


def apply_command_result(res):
    """处理板端回传的 result，返回一句给人看的事件文本（无关/不匹配则 None）。

    注意本函数内部会取 LOCK，不能在持有 LOCK 时调用。
    """
    if not isinstance(res, dict):
        return None
    cid = str(res.get("id", ""))[:32]
    keep = ("id", "ok", "ms", "n", "x", "y", "z", "std", "err")
    with LOCK:
        rec = COMMANDS.get(cid)
        if rec is None:
            return None                      # 可能是被淘汰的老命令，静默忽略
        rec["state"] = "done" if res.get("ok") else "failed"
        rec["done"] = time.time()
        rec["result"] = {k: res[k] for k in keep if k in res}
        latency = (rec["done"] - rec["sent"]) if rec["sent"] else 0.0
        name = rec["name"]
        r = rec["result"]
        if rec["state"] == "done":
            text = ("命令 %s 完成（往返 %.0f ms）：x=%+.3f y=%+.3f z=%+.3f，%d 样本，标准差 %.4f g"
                    % (name, latency * 1000, r.get("x", 0.0), r.get("y", 0.0), r.get("z", 0.0),
                       r.get("n", 0), r.get("std", 0.0)))
        else:
            text = "命令 %s 执行失败：%s" % (name, r.get("err", "未说明"))
    return text


def expire_commands():
    """把下发后长时间没回音的命令判为超时，返回超时的命令名列表。"""
    now = time.time()
    expired = []
    with LOCK:
        for rec in COMMANDS.values():
            if rec["state"] == "sent" and rec["sent"] and (now - rec["sent"]) > CMD_TIMEOUT_S:
                rec["state"] = "timeout"
                rec["done"] = now
                expired.append(rec["name"])
    return expired


def commands_snapshot():
    """按时间倒序返回最近若干条命令的副本（**调用方必须已持有 LOCK**）。"""
    return [dict(r) for r in sorted(COMMANDS.values(), key=lambda r: r["created"], reverse=True)]


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
                snap["commands"] = commands_snapshot()
            self._send(200, json.dumps(snap, ensure_ascii=False))
        elif path == "/api/commands":
            with LOCK:
                cmds = commands_snapshot()
            self._send(200, json.dumps({"ok": True, "commands": cmds,
                                        "names": list(CMD_NAMES)}, ensure_ascii=False))
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
        elif path == "/api/command":
            self._command()
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    # ---- command (网页 → 服务器 → 板 → 服务器 → 网页) -----------------------
    def _command(self):
        msg = self._read_json()
        name = str(msg.get("name", "capture_once"))[:32]
        if name not in CMD_NAMES:
            self._send(400, json.dumps(
                {"ok": False, "error": "unknown command: %s" % name}, ensure_ascii=False))
            return
        with LOCK:
            online = STATE["device_online"]
        cid = new_command(name, msg.get("params"))
        push_event("cmd", "下发命令 %s（%s）" % (name, cid))
        self._send(200, json.dumps(
            {"ok": True, "id": cid, "device_online": online}, ensure_ascii=False))

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
            # 有排队中的命令就搭这一帧的响应发下去（一次一条）
            cmd = take_command_for_board()

        if LOGGER is not None:
            LOGGER.telemetry(now, src, pts, dt)

        # 板端回传的上一条命令结果（必须在 LOCK 之外处理，apply_command_result 内部取锁）
        cmd_text = apply_command_result(msg.get("result"))
        if cmd_text:
            push_event("cmd", cmd_text)

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
        if cmd is not None:
            out["cmd"] = cmd
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
                    snap["commands"] = commands_snapshot()
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
<title>Ego Link · 实时仪表盘</title>
<style>
/* ==========================================================================
   设计语言与开发板屏幕（device/main/ui.c）刻意保持一致：
   同一套语义色、同一套「圆环 + 姿态球 + 三轴对称条」的表达，
   现场看板子和看网页看到的是同一个东西。
   ========================================================================== */
:root{
  color-scheme:dark;
  --bg:#0a0e14; --bg-deep:#05070a;
  --card:#141a23; --card-hi:#1b2230;
  --line:#252d3a; --track:#232c39; --mark:#3d4757;
  --text:#e6edf3; --dim:#8b949e; --faint:#5a6472;
  --green:#3fb950; --teal:#2dd4bf; --blue:#58a6ff;
  --amber:#e3b341; --red:#f85149; --purple:#a371f7;
  /* 当前活动语义色（JS 按 classify 结果注入 RGB 分量），驱动 hero 卡片与环的联动 */
  --act-rgb:63,185,80;
  --r:16px;
}
*{box-sizing:border-box;margin:0}
body{
  background:radial-gradient(1200px 600px at 20% -10%,#131b27 0%,var(--bg) 55%,var(--bg-deep) 100%);
  background-attachment:fixed;
  color:var(--text);
  font:14px/1.6 "Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:20px 20px 40px;
  min-height:100vh;
  -webkit-font-smoothing:antialiased;
}
.mono{font-family:Consolas,"SF Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
h1,h2{font-weight:650;letter-spacing:.2px}

/* ---------- 顶栏 ---------- */
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;flex-wrap:wrap;max-width:1400px;margin:0 auto 18px}
.brand{display:flex;gap:14px;align-items:flex-start}
.logo{
  width:38px;height:38px;border-radius:12px;flex:none;margin-top:2px;
  background:linear-gradient(140deg,var(--teal),var(--blue) 55%,var(--purple));
  box-shadow:0 6px 20px -6px var(--teal);
  position:relative;
}
.logo::after{content:"";position:absolute;inset:11px;border-radius:50%;background:var(--bg);opacity:.85}
h1{font-size:19px;line-height:1.35}
.sub{color:var(--dim);font-size:12px;max-width:720px}
.pills{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{
  display:inline-flex;align-items:center;gap:7px;
  padding:6px 13px;border-radius:99px;font-size:12px;
  background:var(--card);border:1px solid var(--line);color:var(--dim);
  white-space:nowrap;
}
.pill i{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.pill.on{color:var(--green);border-color:#1c3a26;background:#0f1d15}
.pill.off{color:var(--red);border-color:#3d1f1f;background:#1c1113}
.pill.off i{animation:breathe 1.6s ease-in-out infinite}
.pill.warn{color:var(--amber);border-color:#3a2f14;background:#1d1911}
@keyframes breathe{0%,100%{opacity:1}50%{opacity:.25}}

/* ---------- 卡片网格 ---------- */
.grid{display:grid;gap:14px;max-width:1400px;margin:0 auto;grid-template-columns:320px minmax(0,1fr) 320px}
.card{
  background:linear-gradient(180deg,var(--card-hi),var(--card));
  border:1px solid var(--line);border-radius:var(--r);
  padding:16px;min-width:0;
  transition:border-color .25s ease,transform .25s ease,box-shadow .25s ease;
}
.card:hover{border-color:#37445a;transform:translateY(-1px)}
/* hero 卡片随当前活动色微染：边框一圈淡色 + 外发光，和板端面板边框一个语言 */
.card.hero{
  border-color:rgba(var(--act-rgb),.38);
  box-shadow:0 0 0 1px rgba(var(--act-rgb),.10),0 22px 60px -30px rgba(var(--act-rgb),.55);
}
.card.wide{max-width:1400px;margin:14px auto 0}
.card-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:12px}
h2{font-size:13px;color:var(--text)}
.card-head .src{font-size:11px;color:var(--faint)}

/* ---------- 环形仪表 ---------- */
.gauge{position:relative;width:100%;max-width:210px;margin:4px auto 10px;aspect-ratio:1}
.gauge svg{width:100%;height:100%;display:block;transform:rotate(135deg)}
.gauge circle{fill:none;stroke-linecap:round;transform-origin:70px 70px}
.gauge .track{stroke:var(--track);stroke-width:11;stroke-dasharray:263.9 351.9}
.gauge .val{
  stroke:var(--green);stroke-width:11;stroke-dasharray:0 351.9;
  transition:stroke-dasharray .5s cubic-bezier(.22,1,.36,1),stroke .4s;
  filter:drop-shadow(0 0 5px rgba(var(--act-rgb),.55));
}
.gauge-mid{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px}
.act{font-size:30px;font-weight:700;letter-spacing:2px;line-height:1.1;transition:color .4s}
.act.pop{animation:pop .42s cubic-bezier(.16,1,.3,1)}
@keyframes pop{0%{opacity:.2;transform:translateY(4px)}100%{opacity:1;transform:none}}
.abs{font-size:14px;color:var(--dim)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;border-top:1px solid var(--line);padding-top:12px}
.stats>div{display:flex;flex-direction:column;gap:2px;text-align:center}
.stats span{font-size:11px;color:var(--faint)}
.stats b{font-size:16px;font-weight:650;font-variant-numeric:tabular-nums}
.stats b.sm{font-size:12px;font-weight:500;color:var(--dim)}

/* ---------- 曲线 ---------- */
#cv{width:100%;height:220px;display:block;border-radius:10px}
.legend{display:flex;gap:16px;font-size:11px;color:var(--faint);margin-top:8px;flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}

/* ---------- 姿态球 + 三轴条 ---------- */
.ball{
  position:relative;width:100%;max-width:190px;aspect-ratio:1;margin:6px auto 14px;
  border-radius:50%;border:1px solid var(--line);
  background:radial-gradient(circle at 50% 40%,#0f1620,#0a0e14 70%);
}
.ball .ring2{position:absolute;left:50%;top:50%;width:34%;height:34%;transform:translate(-50%,-50%);border-radius:50%;border:1px dashed var(--track)}
.ball .cross::before,.ball .cross::after{content:"";position:absolute;background:var(--line)}
.ball .cross::before{left:50%;top:14%;bottom:14%;width:1px}
.ball .cross::after{top:50%;left:14%;right:14%;height:1px}
/* 45° 斜辅助线：与板端同一套水平仪刻度，更弱 */
.ball .cross2{position:absolute;left:50%;top:50%;width:52%;height:52%;transform:translate(-50%,-50%) rotate(45deg)}
.ball .cross2::before,.ball .cross2::after{content:"";position:absolute;background:var(--mark);opacity:.7}
.ball .cross2::before{left:50%;top:0;bottom:0;width:1px}
.ball .cross2::after{top:50%;left:0;right:0;height:1px}
.ball .dot{
  position:absolute;left:50%;top:50%;width:20px;height:20px;margin:-10px 0 0 -10px;border-radius:50%;
  background:var(--green);box-shadow:0 0 18px -2px var(--green);
  transition:transform .3s cubic-bezier(.22,1,.36,1),background .4s,box-shadow .4s;
}
.bars{display:flex;flex-direction:column;gap:9px}
.bar{display:grid;grid-template-columns:14px 1fr 52px;gap:9px;align-items:center;font-size:12px}
.bar em{font-style:normal;font-weight:700}
.bar .t{position:relative;height:7px;border-radius:4px;background:var(--track);overflow:visible}
.bar .t i{position:absolute;top:0;height:100%;border-radius:4px;left:50%;width:0;transition:left .3s,width .3s,background .3s}
/* 0 位中线：和板端轴条同色同位 */
.bar .t::after{content:"";position:absolute;left:50%;top:-1px;bottom:-1px;width:1px;transform:translateX(-.5px);background:var(--mark)}
.bar .v{text-align:right;font-size:12px;font-variant-numeric:tabular-nums}

/* ---------- AI 回复 ---------- */
.reply{margin-top:14px;padding:12px 14px 12px 12px;border-radius:12px;background:#0d1219;border:1px solid var(--line);border-left:3px solid rgba(88,166,255,.6);position:relative}
.reply .tag{
  display:inline-block;font-size:10px;font-weight:700;letter-spacing:.5px;
  color:var(--blue);background:#1f6feb33;border-radius:99px;padding:2px 9px;margin-bottom:7px;
}
.reply .txt{font-size:13px;color:var(--dim);white-space:pre-wrap;word-break:break-word;transition:color .3s}
.reply.has .txt{color:var(--amber)}
.reply.pending .txt{color:var(--dim)}
.reply.pending::after{
  content:"";position:absolute;right:14px;top:14px;width:13px;height:13px;border-radius:50%;
  border:2px solid var(--track);border-top-color:var(--amber);animation:spin .9s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ---------- 按钮 ---------- */
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{
  font:inherit;font-size:13px;cursor:pointer;border-radius:9px;padding:8px 16px;
  border:1px solid var(--line);background:var(--card-hi);color:var(--text);
  transition:background .18s,border-color .18s,transform .06s;
}
button:hover:not(:disabled){background:#222c3c;border-color:#37445a}
button:active:not(:disabled){transform:translateY(1px)}
button.primary{background:#1f6feb;border-color:#2f81f7;color:#fff}
button.primary:hover:not(:disabled){background:#2f81f7}
button:disabled{opacity:.45;cursor:not-allowed}
.hint{font-size:12px;color:var(--dim)}

/* ---------- 指令 / 事件列表 ---------- */
.list{display:flex;flex-direction:column}
.item{display:flex;gap:10px;align-items:baseline;padding:8px 6px;border-bottom:1px dashed #1b2230;font-size:12px;flex-wrap:wrap;border-radius:8px;transition:background .18s}
.item:hover{background:rgba(255,255,255,.025)}
.item:last-child{border-bottom:0}
.st{padding:2px 9px;border-radius:99px;font-size:11px;white-space:nowrap;flex:none}
.st-queued{background:#21262d;color:var(--dim)}
.st-sent{background:#d2992233;color:var(--amber)}
.st-done{background:#2ea04333;color:var(--green)}
.st-failed,.st-timeout{background:#f8514933;color:var(--red)}
.item .name{color:var(--text);font-weight:600}
.item .meta{color:var(--faint);font-size:11px}
#feed{max-height:340px;overflow-y:auto}
#feed .item{gap:12px}
#feed .t{color:var(--faint);font-size:11px;flex:none;font-family:Consolas,monospace}
#feed .k{flex:none;width:8px;height:8px;border-radius:50%;margin-top:6px}
.k-info{background:var(--dim)}.k-motion{background:var(--blue)}
.k-shake{background:var(--amber)}.k-fall,.k-alert{background:var(--red)}
.k-ask{background:var(--purple)}.k-cmd{background:var(--teal)}
footer{max-width:1400px;margin:18px auto 0;color:var(--faint);font-size:11px;line-height:1.8}
.empty{color:var(--faint);font-size:12px;padding:6px 0}

@media(max-width:1100px){
  .grid{grid-template-columns:1fr 1fr}
  .grid .hero{grid-column:1 / -1}
}
@media(max-width:760px){
  body{padding:14px 12px 30px}
  .grid{grid-template-columns:1fr}
  .gauge{max-width:180px}
  #cv{height:170px}
}
</style>
</head>
<body>

<header class="top">
  <div class="brand">
    <div class="logo"></div>
    <div>
      <h1>Ego Link · 实时仪表盘</h1>
      <p class="sub">板载 IMU 100Hz 采样 → 本机服务器实时分类与 AI 分析 → 回传板端并推送到本页</p>
    </div>
  </div>
  <div class="pills">
    <span id="dev" class="pill off"><i></i>离线</span>
    <span id="hzp" class="pill">— Hz</span>
    <span id="postsp" class="pill">↑0 帧</span>
  </div>
</header>

<main class="grid">
  <section class="card hero">
    <div class="card-head"><h2>当前活动</h2><span class="src" id="src">—</span></div>
    <div class="gauge">
      <svg viewBox="0 0 140 140" aria-hidden="true">
        <circle class="track" cx="70" cy="70" r="56"></circle>
        <circle class="val" id="ring" cx="70" cy="70" r="56"></circle>
      </svg>
      <div class="gauge-mid">
        <div class="act" id="act">…</div>
        <div class="abs mono" id="abs">0.00 g</div>
      </div>
    </div>
    <div class="stats">
      <div><span>步数 / 8s</span><b id="steps">0</b></div>
      <div><span>晃动 / 8s</span><b id="shakes">0</b></div>
      <div><span>AI 来源</span><b id="mode" class="sm">规则AI</b></div>
    </div>
    <div class="reply" id="replybox">
      <span class="tag">AI</span>
      <div class="txt" id="reply">按板子 BOOT 键即可向服务器 AI 提问。</div>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>合加速度 |a|</h2><span class="src">最近 8 秒 · 服务端降采样</span></div>
    <canvas id="cv"></canvas>
    <div class="legend">
      <span><i style="background:var(--blue)"></i>|a| 曲线</span>
      <span><i style="background:var(--faint)"></i>1g 参考线</span>
      <span><i style="background:var(--red)"></i>失重阈值 0.35g（疑似跌落）</span>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>姿态（屏幕坐标系）</h2><span class="src" id="tilt">倾角 —</span></div>
    <div class="ball">
      <div class="cross"></div>
      <div class="cross2"></div>
      <div class="ring2"></div>
      <div class="dot" id="ball"></div>
    </div>
    <div class="bars">
      <div class="bar"><em style="color:var(--red)">X</em><div class="t"><i id="bx"></i></div><span class="v mono" id="vx">0.00</span></div>
      <div class="bar"><em style="color:var(--green)">Y</em><div class="t"><i id="by"></i></div><span class="v mono" id="vy">0.00</span></div>
      <div class="bar"><em style="color:var(--blue)">Z</em><div class="t"><i id="bz"></i></div><span class="v mono" id="vz">0.00</span></div>
    </div>
  </section>
</main>

<section class="card wide">
  <div class="card-head">
    <h2>远程指令</h2>
    <span class="src">指令搭在「下一帧遥测的响应」里下发，板子在再下一帧回传结果 —— 一次真实的硬件往返</span>
  </div>
  <div class="row">
    <span id="cmdbts"></span>
    <span id="cmdhint" class="hint"></span>
  </div>
  <div class="list" id="cmds" style="margin-top:10px"></div>
</section>

<section class="card wide">
  <div class="card-head"><h2>事件流</h2><span class="src" id="evcount"></span></div>
  <div id="feed"></div>
</section>

<footer>
  服务器 <span class="mono" id="host"></span> · <span id="logdir"></span> ·
  页面只用标准库 SSE 推送，不依赖外网；指令白名单 <span class="mono" id="names"></span>
</footer>

<script>
(function(){
"use strict";

var $ = function(id){ return document.getElementById(id); };

/* ---------------- 语义色（与 device/main/ui.c 同一套） ---------------- */
var C = { green:"#3fb950", blue:"#58a6ff", amber:"#e3b341", red:"#f85149",
          purple:"#a371f7", teal:"#2dd4bf", dim:"#8b949e", faint:"#5a6472" };
/* 同一颜色的 RGB 分量，用来染 hero 卡片边框/光晕（CSS 变量 --act-rgb） */
var CRGB = { green:"63,185,80", blue:"88,166,255", amber:"227,179,65", red:"248,81,73",
             purple:"163,113,247", teal:"45,212,191", dim:"139,148,158", faint:"90,100,114" };

/* 服务器文案 → 活动词 + 颜色（与板端 classify() 同一套规则） */
var ACT = [
  { k:["跌落","失重"], word:"跌落", color:C.red,   rgb:CRGB.red },
  { k:["晃动"],        word:"晃动", color:C.amber, rgb:CRGB.amber },
  { k:["步行"],        word:"步行", color:C.purple,rgb:CRGB.purple },
  { k:["运动"],        word:"运动", color:C.blue,  rgb:CRGB.blue },
  { k:["静置"],        word:"静置", color:C.green, rgb:CRGB.green }
];
function classify(s){
  for (var i=0;i<ACT.length;i++){
    for (var j=0;j<ACT[i].k.length;j++){
      if (s.indexOf(ACT[i].k[j]) >= 0) return ACT[i];
    }
  }
  return { word: s ? s.slice(0,4) : "等待", color:C.faint, rgb:CRGB.faint };
}

/* 指令 → 按钮文案与参数（按钮从 /api/commands 的 names 动态生成，
   所以服务端新增指令时本页不用改代码） */
var CMD_UI = {
  capture_once:{ label:"采集一次", params:{}, primary:true },
  led_blink:   { label:"闪灯 ×3",  params:{n:3,on_ms:80,off_ms:80} },
  led_set:     { label:"LED 常亮", params:{on:true}, toggle:true }
};

/* ---------------- 环形仪表（270°，与板端同款） ---------------- */
var RING_R = 56, RING_C = 2*Math.PI*RING_R, RING_ARC = RING_C*0.75, ABS_FULL = 2.0;
var lastRing = -1, lastAct = "", lastBall = "", lastBallColor = "", lastTilt = "";
var lastActColor = "rgb(90,100,114)";      /* 曲线读数胶囊描边用，随活动色更新 */

function setRing(mag){
  var pct = Math.max(0, Math.min(1, mag/ABS_FULL));
  var v = Math.round(pct*100);
  if (v === lastRing) return;
  lastRing = v;
  $("ring").style.strokeDasharray = (pct*RING_ARC).toFixed(1) + " " + RING_C.toFixed(1);
  $("abs").textContent = mag.toFixed(2) + " g";
}

/* ---------------- 曲线 ---------------- */
var cv = $("cv"), ctx = cv.getContext("2d"), samples = [];
function fitCanvas(){
  var dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(cv.clientWidth*dpr));
  cv.height = Math.max(1, Math.round(cv.clientHeight*dpr));
  drawChart();
}
function drawChart(){
  var W = cv.width, H = cv.height, dpr = window.devicePixelRatio || 1;
  ctx.clearRect(0,0,W,H);
  if (W < 2 || H < 2) return;

  var padL = 34*dpr, padR = 10*dpr, padT = 12*dpr, padB = 18*dpr;
  var plotW = W - padL - padR, plotH = H - padT - padB;
  var maxG = 2.4;

  function yOf(g){ return padT + plotH - Math.min(g, maxG)/maxG*plotH; }

  /* 网格 + 刻度 */
  ctx.strokeStyle = "#1b2230"; ctx.lineWidth = 1*dpr;
  ctx.fillStyle = C.faint; ctx.font = (10*dpr)+"px Consolas,monospace";
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  [0,0.5,1,1.5,2].forEach(function(g){
    var y = yOf(g);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W-padR, y); ctx.stroke();
    ctx.fillText(g.toFixed(1), padL-6*dpr, y);
  });

  /* 失重阈值 + 1g 参考线 */
  function hline(g, color, dash){
    var y = yOf(g);
    ctx.save(); ctx.setLineDash(dash.map(function(x){return x*dpr}));
    ctx.strokeStyle = color; ctx.lineWidth = 1*dpr;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W-padR, y); ctx.stroke();
    ctx.restore();
  }
  hline(0.35, "rgba(248,81,73,.55)", [4,4]);
  hline(1.0,  "rgba(139,148,158,.45)", [2,5]);

  var n = samples.length;
  if (n < 2){
    ctx.fillStyle = C.faint; ctx.textAlign = "left";
    ctx.fillText("等待开发板上报…", padL+6*dpr, padT+14*dpr);
    return;
  }

  /* 面积 + 折线 */
  var pts = samples.map(function(s, i){
    var g = Math.sqrt(s[1]*s[1] + s[2]*s[2] + s[3]*s[3]);
    return [padL + i/(n-1)*plotW, yOf(g), g];
  });

  var grad = ctx.createLinearGradient(0, padT, 0, padT+plotH);
  grad.addColorStop(0, "rgba(88,166,255,.42)");
  grad.addColorStop(1, "rgba(88,166,255,.02)");
  ctx.beginPath(); ctx.moveTo(pts[0][0], padT+plotH);
  pts.forEach(function(p){ ctx.lineTo(p[0], p[1]); });
  ctx.lineTo(pts[n-1][0], padT+plotH); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();

  ctx.beginPath();
  pts.forEach(function(p, i){ i ? ctx.lineTo(p[0],p[1]) : ctx.moveTo(p[0],p[1]); });
  ctx.strokeStyle = C.blue; ctx.lineWidth = 2*dpr;
  ctx.lineJoin = "round"; ctx.stroke();

  /* 末端点 */
  var last = pts[n-1];
  ctx.beginPath(); ctx.arc(last[0], last[1], 4*dpr, 0, 6.2832);
  ctx.fillStyle = C.blue; ctx.fill();
  ctx.beginPath(); ctx.arc(last[0], last[1], 8*dpr, 0, 6.2832);
  ctx.fillStyle = "rgba(88,166,255,.22)"; ctx.fill();

  /* 末端读数胶囊：半透明深色底 + 当前活动色描边，贴在末端点上方，不出右界 */
  var label = last[2].toFixed(2) + " g";
  ctx.font = (11*dpr)+"px Consolas,monospace";
  var tw = ctx.measureText(label).width;
  var lx = Math.max(padL + 4*dpr, Math.min(last[0] - tw/2, W - padR - tw - 12*dpr));
  var ly = Math.max(padT, padT - 2*dpr);
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(lx - 6*dpr, ly, tw + 12*dpr, 16*dpr, 8*dpr);
  else ctx.rect(lx - 6*dpr, ly, tw + 12*dpr, 16*dpr);
  ctx.fillStyle = "rgba(13,18,25,.88)";
  ctx.fill();
  ctx.lineWidth = 1*dpr;
  ctx.strokeStyle = lastActColor.replace(")", ",.45)").replace("rgb", "rgba");
  ctx.stroke();
  ctx.fillStyle = C.dim; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(label, lx, ly + 8*dpr);
}
addEventListener("resize", fitCanvas);

/* ---------------- 姿态球与三轴条 ---------------- */
var BUBBLE_MAX = 62;                     /* 1g 对应的像素偏移（相对球半径 50%） */
function setBall(x, y, mag){
  var bx = Math.max(-1, Math.min(1, x)) * BUBBLE_MAX;
  var by = Math.max(-1, Math.min(1, y)) * BUBBLE_MAX;
  var key = bx.toFixed(0)+","+by.toFixed(0);
  var horiz = Math.sqrt(x*x + y*y);
  /* 与板端同一套判据：mag<0.05 是还没有效读数（灰），<0.35 才是失重红 */
  var color = (mag < 0.05) ? C.faint
            : (mag < 0.35) ? C.red
            : (horiz < 0.15) ? C.green
            : (horiz < 0.7) ? C.amber : C.red;
  if (key !== lastBall || color !== lastBallColor){
    lastBall = key;
    lastBallColor = color;
    var d = $("ball");
    d.style.transform = "translate(" + bx.toFixed(1) + "px," + by.toFixed(1) + "px)";
    d.style.background = color;
    d.style.boxShadow = "0 0 18px -2px " + color;
  }
}
function setTilt(z, mag){
  var t, color;
  if (mag < 0.05){ t = "无读数"; color = C.faint; }
  else if (mag < 0.35){ t = "失重"; color = C.red; }
  else {
    var c = Math.min(1, Math.abs(z)/mag);
    var deg = Math.round(Math.acos(c)*180/Math.PI);
    t = "倾角 " + deg + "°";
    color = deg === 0 ? C.green : (deg < 40 ? C.amber : C.red);
  }
  if (t !== lastTilt){
    lastTilt = t;
    var el = $("tilt");
    el.textContent = t;
    el.style.color = color;
  }
}
function setBar(i, v){
  var el = $(["bx","by","bz"][i]), vl = $(["vx","vy","vz"][i]);
  var pct = Math.min(Math.abs(v), 2)/2*50;      /* 对称：从中点往两边长 */
  el.style.width = pct + "%";
  el.style.left = (v >= 0 ? 50 : 50-pct) + "%";
  var ac = [C.red, C.green, C.blue][i];
  el.style.background = ac;
  vl.style.color = ac;                           /* 数值与 X/Y/Z 标签同色，与板端一致 */
  vl.textContent = (v >= 0 ? "+" : "") + v.toFixed(2);
}

/* ---------------- 指令面板 ---------------- */
var busy = false, devOnline = false, ledSteady = false, cmdNames = [], cmdsSeen = [];
var STNAME = { queued:"排队中", sent:"已下发", done:"已完成", failed:"失败", timeout:"超时" };

function renderButtons(){
  var host = $("cmdbts");
  if (host.dataset.built === cmdNames.join(",")) { syncButtons(); return; }
  host.dataset.built = cmdNames.join(",");
  host.innerHTML = cmdNames.map(function(n){
    var ui = CMD_UI[n] || { label:n };
    return '<button data-cmd="' + n + '"' + (ui.primary ? ' class="primary"' : '') + '>'
         + ui.label + '</button>';
  }).join(" ") || '<span class="empty">服务端没有开放任何远程指令</span>';
  Array.prototype.forEach.call(host.querySelectorAll("button"), function(b){
    b.onclick = function(){ sendCmd(b.dataset.cmd, b); };
  });
  syncButtons();
}
function syncButtons(){
  var host = $("cmdbts");
  Array.prototype.forEach.call(host.querySelectorAll("button"), function(b){
    var n = b.dataset.cmd, ui = CMD_UI[n] || {};
    b.disabled = busy || !devOnline;
    if (ui.toggle && n === "led_set"){
      b.textContent = ledSteady ? "LED 熄灭" : "LED 常亮";
    } else if (n === "capture_once" && busy){
      b.textContent = "等待板子回传…";
    } else {
      b.textContent = ui.label || n;
    }
  });
}
function sendCmd(name, btn){
  var ui = CMD_UI[name] || {};
  var params = ui.toggle && name === "led_set" ? {on: !ledSteady} : (ui.params || {});
  if (btn) btn.disabled = true;
  $("cmdhint").textContent = "";
  fetch("/api/command", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({name:name, params:params})
  }).then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok){ $("cmdhint").textContent = "下发失败：" + (d.error || "未知错误"); return; }
    $("cmdhint").textContent = "已下发 " + d.id + (d.device_online ? "" : "（注意：板子当前不在线）");
    pull();
  }).catch(function(e){
    $("cmdhint").textContent = "下发失败：" + e;
  }).then(function(){ syncButtons(); });
}

function renderCmds(cmds){
  cmdsSeen = cmds || [];
  var el = $("cmds");
  if (!cmds || !cmds.length){ el.innerHTML = '<div class="empty">还没有下发过指令。</div>'; return; }
  el.innerHTML = cmds.slice(0,8).map(function(c){
    var r = c.result || {}, extra = "";
    if (c.state === "done" && r.x !== undefined){
      extra = '<span class="meta mono">x=' + (+r.x).toFixed(3) + ' y=' + (+r.y).toFixed(3)
            + ' z=' + (+r.z).toFixed(3) + ' · ' + (r.n||0) + ' 样本 · '
            + Math.round(r.ms||0) + ' ms · σ=' + (+(r.std||0)).toFixed(4) + ' g</span>';
    } else if (c.state === "failed"){
      extra = '<span class="meta mono">' + (r.err || "") + '</span>';
    }
    var lat = (c.sent && c.done)
      ? '<span class="meta mono">往返 ' + Math.round((c.done-c.sent)*1000) + ' ms</span>' : "";
    var ps = (c.params && Object.keys(c.params).length)
      ? '<span class="meta mono">' + JSON.stringify(c.params) + '</span>' : "";
    return '<div class="item"><span class="st st-' + c.state + '">' + (STNAME[c.state]||c.state) + '</span>'
         + '<span class="name">' + c.name + '</span>' + ps
         + '<span class="meta mono">' + c.id + '</span>' + lat + extra + '</div>';
  }).join("");

  busy = cmds.some(function(c){ return c.state === "queued" || c.state === "sent"; });
  var lastSet = cmds.filter(function(c){ return c.name === "led_set" && c.state === "done"; })[0];
  if (lastSet && lastSet.params) ledSteady = !!lastSet.params.on;
  syncButtons();
}

/* ---------------- 事件流 ---------------- */
var evHTML = "";
function renderEvents(evts){
  if (!evts || !evts.length) return;
  var h = evts.slice(0,40).map(function(ev){
    var k = "k-" + (ev.kind || "info");
    var time = new Date(ev.ts*1000).toLocaleTimeString("zh-CN", {hour12:false});
    return '<div class="item"><span class="k ' + k + '"></span><span class="t">' + time + '</span>'
         + '<span>' + ev.text + '</span></div>';
  }).join("");
  if (h !== evHTML){
    evHTML = h;
    $("feed").innerHTML = h;
    $("evcount").textContent = "最近 " + evts.length + " 条";
  }
}

/* ---------------- 主刷新 ---------------- */
var aiReply = "";
function render(s){
  devOnline = !!s.device_online;
  var dev = $("dev");
  dev.className = "pill " + (devOnline ? "on" : "off");
  dev.innerHTML = "<i></i>" + (devOnline ? "在线" : "离线");

  $("src").textContent = (s.source && s.source !== "-") ? s.source : "—";
  $("hzp").textContent = s.sample_hz ? (s.sample_hz + " Hz") : "— Hz";
  $("postsp").textContent = "↑" + (s.posts_ok !== undefined ? s.posts_ok : 0) + " 帧";
  $("steps").textContent = s.step_count;
  $("shakes").textContent = s.shake_count;
  $("mode").textContent = (s.ai_mode || "规则AI") + (s.ai_pending ? " · 生成中…" : "");

  var a = classify(s.activity || "");
  if (a.word !== lastAct){
    lastAct = a.word;
    var e = $("act");
    e.textContent = a.word;
    e.style.color = a.color;
    $("ring").style.stroke = a.color;
    /* hero 卡片边框/光晕染成活动色（CSS 变量联动），活动词淡入一次 */
    document.querySelector(".card.hero").style.setProperty("--act-rgb", a.rgb);
    lastActColor = "rgb(" + a.rgb + ")";
    e.classList.remove("pop");
    void e.offsetWidth;
    e.classList.add("pop");
  }

  /* 有真回复才用琥珀色并加 has；空回复回到引导语（dim），与板端一致 */
  if (s.ai_reply && s.ai_reply !== aiReply){
    aiReply = s.ai_reply;
    $("reply").textContent = aiReply;
  } else if (!s.ai_reply && aiReply !== ""){
    aiReply = "";
    $("reply").textContent = "按板子 BOOT 键即可向服务器 AI 提问。";
  }
  var rbox = $("replybox");
  rbox.className = "reply" + (s.ai_pending ? " pending" : "") + (s.ai_reply ? " has" : "");

  if (s.latest){
    var x = s.latest[1], y = s.latest[2], z = s.latest[3];
    var mag = Math.sqrt(x*x + y*y + z*z);
    setRing(mag); setBall(x, y, mag); setTilt(z, mag);
    setBar(0, x); setBar(1, y); setBar(2, z);
  }
  renderCmds(s.commands);
  renderEvents(s.events);
}

function pull(){
  fetch("/api/latest").then(function(r){ return r.json(); }).then(function(s){
    samples = s.samples || [];
    drawChart();
    render(s);
  }).catch(function(){});
}

function stream(){
  var es = new EventSource("/api/stream");
  es.onmessage = function(e){
    var s = JSON.parse(e.data);
    render(s);
  };
  es.onerror = function(){ es.close(); setTimeout(stream, 2000); };
}

/* ---------------- 启动 ---------------- */
$("host").textContent = location.host;
fitCanvas();
fetch("/api/logs").then(function(r){ return r.json(); }).then(function(l){
  if (l && l.dir) $("logdir").textContent = "落盘 " + l.dir;
}).catch(function(){});
fetch("/api/commands").then(function(r){ return r.json(); }).then(function(d){
  cmdNames = d.names || ["capture_once"];
  $("names").textContent = cmdNames.join(" / ");
  renderButtons();
  renderCmds(d.commands);
}).catch(function(){
  cmdNames = ["capture_once"];
  renderButtons();
});
pull();
setInterval(pull, 2000);
stream();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main():
    global LOGGER, CMD_TIMEOUT_S

    ap = argparse.ArgumentParser(description="AI 交互课 · PC 服务器（遥测接收 + 远程指令 + 网页仪表盘）")
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
    ap.add_argument("--cmd-timeout", type=float, default=CMD_TIMEOUT_S,
                    help="远程指令下发后多久没回传结果就判超时（秒），默认 %.0f" % CMD_TIMEOUT_S)
    args = ap.parse_args()

    CMD_TIMEOUT_S = max(1.0, args.cmd_timeout)
    LOGGER = JsonlLogger(args.data_dir, retain_days=args.retain_days,
                         log_telemetry=not args.no_log_telemetry, log_hz=args.log_hz)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    # 后台心跳：设备超时判定离线 + 命令超时看护
    def watchdog():
        while True:
            time.sleep(1.0)
            with LOCK:
                online = (time.time() - STATE["last_post"]) < DEVICE_TIMEOUT
                if STATE["device_online"] and not online:
                    push_event("info", "开发板连接超时，已标记离线")
                STATE["device_online"] = online
            for name in expire_commands():
                push_event("cmd", "命令 %s 超时（%.0f 秒内没有回传结果）"
                           % (name, CMD_TIMEOUT_S))
    threading.Thread(target=watchdog, daemon=True).start()

    llm = "大模型已配置 (%s)" % os.environ.get("RW1_LLM_MODEL", "?") \
        if os.environ.get("RW1_LLM_API_KEY") else "本地规则AI（可配 RW1_LLM_API_KEY 升级）"
    print("=" * 66)
    print(" AI 交互课 · PC 服务器已启动")
    print("   仪表盘:  http://localhost:%d/" % args.port)
    print("   遥测:    POST http://<本机IP>:%d/api/telemetry" % args.port)
    print("   指令:    POST http://<本机IP>:%d/api/command   {\"name\":\"capture_once\"}" % args.port)
    print("   AI模式:  %s" % llm)
    print("   指令超时: %.0f 秒" % CMD_TIMEOUT_S)
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

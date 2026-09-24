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
from urllib.parse import unquote

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
DT_NOMINAL = 0.01         # 标称采样间隔（对应板端 CONFIG_RW1_SAMPLE_PERIOD_MS=10）
DT_TRUST_MAX = 0.05       # 超过这个推断间隔就认为该帧晚到了、时间轴不可信
MOTION_STD = 0.06         # "算得上在动"的短窗标准差阈值（g）
SHAKE_ZCR = 3.5           # 晃动判定：|a| 起伏频率高于该值（Hz）。步行 1.5~2.5Hz，
                          # 晃动 3~8Hz —— 只靠幅度分不开两者（走路也有 0.5g 起伏），
                          # 必须靠频率。
WALK_ZCR_MIN = 1.2        # 步行判定：起伏频率下限（Hz）
SHAKE_MIN_GAP_S = 0.6     # 晃动事件的最小间隔（秒），避免一次晃动记成多次
FREEFALL_G = 0.35         # 失重判定：合加速度阈值（g）
FREEFALL_MIN_S = 0.05     # 失重判定：至少持续这么久才算疑似跌落（秒）

# ---- 远程命令（第 2 周：Web 下发「采集一次」，按 request_id 反馈结果）----
# 第 3 周在此基础上加了两个「物理反馈」指令：led_blink / led_set —— 板载 LED 在 GPIO3。
CMD_TIMEOUT_S = 10.0      # 命令下发后多久没收到结果就判超时
CMD_MAX_HISTORY = 20      # 保留最近多少条命令供网页显示
CMD_NAMES = ("capture_once", "led_blink", "led_set", "set_orient",
             "set_config", "sd_format", "sd_ls", "sd_rm",
             "cam_capture", "cam_stream")   # 白名单（不认的名字直接 400）
# set_orient 是"远程改方向档位"，与板端长按 BOOT 等价 —— 网页上点选比盲按 N 次靠谱。

LED_MAX_BLINKS = 12       # 一次 led_blink 最多闪几下（板端也会再夹一道）
LED_PATTERNS = ("alert", "ack", "error")   # led_blink 的语义图案（板端映射到预置图案）
FALL_AUTO_ALERT = True    # 判定跌落时自动下发 LED 告警（远端物理反馈）

ACTIVITY_IDLE = "等待数据…"

# --------------------------------------------------------------------------
# 全局状态
#
# 单板时代只有一个 STATE 字典；多板场景（一个班 20 块板连同一个服务器）必须按设备
# 分开，否则两块板的姿态球会互相覆盖 —— 那正是"配网做完了，结果 20 块板一起挤进
# 同一个单设备仪表盘"的尴尬局面（PROPOSAL §4.1）。
#
# 分片原则：**一把锁保护全部设备**。每台设备的数据量极小（一个 8 秒窗口 + 200 条
# 事件），拆成多把锁只会引入死锁风险、换不到任何并发收益；而跨设备操作
# （命令超时看护、设备列表）本来就要同时看多台，一把锁反而更简单。
# --------------------------------------------------------------------------
LOCK = threading.Lock()

DEFAULT_DEVICE = "-"        # 不带 device 字段的老固件都归到这里（向后兼容）
DEVICE_ID_MAX = 32          # 设备名长度上限，够写「第三组-07」这种
MAX_DEVICES = 32            # 同时在册设备上限；超出时淘汰最久没上报的那台


class Device:
    """一台板子在服务端的全部状态。

    刻意用 __slots__：设备数上限 32，但每台都可能持有 1200 样本的窗口，
    固定布局能少一层每实例字典。
    """

    __slots__ = ("id", "samples", "shake_times", "events", "commands",
                 "queue", "seq", "st")

    def __init__(self, did):
        self.id = did
        self.samples = deque(maxlen=MAX_WINDOW)   # [(ts, x, y, z)]，x/y 为屏幕坐标
        self.shake_times = deque(maxlen=256)      # 晃动事件时间戳（窗口内计数用）
        self.events = deque(maxlen=200)           # 事件流（网页显示）
        self.commands = {}                        # request_id -> 命令记录
        self.queue = deque()                      # 待下发的 request_id（FIFO）
        self.seq = 0                              # 生成可读 request_id 的递增序号
        self.st = {
            "device": did,
            "device_online": False,
            "last_post": 0.0,
            "posts_ok": 0,                        # 成功上报帧数（网页顶栏「↑N 帧」）
            "source": "-",
            "orient": None,                       # 板端上报的方向档位 oN（老固件没有 → None）
            "latest": None,                       # 最近一帧原始数据
            "activity": ACTIVITY_IDLE,
            "step_count": 0,
            "shake_count": 0,
            "ai_reply": "",
            "ai_pending": False,
            "ai_mode": "规则AI",
            "sample_hz": 0.0,                     # 实测采样率（由批量大小与到达间隔推算）
            "dt_trusted": 0.0,                    # 最近一次可信的采样间隔（P1-6：晚到帧不参与）
            "fall_active": False,
            "boot_time": time.time(),
        }


DEVICES = {DEFAULT_DEVICE: Device(DEFAULT_DEVICE)}

LOGGER = None                              # 由 main() 注入的 JsonlLogger

# ---- 摄像头：最近一帧 + 拍照留档 -------------------------------------------
# 板子是 HTTP **客户端**（它没有自己的服务端），所以"实时画面"的做法是
# **板子 POST 帧上来、网页再从这里取** —— 不是网页直连板子。
FRAMES = {}                # device -> {"jpeg": bytes, "ts": float, "n": int}
FRAME_MAX_BYTES = 400 * 1024   # 单帧上限，超了直接丢（防止有人拿它塞垃圾）
SHOTS_DIR = None           # 拍照留档目录（main() 里按 --data-dir 设）
SHOT_MAX = 200             # 每台设备最多留多少张（免得把磁盘塞满）


def safe_name(s):
    """把设备名变成安全的目录/文件名：只留字母数字和 - _ .，其余换成 _。"""
    keep = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(s))
    return (keep[:48] or "unknown")


def clean_device_id(raw):
    """把上报/查询里的设备名收敛成安全的 key。

    **允许中文**：课堂里学生多半会填「第三组-07」这种名字，用 ASCII 白名单会把它们
    整串吃成空串、全班退化成同一个设备。JSON 与 SSE 对任意 Unicode 都是安全的，
    HTML 侧的转义由前端负责，所以这里只做三件真正必要的事：
      - 去掉不可打印字符（换行/制表/NUL 会破坏 SSE 的「一行一个 data:」分帧）
      - 把连续空白压成一个空格（否则「rw1  07」和「rw1 07」是两台板）
      - 限长（避免一个 4 MB 的畸形 device 字段把内存顶爆）
    """
    if raw is None:
        return DEFAULT_DEVICE
    s = raw if isinstance(raw, str) else str(raw)
    s = "".join(ch for ch in s if ch.isprintable())
    s = " ".join(s.split())[:DEVICE_ID_MAX]
    return s or DEFAULT_DEVICE


def get_device(did):
    """按 id 取设备，没有就新建一台。**调用方必须已持有 LOCK。**"""
    dev = DEVICES.get(did)
    if dev is None:
        if len(DEVICES) >= MAX_DEVICES:
            # 淘汰最久没上报的那台。正在上报的设备永远不会被选中，
            # 因为它的 last_post 一定比谁都新。
            victim = min(DEVICES.values(), key=lambda d: d.st["last_post"])
            if victim.id != did:
                DEVICES.pop(victim.id, None)
        dev = Device(did)
        DEVICES[did] = dev
    return dev


def pick_device(did=None):
    """解析「这次要操作哪台设备」。**调用方必须已持有 LOCK。**

    - 给了 did 且在册 → 就用它
    - 没给 did → 用**最近上报过**的那台（老固件不带 device 字段、单板场景下
      /api/latest 与 /api/commands 都不带 ?device=，向后兼容全靠这条）
    - 给了 did 但不在册（拼错、或刚被淘汰）→ 也退回「最近上报过」的那台；
      响应里会带上真实的 device id，前端据此自我纠正。
      比返回 404 让整个页面白屏友好得多。
    """
    if did is not None:
        dev = DEVICES.get(did)
        if dev is not None:
            return dev
    live = [d for d in DEVICES.values() if d.st["last_post"] > 0.0]
    if live:
        return max(live, key=lambda d: d.st["last_post"])
    return DEVICES[DEFAULT_DEVICE]


def snapshot(dev):
    """一台设备的完整快照。**调用方必须已持有 LOCK。**"""
    snap = dict(dev.st)
    snap["events"] = list(dev.events)
    snap["commands"] = commands_snapshot(dev)
    return snap


def devices_snapshot(now=None):
    """所有「上报过」的设备的一句话摘要，给仪表盘的设备列表用。

    **调用方必须已持有 LOCK。** 从没上报过的占位设备不列出来 —— 否则单板场景下
    页面会多出一个空的「-」条目。
    """
    now = time.time() if now is None else now
    out = []
    for dev in DEVICES.values():
        st = dev.st
        if st["last_post"] <= 0.0:
            continue
        out.append({
            "id": dev.id,
            "online": bool(st["device_online"]),
            "age": round(max(0.0, now - st["last_post"]), 1),
            "source": st["source"],
            "orient": st.get("orient"),
            "activity": st["activity"],
            "sample_hz": st["sample_hz"],
            "step_count": st["step_count"],
            "shake_count": st["shake_count"],
            "posts_ok": st["posts_ok"],
        })
    # 在线的排前面，同状态按名字排 —— 列表顺序稳定，刷新时不会跳来跳去
    out.sort(key=lambda d: (not d["online"], d["id"]))
    return out


def query_param(query, name):
    """从原始查询串里取一个参数（只需要一层，不必上 parse_qs）。

    用 unquote 而不是 unquote_plus：前端用 encodeURIComponent 编码，
    空格会变成 %20；把裸 '+' 当空格解释反而会把设备名里的 '+' 改掉。
    """
    for part in (query or "").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if key == name:
            return unquote(value)
    return None


def push_event(dev, kind, text):
    dev.events.appendleft({"ts": time.time(), "kind": kind, "text": text})
    if LOGGER is not None:
        LOGGER.event(dev.id, kind, text)


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
def clamp_int(value, lo, hi, dflt):
    """把外部传来的数字夹进 [lo, hi]；不是数字就用默认值。"""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return dflt
    return max(lo, min(hi, v))


def sanitize_params(name, params):
    """把网页/调用方传来的参数夹到安全范围。

    服务端先夹一道，板端还会再夹一道 —— 外部输入不信任，谁也别指望对方把好关。
    """
    p = params if isinstance(params, dict) else {}
    if name == "led_blink":
        out = {
            "n": clamp_int(p.get("n"), 1, LED_MAX_BLINKS, 3),
            "on_ms": clamp_int(p.get("on_ms"), 20, 5000, 80),
            "off_ms": clamp_int(p.get("off_ms"), 20, 5000, 80),
        }
        # 可选的语义图案：板端会映射到带含义的预置闪烁（告警/确认/错误），
        # 比只丢一个"闪 N 次"更能表达意图。不认的值直接丢掉，不报错。
        pat = str(p.get("pattern", "")).strip().lower()
        if pat in LED_PATTERNS:
            out["pattern"] = pat
        return out
    if name == "led_set":
        v = p.get("on", True)
        if isinstance(v, str):
            v = v.strip().lower() in ("1", "true", "yes", "on")
        return {"on": bool(v)}
    if name == "set_orient":
        return {"o": clamp_int(p.get("o"), 0, 15, 0)}
    if name == "cam_stream":
        # 只有开/关两种状态，布尔化即可（别把任意值透传下去）
        return {"on": bool(p.get("on"))}
    if name == "sd_rm":
        # 文件名：只放行"根目录下的文件名"，不接受路径分隔符 —— 板端还会再拦一道，
        # 但服务端不该把明显越界的东西发下去。长度按板端的 s_rm_name[64] 夹。
        fn = p.get("name")
        if not isinstance(fn, str):
            return {}
        fn = fn.strip()
        if not fn or "/" in fn or "\\" in fn or ".." in fn:
            return {}
        return {"name": fn[:63]}
    if name == "set_config":
        # 网页"板子设置"卡片：只放行这四项，长度按板端 NVS 的字段上限夹
        # （ssid 32 / pass 64 / url 127），空串一律丢掉 —— 板端把"没传"
        # 当作"不改这一项"，传空串反而会被当成"清空"。
        out = {}
        for key, lim in (("ssid", 32), ("pass", 64), ("url", 127)):
            v = p.get(key)
            if isinstance(v, str):
                v = v.strip()
                if v:
                    out[key] = v[:lim]
        if "period_ms" in p:
            # 下限 50ms = 20Hz（板端夹同样的范围，两边保持一致）
            out["period_ms"] = clamp_int(p.get("period_ms"), 50, 2000, 500)
        return out
    return {}


def _mark(rec, state, ts=None):
    """改命令状态并追加迁移历史。**调用方必须已持有 LOCK。**

    为什么要有 history：`sent` 只在下发帧和回传帧之间存活约一个遥测周期
    （默认 0.5 s），外部用轮询去捕捉这个中间态本质是竞态断言——测试会因为
    采样时机而随机失败。有了历史，断言改成查表即可，网页也能画出时间线。
    """
    rec["state"] = state
    rec["history"].append([state, ts if ts is not None else time.time()])


def new_command(dev, name, params=None):
    """给某台设备建一条待下发的命令，返回 request_id。"""
    with LOCK:
        dev.seq += 1
        cid = "c-%d-%d" % (int(time.time()), dev.seq)
        dev.commands[cid] = {
            "id": cid,
            "name": name,
            "params": sanitize_params(name, params),
            "state": "queued",      # queued / sent / done / failed / timeout
            "history": [["queued", time.time()]],
            "created": time.time(),
            "sent": None,
            "done": None,
            "result": None,
        }
        dev.queue.append(cid)
        # 只保留最近 CMD_MAX_HISTORY 条，避免长跑时无限增长
        while len(dev.commands) > CMD_MAX_HISTORY:
            oldest = min(dev.commands, key=lambda k: dev.commands[k]["created"])
            dev.commands.pop(oldest, None)
            try:
                dev.queue.remove(oldest)
            except ValueError:
                pass
    return cid


def take_command_for_board(dev):
    """取一条该设备待下发的命令并标记为 sent。**调用方必须已持有 LOCK。**

    返回给板端的 {"id","name","params"}，或 None。一次只发一条，
    板子也一次只执行一条，语义简单、不会乱序。
    """
    while dev.queue:
        cid = dev.queue.popleft()
        rec = dev.commands.get(cid)
        if rec is None or rec["state"] != "queued":
            continue
        rec["sent"] = time.time()
        _mark(rec, "sent", rec["sent"])
        return {"id": cid, "name": rec["name"], "params": rec["params"]}
    return None


def apply_command_result(dev, res):
    """处理某台设备回传的 result，返回一句给人看的事件文本（无关/不匹配则 None）。

    注意本函数内部会取 LOCK，不能在持有 LOCK 时调用。
    """
    if not isinstance(res, dict):
        return None
    cid = str(res.get("id", ""))[:32]
    keep = ("id", "ok", "ms", "n", "x", "y", "z", "std", "err", "note")
    with LOCK:
        rec = dev.commands.get(cid)
        if rec is None:
            return None                      # 可能是被淘汰的老命令，静默忽略
        rec["done"] = time.time()
        _mark(rec, "done" if res.get("ok") else "failed", rec["done"])
        rec["result"] = {k: res[k] for k in keep if k in res}
        latency = (rec["done"] - rec["sent"]) if rec["sent"] else 0.0
        name = rec["name"]
        r = rec["result"]
        if rec["state"] == "done":
            if "x" in r:      # capture_once 有测量值
                text = ("[%s] 命令 %s 完成（往返 %.0f ms）：x=%+.3f y=%+.3f z=%+.3f，%d 样本，标准差 %.4f g"
                        % (dev.id, name, latency * 1000, r.get("x", 0.0), r.get("y", 0.0), r.get("z", 0.0),
                           r.get("n", 0), r.get("std", 0.0)))
            elif name == "sd_ls":
                # 文件列表放 note 里（`SPACE,total,free;NAME|SIZE;...`），
                # 事件流里只报"列了几个"，免得把一长串文件名刷进事件流
                n_files = max(0, r.get("note", "").count(";") - 1)
                text = "[%s] 已读取存储信息（%d 个文件，往返 %.0f ms）" % (
                    dev.id, n_files, latency * 1000)
            elif name == "sd_rm":
                text = "[%s] 命令 %s 完成（往返 %.0f ms）：%s" % (
                    dev.id, name, latency * 1000, r.get("note", ""))
            else:             # led_blink / led_set 只有执行确认
                text = "[%s] 命令 %s 完成（往返 %.0f ms）" % (dev.id, name, latency * 1000)
        else:
            text = "[%s] 命令 %s 执行失败：%s" % (dev.id, name, r.get("err", "未说明"))
    return text


def expire_commands():
    """把长时间没有进展的命令判为超时，返回 [(设备, 命令名)] 列表。

    **`queued` 也要算**：设备离线时下发的命令会一直停在 queued，而每台设备的命令
    上限只有 CMD_MAX_HISTORY 条 —— 长时间离线会把历史全占满，新命令被挤掉；
    设备几小时后重新上线还会被补发一批几小时前的操作（比如"闪灯"）。
    所以 queued 以 created 为基准计时，sent 以 sent 为基准。

    看护必须扫**全部**设备：课堂场景下离线的是其中几块板，不能只盯着当前选中的那台。
    """
    now = time.time()
    expired = []
    with LOCK:
        for dev in list(DEVICES.values()):
            for rec in dev.commands.values():
                if rec["state"] not in ("queued", "sent"):
                    continue
                base = rec["sent"] or rec["created"]
                if (now - base) > CMD_TIMEOUT_S:
                    rec["done"] = now
                    _mark(rec, "timeout", now)
                    expired.append((dev, rec["name"]))
    return expired


def commands_snapshot(dev):
    """按时间倒序返回该设备最近若干条命令的副本（**调用方必须已持有 LOCK**）。"""
    return [dict(r) for r in sorted(dev.commands.values(),
                                    key=lambda r: r["created"], reverse=True)]


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
    def telemetry(self, ts, device, source, pts, dt):
        if not self.log_telemetry or not pts:
            return
        hz = 1.0 / dt if dt > 0 else 0.0
        step = max(1, int(round(hz / self.log_hz))) if hz else 1
        thin = pts[::step]
        self._put({
            "k": "t",
            "ts": round(ts, 3),
            "dev": device,
            "src": source,
            "hz": round(hz, 1),
            "s": [[round(x, 3), round(y, 3), round(z, 3)] for (x, y, z) in thin],
        })

    def event(self, device, kind, text):
        self._put({"k": "e", "ts": round(time.time(), 3), "dev": device,
                   "kind": kind, "text": text})

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


def analyze(dev, now, pts, dt):
    """把一批样本并入该设备的滑动窗口，返回 (活动标签, 事件列表)。"""
    events = []
    prev_activity = dev.st["activity"]
    prev_fall = dev.st.get("fall_active", False)

    n = len(pts)
    for i, (x, y, z) in enumerate(pts):
        dev.samples.append((now - (n - 1 - i) * dt, x, y, z))

    win = [s for s in dev.samples if now - s[0] <= WINDOW_S]
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
    dev.st["step_count"] = steps

    zcr = zero_cross_rate(r_mags, dt)          # 起伏频率，用来区分步行与晃动

    # ---- 晃动：窗口内计数（修复前是开机累计值，文案却说"最近 8 秒内"）----
    shaking = r_std > MOTION_STD and zcr > SHAKE_ZCR
    if shaking:
        if not dev.shake_times or (now - dev.shake_times[-1]) >= SHAKE_MIN_GAP_S:
            dev.shake_times.append(now)
            events.append(("shake", "检测到晃动/敲击（%.1f Hz）" % zcr))
    while dev.shake_times and (now - dev.shake_times[0]) > WINDOW_S:
        dev.shake_times.popleft()
    dev.st["shake_count"] = len(dev.shake_times)

    # ---- 跌落：短窗内的连续失重 ----
    fall_run = max_freefall_run(r_mags, dt)
    fall_active = fall_run > 0
    if fall_active and not prev_fall:
        events.append(("fall", "检测到疑似跌落（失重 %.0f ms）" % (fall_run * dt * 1000)))
    dev.st["fall_active"] = fall_active

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


def ai_summary(dev):
    """基于该设备的统计窗口生成中文摘要（本地模板 / LLM 均可复用）。"""
    now = time.time()
    with LOCK:
        n = len(dev.samples)
        steps = dev.st["step_count"]
        shakes = dev.st["shake_count"]
        act = dev.st["activity"]
        src = dev.st["source"]
        last = dev.st["latest"] or (0, 0, 0, 0)
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


def ask_llm(dev, question):
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
            {"role": "user", "content": "开发板传感器情况：%s\n同学想问：%s" % (ai_summary(dev), question)},
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
        push_event(dev, "warn", "大模型调用失败(%s)，已用本地AI回复" % exc.__class__.__name__)
        return None


def _llm_worker(dev, question):
    """后台线程：调大模型，完成后把结果写回该设备的状态。

    注意 ai_summary() 内部会取 LOCK，必须在进入 with LOCK 之前调用，否则自锁。
    """
    reply = ask_llm(dev, question)
    mode = "大模型" if reply else "规则AI"
    if not reply:
        reply = ai_summary(dev)
    with LOCK:
        dev.st["ai_pending"] = False
        dev.st["ai_reply"] = reply
        dev.st["ai_mode"] = mode
    push_event(dev, "ai" if mode == "大模型" else "warn",
               "%s回复：%s" % (mode, reply[:60]))


def request_ai(dev, question):
    """处理一次板端提问，**立即**返回文本；大模型在后台生成，稍后自动生效。

    板端每个遥测周期都会带回最新的 ai_reply，所以后台结果下一帧就会显示，
    不需要板端等待，也就彻底消除了"板端 3s 超时 vs 服务端 8s 等待"的死结。

    `ai_pending` 是**按设备**的：A 板在等大模型不该让 B 板的提问被吞掉。
    """
    if os.environ.get("RW1_LLM_API_KEY"):
        with LOCK:
            if dev.st["ai_pending"]:
                return dev.st["ai_reply"] or "我还在想上一个问题，稍等一下…"
            dev.st["ai_pending"] = True
            dev.st["ai_reply"] = "正在思考…"
            dev.st["ai_mode"] = "大模型"
        push_event(dev, "ask", "板端提问「%s」→ 已转交大模型" % question)
        threading.Thread(target=_llm_worker, args=(dev, question), daemon=True,
                         name="llm").start()
        return "正在思考…"

    reply = ai_summary(dev)
    with LOCK:
        dev.st["ai_reply"] = reply
        dev.st["ai_mode"] = "规则AI"
    push_event(dev, "ask", "板端提问「%s」→ %s" % (question, reply[:40]))
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
    def _send_vendor_js(self, relpath):
        """只服务仓库里那几个 vendor 文件；路径写死在调用点，不接受外部输入。"""
        full = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            relpath.replace("/", os.sep))
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            self._send(404, json.dumps({"ok": False, "error": str(exc)}))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 内容不会变，缓存久一点；不然每次打开页面都要重下 600KB
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

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
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif path == "/vendor/three.min.js":
            # **白名单**，不做通用静态目录：通用目录会把 server/data/ 里的遥测
            # jsonl（含设备名、事件文本）也一并暴露出去。
            # 用仓库里 vendor 的那份，不引 CDN —— 教室/局域网常常没有外网，
            # 引 CDN 必然白屏（配网页当初就是踩过这个才改成内联的）。
            self._send_vendor_js("docs/vendor/three/build/three.min.js")
        elif path == "/api/devices":
            with LOCK:
                devs = devices_snapshot()
            self._send(200, json.dumps({"ok": True, "devices": devs, "now": time.time()},
                                       ensure_ascii=False))
        elif path == "/api/latest":
            want = query_param(query, "device")
            with LOCK:
                dev = pick_device(clean_device_id(want) if want else None)
                snap = snapshot(dev)
                snap["samples"] = [[round(t, 2), x, y, z]
                                   for (t, x, y, z) in decimate(dev.samples, 240)]
            self._send(200, json.dumps(snap, ensure_ascii=False))
        elif path == "/api/commands":
            want = query_param(query, "device")
            with LOCK:
                dev = pick_device(clean_device_id(want) if want else None)
                cmds = commands_snapshot(dev)
                did = dev.id
            self._send(200, json.dumps({"ok": True, "device": did, "commands": cmds,
                                        "names": list(CMD_NAMES)}, ensure_ascii=False))
        elif path == "/api/logs":
            self._send(200, json.dumps(LOGGER.stats() if LOGGER else {}, ensure_ascii=False))
        elif path == "/api/frame":
            # 网页的 <img src="/api/frame?device=X&t=..."> 直接取这一帧
            # ⚠️ 不能直接用 clean_device_id("")：它对空串会返回 DEFAULT_DEVICE，
            # 于是"没指定设备"会被当成"指定了默认设备"，永远取不到帧（踩过）。
            raw_dev = (query_param(query, "device") or "").strip()
            want = clean_device_id(raw_dev) if raw_dev else ""
            with LOCK:
                if want:
                    fr = FRAMES.get(want) or FRAMES.get(pick_device(want).id)
                else:
                    # 没指定设备 → 给"最近收到的那一帧"（单板场景网页就是这么用的）
                    fr = max(FRAMES.values(), key=lambda x: x["ts"]) if FRAMES else None
            if not fr:
                self._send(404, b"", "image/jpeg")
            else:
                self._send(200, fr["jpeg"], "image/jpeg",
                           extra={"Cache-Control": "no-store"})
        elif path == "/api/shots":
            self._send(200, json.dumps({"ok": True, "shots": self._shots_list(query)},
                                       ensure_ascii=False))
        elif path.startswith("/api/shots/"):
            self._send_shot(path[len("/api/shots/"):])
        elif path == "/api/stream":
            self._sse(query)
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == "/api/telemetry":
            self._telemetry()
        elif path == "/api/command":
            self._command()
        elif path == "/api/frame":
            self._frame(query)
        elif path == "/api/shots/delete":
            self._shot_delete()
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    # ---- camera (板 → 服务器 → 网页) ----------------------------------------
    def _frame(self, query):
        """板子 POST 上来的一帧 JPEG。`?device=X`；`?save=1` 表示这是"拍照"要留档。"""
        dev = clean_device_id(query_param(query, "device") or "-")
        save = query_param(query, "save") == "1"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > FRAME_MAX_BYTES:
            self._send(400, json.dumps({"ok": False, "error": "bad frame size"},
                                       ensure_ascii=False))
            return
        data = self.rfile.read(n)
        # JPEG 头魔数校验：防止把随便什么字节当帧存下来
        if len(data) < 4 or data[:2] != b"\xff\xd8":
            self._send(400, json.dumps({"ok": False, "error": "not a jpeg"},
                                       ensure_ascii=False))
            return
        with LOCK:
            old = FRAMES.get(dev)
            FRAMES[dev] = {"jpeg": data, "ts": time.time(),
                           "n": (old["n"] + 1) if old else 1}
        saved = None
        if save:
            saved = self._shot_save(dev, data)
        self._send(200, json.dumps({"ok": True, "bytes": len(data), "saved": saved},
                                   ensure_ascii=False))

    def _shot_save(self, dev, data):
        """把一帧存成文件。返回文件名（失败返回 None）。"""
        if not SHOTS_DIR:
            return None
        d = os.path.join(SHOTS_DIR, safe_name(dev))
        try:
            os.makedirs(d, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-%03d.jpg" % (int(time.time() * 1000) % 1000)
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data)
            # 超过上限就删最旧的（按文件名排序 = 按时间排序）
            files = sorted(f for f in os.listdir(d) if f.endswith(".jpg"))
            for f in files[:-SHOT_MAX]:
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
            return name
        except OSError as e:
            print("  拍照存盘失败: %s" % e)
            return None

    def _shots_list(self, query):
        dev = clean_device_id(query_param(query, "device") or "")
        out = []
        if SHOTS_DIR and dev:
            d = os.path.join(SHOTS_DIR, safe_name(dev))
            if os.path.isdir(d):
                for f in sorted(os.listdir(d), reverse=True):
                    if f.endswith(".jpg"):
                        try:
                            sz = os.path.getsize(os.path.join(d, f))
                        except OSError:
                            sz = 0
                        out.append({"name": f, "bytes": sz, "device": dev})
        return out

    def _send_shot(self, name):
        """按设备+文件名取一张留档照片。**路径必须夹紧**，不能让它读到目录外。"""
        name = urllib.parse.unquote(name)
        dev, _, fn = name.rpartition("/")
        if (not dev or not fn or "/" in fn or "\\" in fn or ".." in fn
                or ".." in dev or not SHOTS_DIR):
            self._send(404, b"", "image/jpeg")
            return
        full = os.path.join(SHOTS_DIR, safe_name(dev), fn)
        if not os.path.isfile(full):
            self._send(404, b"", "image/jpeg")
            return
        with open(full, "rb") as fh:
            self._send(200, fh.read(), "image/jpeg",
                       extra={"Cache-Control": "max-age=31536000"})

    def _shot_delete(self):
        msg = self._read_json()
        dev = clean_device_id(str(msg.get("device", "")))
        fn = str(msg.get("name", ""))
        if (not dev or not fn or "/" in fn or "\\" in fn or ".." in fn or not SHOTS_DIR):
            self._send(400, json.dumps({"ok": False, "error": "bad request"},
                                       ensure_ascii=False))
            return
        full = os.path.join(SHOTS_DIR, safe_name(dev), fn)
        try:
            os.remove(full)
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        except OSError as e:
            self._send(404, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))

    # ---- command (网页 → 服务器 → 板 → 服务器 → 网页) -----------------------
    def _command(self):
        msg = self._read_json()
        name = str(msg.get("name", "capture_once"))[:32]
        if name not in CMD_NAMES:
            self._send(400, json.dumps(
                {"ok": False, "error": "unknown command: %s" % name}, ensure_ascii=False))
            return
        # 目标设备 —— 三种写法都支持（**单台的响应格式一字未改**，老页面照旧）：
        #   device: "a"          单台
        #   device: ["a","b"]    多台
        #   devices: ["a","b"]   多台（显式；网页"多选/全选"走这个）
        #   都不带                → 最近上报过的那台（单板场景，与以前一致）
        want = msg.get("devices")
        if want is None:
            want = msg.get("device")
        if isinstance(want, (list, tuple)):
            raw = [clean_device_id(x) for x in want]
        elif want:
            raw = [clean_device_id(want)]
        else:
            raw = [None]

        # 去重但保持顺序：全选时列表里可能有重复项，重复下发会让同一台收到两条一样的命令
        seen, targets = set(), []
        for t in raw:
            k = t if t else "-"
            if k in seen:
                continue
            seen.add(k)
            targets.append(t)
        if not targets:
            targets = [None]

        results = []
        for t in targets:
            with LOCK:
                dev = pick_device(t)
                online = dev.st["device_online"]
                target = dev.id
            cid = new_command(dev, name, msg.get("params"))
            with LOCK:
                rec = dev.commands.get(cid)
                pdesc = (" %s" % json.dumps(rec["params"], ensure_ascii=False)) if rec and rec["params"] else ""
            push_event(dev, "cmd", "下发命令 %s%s（%s → %s）" % (name, pdesc, cid, target))
            results.append({"device": target, "id": cid, "device_online": online})

        if len(results) == 1:
            r = results[0]
            self._send(200, json.dumps(
                {"ok": True, "id": r["id"], "device": r["device"],
                 "device_online": r["device_online"]}, ensure_ascii=False))
        else:
            self._send(200, json.dumps(
                {"ok": True, "batch": True, "count": len(results), "results": results},
                ensure_ascii=False))

    # ---- telemetry (板 → 服务器 → 板) --------------------------------------
    def _telemetry(self):
        msg = self._read_json()
        now = time.time()

        pts = self._parse_points(msg)
        if not pts:
            self._send(400, json.dumps({"ok": False, "error": "bad payload"}))
            return

        # 设备名是外部输入：清洗、限长，然后按它分片。老固件不带这个字段，
        # clean_device_id(None) → "-"，全部落进默认设备，行为与单板时代一致。
        dev = None
        with LOCK:
            dev = get_device(clean_device_id(msg.get("device")))
            st = dev.st
            prev_post = st["last_post"]
            was_online = st["device_online"]
            # 采样间隔由"本批样本数 / 两批到达的间隔"自校准，不依赖板端上报
            # dt 由"本批样本数 / 两批到达的间隔"自校准。但**晚到的帧不可信**：
            # 若某帧因网络抖动晚到 3 秒，50 个样本的推断间隔会被算成 60 ms，
            # 这批样本就被摊到 3 秒的时间轴上 —— MOTION_WINDOW_S(0.6 s) 的短窗里
            # 只剩最后 1~2 个样本，晃动/跌落全部漏检，sample_hz 也会跳变。
            # 所以推断值明显偏大时沿用上一帧的可信值，不让它污染时间轴（P1-6）。
            if prev_post > 0 and len(pts) > 1:
                dt_est = (now - prev_post) / float(len(pts))
                dt_est = min(0.6, max(0.002, dt_est))
                if dt_est > DT_TRUST_MAX:
                    dt = st["dt_trusted"] or DT_NOMINAL
                else:
                    dt = dt_est
                    st["dt_trusted"] = dt_est
            else:
                dt = DT_NOMINAL
            st["sample_hz"] = round(1.0 / dt, 1)

            st["device_online"] = True
            st["last_post"] = now
            st["posts_ok"] += 1
            st["source"] = str(msg.get("source", "-"))[:24]
            # 方向档位：板端每帧都带（老固件不带 → None，网页就不显示这一项）
            o_raw = msg.get("o")
            if isinstance(o_raw, bool):
                o_raw = None
            if isinstance(o_raw, (int, float)) and 0 <= int(o_raw) <= 15:
                st["orient"] = int(o_raw)
            st["latest"] = (now, pts[-1][0], pts[-1][1], pts[-1][2])
            if not was_online:
                push_event(dev, "info", "设备 %s 已连接" % dev.id)

            activity, events = analyze(dev, now, pts, dt)
            st["activity"] = activity
            for kind, text in events:
                push_event(dev, kind, text)
            fell = any(k == "fall" for k, _ in events)
            src = st["source"]
            # 有排队中的命令就搭这一帧的响应发下去（一次一条）
            cmd = take_command_for_board(dev)

        if LOGGER is not None:
            LOGGER.telemetry(now, dev.id, src, pts, dt)

        # 板端回传的上一条命令结果（必须在 LOCK 之外处理，apply_command_result 内部取锁）
        cmd_text = apply_command_result(dev, msg.get("result"))
        if cmd_text:
            push_event(dev, "cmd", cmd_text)

        # 按键触发（第 3 周）：板子把 BOOT 按下的次数带上来，
        # 这样网页/日志能看到"物理动作真的发生了"，而不只是间接看到 ask。
        btn = msg.get("btn")
        if isinstance(btn, (int, float)) and btn > 0:
            push_event(dev, "btn", "设备 %s 的 BOOT 键按下 %d 次" % (dev.id, int(btn)))

        # 远端物理反馈闭环：判定跌落就自动下发 LED 告警。
        # 必须在 LOCK 之外 —— new_command 内部要取锁，在锁里调用会死锁。
        # 告警只发给摔的那台板：课堂里 20 块板同时闪灯就成噪音了。
        if fell and FALL_AUTO_ALERT:
            aid = new_command(dev, "led_blink", {"pattern": "alert"})
            push_event(dev, "alert", "判定跌落，自动下发 LED 告警（%s）" % aid)

        reply = ""
        if msg.get("ask"):                  # 板子 BOOT 键 → 请求一次 AI 交互
            question = str(msg.get("q", "我现在的状态怎么样？"))[:200]
            reply = request_ai(dev, question)   # 立刻返回；大模型走后台线程

        with LOCK:
            final_reply = reply or dev.st["ai_reply"]
            pending = dev.st["ai_pending"]

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
    def _sse(self, query=""):
        want = query_param(query, "device")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                with LOCK:
                    now = time.time()
                    # 每轮重新解析一次设备：选中的板子被淘汰/改名时，
                    # 这里会自动退回"最近上报过的那台"，前端看到 device 变了就跟着切。
                    dev = pick_device(clean_device_id(want) if want else None)
                    snap = snapshot(dev)
                    snap["devices"] = devices_snapshot(now)
                    snap["sample"] = list(dev.samples[-1]) if dev.samples else None
                snap["now"] = now
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
<!-- 空 favicon：不写这行浏览器会去请求 /favicon.ico，服务端没有这条路由，
     于是每次打开页面都留一条 404 在控制台（2026-09-23 E2E 抓到的）。 -->
<link rel="icon" href="data:,">
<script src="/vendor/three.min.js"></script>
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

/* ---------- 设备列表（多板场景） ---------- */
/* 只有 1 台板时不显示这一块 —— 单板课堂不该多出一张只有一个按钮的卡片 */
.devlist{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
.dev{
  display:flex;flex-direction:column;gap:5px;text-align:left;
  padding:11px 13px;border-radius:12px;cursor:pointer;
  border:1px solid var(--line);background:var(--card-hi);
  transition:border-color .18s,background .18s,box-shadow .18s;
}
.dev:hover{background:#222c3c;border-color:#37445a}
.dev.sel{border-color:rgba(88,166,255,.7);background:#12202f;box-shadow:0 0 0 1px rgba(88,166,255,.22)}
.dev .row1{display:flex;align-items:center;gap:8px;min-width:0}
.dev .dot{width:8px;height:8px;border-radius:50%;flex:none}
.dev .dot.off{animation:breathe 1.6s ease-in-out infinite}
.dev .did{font-weight:650;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dev .meta{font-size:11px;color:var(--faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

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
/* 存储占用圆环：比一条横条直观，而且点一下就能进管理 */
.ringwrap{display:flex;align-items:center;gap:16px;margin-top:6px}
.ring{position:relative;width:104px;height:104px;flex:0 0 auto;cursor:pointer;
  border-radius:50%;transition:transform .15s}
.ring:hover{transform:scale(1.04)}
.ring svg{display:block;transform:rotate(-90deg)}
.ring .ringtxt{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;line-height:1.15}
.ring .ringtxt b{font-size:20px;font-weight:650}
.ring .ringtxt span{font-size:11px;color:var(--faint,#8892a4)}
.ringmeta{font-size:12px;line-height:1.7}
.ringmeta .k{color:var(--faint,#8892a4)}
/* 摄像头：左画面右按钮，窄屏自动堆叠 */
.camwrap{display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:14px;margin-top:6px}
.camview{background:#0b0f16;border-radius:10px;overflow:hidden;aspect-ratio:4/3;
  display:flex;align-items:center;justify-content:center;position:relative}
.camview img{width:100%;height:100%;object-fit:contain;display:block}
.camview .nosig{color:#5b6678;font-size:12px}
.camside{display:flex;flex-direction:column;gap:8px}
.shot{display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:8px}
.shot img{width:56px;height:42px;object-fit:cover;border-radius:6px;background:#0b0f16}
.shot .nm{font-size:12px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:900px){.camwrap{grid-template-columns:1fr}}
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
  /* 小球：**短且线性**。原来 .3s 的 ease-out 配 500ms 一次的数据更新，
     观感是「猛冲一下、然后停住」的顿感（用户反馈「不灵敏」）。
     0.15s linear 让它贴着数据走，没有加速-减速的假动作。 */
  transition:transform .15s linear,background .4s,box-shadow .4s;
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

<section class="card wide" id="devcard" hidden>
  <div class="card-head">
    <h2>设备</h2>
    <span class="src" id="devcount"></span>
    <button id="devall" style="margin-left:auto">全选</button>
  </div>
  <div class="devlist" id="devs"></div>
  <div class="hint" style="margin-top:6px">勾选多台后，下面的远程指令会**同时**下发给它们
    （不勾 = 只发给当前查看的那台）</div>
</section>

<main class="grid">
  <section class="card wide" id="card3d" hidden>
    <div class="card-head"><h2>板子姿态 · 3D</h2>
      <span class="src" style="display:flex;align-items:center;gap:8px">
        <span id="d3src">—</span>
        <span>刷新
          <select id="pollsel" title="页面从服务器取数的频率。想更跟手就选 60Hz —— 但前提是板端上报周期也调到 50ms（板子设置里的「灵敏度」），两个都够快才真跟手">
            <option value="16">60 Hz</option>
            <option value="33">30 Hz</option>
            <option value="50">20 Hz</option>
            <option value="100" selected>10 Hz</option>
            <option value="200">5 Hz</option>
            <option value="500">2 Hz</option>
            <option value="2000">0.5 Hz</option>
          </select>
        </span>
      </span></div>
    <canvas id="board3d" style="width:100%;height:280px;display:block"></canvas>
    <div class="hint" style="margin-top:6px">橙色小条 = 板子顶边（远的那条）；地面网格固定不动，板子跟着实时姿态转。
      数据**多久来一次**由两处共同决定：这里的「刷新」+ 板端「灵敏度」（上报周期）—— 两个都够快才真跟手。
      画面仍按 60Hz 平滑（中间做 slerp），所以取数慢一点也不会一跳一跳。</div>
  </section>

  <section class="card wide" id="cardcam">
    <div class="card-head"><h2>摄像头</h2>
      <span class="src" id="camwho">—</span></div>
    <div class="camwrap">
      <div class="camview"><img id="camimg" alt="摄像头画面">
        <div class="nosig" id="camnosig">点右边「开启实时画面」<br>
          （板子离线 / 摄像头自检没过时不会有画面）</div></div>
      <div class="camside">
        <button id="camshot">拍照 → 存进板子 SD 卡</button>
        <button id="camlive">开启实时画面</button>
        <div class="hint" style="margin-top:8px">
          实时画面是板子<b>推</b>上来的（板子是 HTTP 客户端，网页连不到板子本身）。
          开启后约 2 帧/秒，关掉可省 WiFi 带宽（OV3660 的 JPEG 是 1280x720，一帧约 27KB）。
        </div>
        <div class="hint" id="camstat" style="margin-top:6px">—</div>
      </div>
    </div>
    <div class="card-head" style="margin-top:14px"><h3 style="margin:0;font-size:13px">本机照片存档</h3>
      <span class="src" id="shotwho">—</span></div>
    <div class="list" id="shotlist" style="margin-top:8px"></div>
  </section>

  <section class="card wide" id="cardsd">
    <div class="card-head"><h2>存储管理</h2>
      <span class="src" id="sdwho">数据来自最近一次「读取存储信息」的回传</span></div>
    <div class="row" style="flex-wrap:wrap;gap:8px;align-items:center">
      <button id="sdrefresh">读取存储信息</button>
      <span class="hint">命令发给「设备」卡片里勾选的那些（勾多台就一起读）</span>
    </div>
    <div id="sdring" class="ringwrap"></div>
    <div class="list" id="sdfiles" style="margin-top:8px"></div>
  </section>

  <section class="card wide" id="cardcfg">
    <div class="card-head"><h2>板子设置</h2>
      <span class="src">改完通过「下一帧遥测的响应」下发到板子并写入 NVS；空着 = 不改那一项</span></div>
    <div class="row" style="flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px">
      <span class="src">WiFi 名称</span>
      <input id="cfgssid" placeholder="不改就留空" autocomplete="off"
             style="background:var(--card-hi);color:var(--text);border:1px solid var(--line);
                    border-radius:8px;padding:5px 9px;min-width:150px">
      <span class="src">密码</span>
      <input id="cfgpass" type="password" placeholder="不改就留空" autocomplete="new-password"
             style="background:var(--card-hi);color:var(--text);border:1px solid var(--line);
                    border-radius:8px;padding:5px 9px;min-width:150px">
    </div>
    <div class="row" style="flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px">
      <span class="src">服务器地址</span>
      <input id="cfgurl" placeholder="http://192.168.x.x:8000" autocomplete="off"
             style="background:var(--card-hi);color:var(--text);border:1px solid var(--line);
                    border-radius:8px;padding:5px 9px;min-width:210px">
      <span class="src">上报周期 ms</span>
      <input id="cfgper" type="number" min="50" max="2000" step="10" placeholder="50~2000"
             style="background:var(--card-hi);color:var(--text);border:1px solid var(--line);
                    border-radius:8px;padding:5px 9px;width:110px">
      <button id="cfgapply">下发到板子</button>
      <span id="cfghint" class="hint"></span>
    </div>
    <div class="hint" style="margin-top:6px">
      <b>改 WiFi 会先试连、连上了才保存</b>（最多 8 秒）—— 密码填错不会把板子弄失联。
      <b>上报周期就是「灵敏度」</b>：50ms=20Hz（最跟手）/ 100ms=10Hz / 500ms=2Hz（默认）。
      越小越跟手，但 WiFi 压力越大；50ms 已接近单次往返的量级，跑不到也正常。
    </div>
  </section>

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
    <span class="src">指令搭在「下一帧遥测的响应」里下发，板子在再下一帧回传结果 —— 一次真实的硬件往返；多台板时先在「设备」里点选目标</span>
  </div>
  <div class="row">
    <span id="cmdbts"></span>
    <span id="cmdhint" class="hint"></span>
  </div>
  <div class="row" style="margin-top:10px;align-items:center;gap:8px">
    <span class="src">方向档位 oN</span>
    <select id="orientsel" style="background:var(--card-hi);color:var(--text);
      border:1px solid var(--line);border-radius:8px;padding:4px 8px"></select>
    <button id="orientapply">应用档位</button>
    <span id="orientnow" class="hint"></span>
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

/* 设备名/事件文本都是**外部输入**（板端上报、且设备名由用户在配网页手填），
   拼进 innerHTML 之前必须转义。 */
var ESCMAP = { "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" };
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return ESCMAP[c]; });
}
/* 「最后上报」的人话表述 */
function ago(sec){
  if (!(sec >= 0)) return "—";
  if (sec < 1) return "刚刚";
  if (sec < 60) return sec.toFixed(1) + " 秒前";
  if (sec < 3600) return Math.round(sec/60) + " 分钟前";
  return Math.round(sec/3600) + " 小时前";
}

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
  led_set:     { label:"LED 常亮", params:{on:true}, toggle:true },
  set_orient:  { label:"设置方向档位" },
  set_config:  { label:"下发板子设置" },
  sd_format:   { label:"格式化 SD 卡", danger:true },
  sd_ls:       { label:"读取存储信息" },
  /* sd_rm 需要文件名参数，裸点必然失败 —— 它只该从「存储管理」里每个文件的
     删除按钮触发，所以这里标 hidden，不在指令栏出按钮。 */
  sd_rm:       { label:"删除文件", danger:true, hidden:true },
  cam_capture: { label:"拍照" },
  /* cam_stream 由摄像头卡片里的「开启/关闭实时画面」按钮触发，
     不在指令栏出按钮（那里会出现"开/关"两种语义，容易点错）。 */
  cam_stream:  { label:"实时画面", hidden:true }
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
/* ---------------- 板子姿态 3D ----------------
 * 把板子做成一块小牌子，按加速度计实时转动 —— "板子现在什么姿态"一眼就懂，
 * 比数字和圆点直观得多（用户 2026-09-23 提的需求）。
 *
 * three.js 用仓库里 vendor 的那份（`/vendor/three.min.js`，服务端白名单放行），
 * **不引 CDN**：教室/局域网常常没有外网，引 CDN 必然白屏。
 *
 * 坐标约定与板端屏幕、姿态球**完全一致**：+x 右、+y 下、+z 出屏。 */
var d3 = null;
function init3d(){
  if (typeof THREE === "undefined") return;
  var cv = $("board3d");
  if (!cv) return;
  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({canvas: cv, antialias: true, alpha: true});
  } catch (e) {
    return;                    /* 没有 WebGL 就不显示这张卡，别留个空白框 */
  }
  var W = Math.max(200, cv.clientWidth || 320), H = 280;
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.setSize(W, H, false);

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(38, W / H, 0.1, 100);
  /* 相机仰角要**高**：原来是 (0, 2.4, 4.8) 只有 26.6°，
   * 板子平放时几乎是"侧着一条线"，左右倾斜根本看不出来（用户反馈：
   * "平放着向左右倾斜板子不会显示，立起来就会"）。抬到 ~53°，
   * 平放时看到的是一个完整的方形面，倾斜一眼就能看出来。 */
  camera.position.set(0, 4.6, 3.4);
  camera.lookAt(0, 0, 0);

  scene.add(new THREE.AmbientLight(0xffffff, 0.8));
  var dl = new THREE.DirectionalLight(0xffffff, 1.05);
  dl.position.set(3, 6, 5);
  scene.add(dl);

  /* 板子本体：按**屏幕系**建模，于是"把测到的重力方向转到世界正上方"
     就等于"板子的真实姿态"（见 update3d）。 */
  var g = new THREE.Group();
  g.add(new THREE.Mesh(
    new THREE.BoxGeometry(2.0, 2.0, 0.2),
    new THREE.MeshStandardMaterial({color: 0x2b3444, roughness: 0.62, metalness: 0.18})));
  /* 屏幕面（+z 那面）用活动色的绿，一眼分出正反面。
   * ⚠️ 必须**露在板子表面之外**：板厚 0.2 → 表面在 z=±0.1，
   * 所以这里要 > 0.1。之前是 0.085（板厚还是 0.16 时勉强露在外面），
   * 我把板厚加到 0.2 之后它就被埋进板子里了 —— 屏幕面整块看不见。 */
  var scr = new THREE.Mesh(new THREE.PlaneGeometry(1.62, 1.62),
                           new THREE.MeshBasicMaterial({color: 0x1d9e75,
                                                        side: THREE.DoubleSide}));
  /* ⚠️ 屏幕面在 **-z** 侧。板子的坐标系是「+x 右、+y 下、**+z 朝屏幕里**」——
   * 这样才是右手系（x×y = z：右×下 = 朝里）。我第一版把 +z 当成「朝屏幕外」，
   * 于是整个模型是**镜像**的 —— 这才是立起来看到背面的真正根因。 */
  scr.position.z = -0.106;
  g.add(scr);
  /* 顶边标记：和板端屏幕的橙色小条同一个约定（+y 是"下"，所以顶边在 -y） */
  /* 顶边标记：做成**跨在板子顶边上、两面都露出来的一条棱**，
   * 而不是贴在某一面上的薄片 —— 原来贴在 +z 面（z=0.09），从背面看被板子挡住。
   * 现在 z 方向做到 ±0.14（板厚 ±0.1），正反面都看得到；
   * +y 是"下"（屏幕系），所以顶边在 -y。 */
  /* 位置在 **-y**：屏幕系里 +y 是"下"，所以 -y 才是板子的**顶边**。
   * 这样两种摆法都对：
   *   平放（屏幕朝上）→ 顶边在**离你远的那条边**
   *   立起来          → 顶边在**上面**
   * （曾经为了"平放时在远边"改成 +y，结果立起来就跑到下面去了 —— 用 -y 才对。） */
  var top = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.14, 0.28),
                           new THREE.MeshBasicMaterial({color: 0xef9f27}));
  top.position.set(0, -1.0, 0);
  g.add(top);
  scene.add(g);

  /* 地面网格固定在世界里：板子转、网格不动，倾斜才有参照物 */
  var grid = new THREE.GridHelper(9, 18, 0x3d4757, 0x252d3a);
  grid.position.y = -2.0;
  scene.add(grid);

  var flat = makeFlatPose();
  d3 = {renderer: renderer, scene: scene, camera: camera, group: g,
        target: flat.clone(), cur: flat.clone()};
  g.quaternion.copy(flat);
  $("card3d").hidden = false;

  /* 画面 60Hz 走、数据按「刷新」选择器的频率到：每帧朝目标姿态 slerp 一点点，
     所以即使数据来得慢，画面也是连续转的（取数越快越跟手）。 */
  (function loop(){
    requestAnimationFrame(loop);
    d3.cur.slerp(d3.target, 0.18);
    d3.group.quaternion.copy(d3.cur);
    d3.renderer.render(d3.scene, d3.camera);
  })();

  window.addEventListener("resize", function(){
    var w = Math.max(200, cv.clientWidth || 320);
    renderer.setSize(w, H, false);
    camera.aspect = w / H;
    camera.updateProjectionMatrix();
  });
}

/* 把板子转到「真实姿态」。
 *
 * 板子的坐标系（右手系）：**+x 右、+y 下、+z 朝屏幕里**（右×下 = 朝里 ✓）。
 * 上报的 x/y/z 就是**重力（下坡）方向**在这个坐标系里的分量 ——
 * 三个物理校准点（2026-09-24 实测）都吻合：
 *
 *     平放（屏幕朝上）  → (0.00,  0.02, +0.99)   下坡 = 朝屏幕里 = 朝下 ✓
 *     立起来（屏幕朝我）→ (0.00, +1.00,  0.00)   下坡 = 屏幕的「下」方向 ✓
 *     右边压低          → (+0.70, -0.03, +0.72)  下坡偏向右边 ✓
 *
 * 所以只要把**下坡方向对到世界的下方 (0,-1,0)**，板子姿态就对了 ——
 * 不需要任何镜像。（我前几版在这里又是翻 z 又是镜像 x/y，都是在补
 * 「模型镜像」这个更底层的错，两个错互相抵消了一部分，越补越乱。）
 *
 * ⚠️ 剩下的自由度：绕竖直轴转了多少，加速度计**测不出来**。
 * 板子立着时它能告诉你「下在哪」，却分不清屏幕朝你还是朝墙。
 * 用「最短弧」补这个自由度会挑到把板子翻过去的解 ——
 * 所以绕世界竖直轴扫一圈，取**离当前姿态最近**的解（靠连续性消歧，不猜）。
 * 初始姿态设成「平放、屏幕朝上、顶边朝远处」（板子开机通常就这么放）。
 */
function makeFlatPose(){
  /* 平放自然姿态：板子 +z（朝屏幕里）→ 世界下方；+y（屏幕的「下」）→ 朝观察者；
     于是 -z（屏幕面）→ 世界上方，-y（顶边）→ 世界远处。
     第三列必须是 (0,-1,0) —— 取 (0,1,0) 的话行列式 = -1，是镜像不是旋转。 */
  var m = new THREE.Matrix4();
  m.makeBasis(new THREE.Vector3(1, 0, 0),    /* 板子 +x → 世界 +x */
              new THREE.Vector3(0, 0, 1),    /* 板子 +y → 世界 +z（朝观察者） */
              new THREE.Vector3(0, -1, 0));  /* 板子 +z → 世界 -y（朝下） */
  return new THREE.Quaternion().setFromRotationMatrix(m);
}
function update3d(x, y, z){
  if (!d3) return;
  var down = new THREE.Vector3(x, y, z);
  if (down.lengthSq() < 1e-6) return;
  down.normalize();
  var base = new THREE.Quaternion().setFromUnitVectors(down, new THREE.Vector3(0, -1, 0));

  /* 绕世界竖直轴扫一圈，挑唯一解。加速度计**测不出**绕竖直轴的朝向，
   * 这一段是「约定」不是「测量」，所以两条约定要按板子的姿态**平滑加权**：
   *
   *   ① 屏幕面尽量朝观察者 —— 板子**立着**时才有意义（正面朝我是最自然的拿法）
   *   ② 顶边尽量朝远处     —— 板子**平放**时才有意义（通常就是这么摆在桌上）
   *
   * ⚠️ 这里踩过一个坑：一开始给①设了 1e-3 的固定容差，以为"平放时①会退化"。
   * 实际上平放时①不是**零**、而是**很小**（屏幕面朝上，其 z 分量只有零点几的
   * 噪声量级）—— 于是①照样压过②，**旋转角由噪声决定**：
   * 用户板子明明平放（0,-0.03,1.02），3D 却转了 30°。
   * 现在按"有多平"加权：w=1 完全用②，w=0 完全用①，中间平滑过渡。 */
  var flatness = Math.min(1, Math.abs(down.z));   /* 重力越沿屏幕法线 → 越平 */
  var w = flatness;

  var axis = new THREE.Vector3(0, 1, 0);
  var q = new THREE.Quaternion();
  var vFace = new THREE.Vector3(), vTop = new THREE.Vector3();
  var best = null, bestScore = -9;
  for (var i = 0; i < 72; i++) {
    q.setFromAxisAngle(axis, i * Math.PI / 36).multiply(base);
    /* 板子 -z 是屏幕面（+z 朝屏幕里），-y 是顶边 */
    vFace.set(0, 0, -1).applyQuaternion(q);
    vTop.set(0, -1, 0).applyQuaternion(q);
    var score = (1 - w) * vFace.z + w * (-vTop.z);
    if (score > bestScore + 1e-6) {
      bestScore = score;
      best = q.clone();
    }
  }
  d3.target.copy(best || base);
}




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
var lastDeg = null;
function setTilt(z, mag){
  var t, color;
  if (mag < 0.05){ t = "无读数"; color = C.faint; lastDeg = null; }
  else if (mag < 0.35){ t = "失重"; color = C.red; lastDeg = null; }
  else {
    var c = Math.min(1, Math.abs(z)/mag);
    var raw = Math.acos(c)*180/Math.PI;
    /* 只对**角度**做低通：acos 在接近平放时对噪声极敏感（0.03g → 14°），
       板端踩过同一个坑。姿态球不动它 —— 球要跟手。 */
    lastDeg = (lastDeg === null) ? raw : (lastDeg + 0.4*(raw - lastDeg));
    var deg = Math.round(lastDeg);
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
/* 多选下发：勾中的设备名集合（空 = 不指定，交给服务端用"最近上报的那台"，
   单板场景与以前完全一样）。**只在"下发指令"时生效**，"查看"仍然是单选。 */
var selDevices = {};
/* 板端上报的方向档位（每台一份），用来显示"板子现在是哪一档" */
var devOrient = {};
var STNAME = { queued:"排队中", sent:"已下发", done:"已完成", failed:"失败", timeout:"超时" };

function renderButtons(){
  var host = $("cmdbts");
  if (host.dataset.built === cmdNames.join(",")) { syncButtons(); return; }
  host.dataset.built = cmdNames.join(",");
  host.innerHTML = cmdNames.filter(function(n){
    return !(CMD_UI[n] || {}).hidden;          /* hidden：只走程序内部触发，不出按钮 */
  }).map(function(n){
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
function sendCmd(name, btn, overrideParams){
  /* 破坏性指令（目前只有 sd_format）必须先确认 —— 一键清掉整张卡，
     点错了没有撤销。 */
  var ui0 = CMD_UI[name] || {};
  if (ui0.danger){
    /* 单行文案：**刻意不写 \n** —— 这段 JS 住在 Python 的三引号字符串里，
       写一个反斜杠会被 Python 先吃成真换行，JS 就语法错误了。 */
    if (!window.confirm("确定要格式化板子上的 SD 卡吗？卡里原有内容会全部丢失，无法恢复。")) return;
  }
  var ui = CMD_UI[name] || {};
  var params = overrideParams !== undefined ? overrideParams
             : (ui.toggle && name === "led_set" ? {on: !ledSteady} : (ui.params || {}));
  if (btn) btn.disabled = true;
  $("cmdhint").textContent = "";
  var body = {name:name, params:params};
  var picked = Object.keys(selDevices);
  if (picked.length > 1){
    /* 多选 → 一次请求让服务端给每台各建一条命令（比前端循环 N 次更省事，
       而且服务端会去重、保持顺序） */
    body.devices = picked;
  } else if (picked.length === 1){
    body.device = picked[0];
  } else if (selDevice){
    /* 没勾任何一台 → 就发给"当前查看的那台"（单板场景与以前完全一样） */
    body.device = selDevice;
  }
  fetch("/api/command", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)
  }).then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok){ $("cmdhint").textContent = "下发失败：" + (d.error || "未知错误"); return; }
    if (d.batch){
      var off = (d.results || []).filter(function(x){ return !x.device_online; }).length;
      $("cmdhint").textContent = "已下发给 " + d.count + " 台"
        + (off ? ("（其中 " + off + " 台当前不在线，等它们回来才会执行）") : "");
    } else {
      $("cmdhint").textContent = "已下发 " + d.id
        + (d.device ? " → " + d.device : "")
        + (d.device_online ? "" : "（注意：这块板子当前不在线）");
    }
    pull();
  }).catch(function(e){
    $("cmdhint").textContent = "下发失败：" + e;
  }).then(function(){ syncButtons(); });
}

/* ---------------- 摄像头 ----------------
 * 板子是 HTTP 客户端、没有自己的服务端，所以画面是**板子 POST 上来、网页从这里取**。
 * 网页只拿最近一帧（/api/frame），拍照则由板子写 SD 卡、服务器另存一份给这里展示。 */
var camDev = "", camOn = false, camTimer = null, camFails = 0;

/* 卡片现在是**常驻**的：早先版本让它 hidden、等收到第一帧才显示，
 * 但"开启实时画面"按钮就在卡片里 —— 收不到帧就点不到按钮，点不到按钮就
 * 收不到帧，**死锁**（用户反馈"我好像没看到实时画面"就是这个）。
 * 这里保留函数只为了别处调用不报错。 */
function camShow(on){
  var c = $("cardcam");
  if (c) c.hidden = false;
}
function camFrame(){
  var im = $("camimg");
  if (!im) return;
  var dev = selDevice || "";      /* 空着就交给服务端回退到"最近上报的那台" */
  camDev = dev;
  im.src = "/api/frame?device=" + encodeURIComponent(dev) + "&t=" + Date.now();
  im.onload = function(){
    camFails = 0;
    camShow(true);
    var ns = $("camnosig");
    if (ns) ns.style.display = "none";
    var st = $("camstat");
    if (st) st.textContent = "画面 " + im.naturalWidth + "×" + im.naturalHeight +
                             "（约 2 帧/秒）";
  };
  im.onerror = function(){
    camFails++;
    var ns = $("camnosig");
    if (ns) ns.style.display = "";
    /* 连续取不到就别一直重试了，提示一次即可 */
    if (camFails === 3) {
      var st = $("camstat");
      if (st) st.textContent = "还没有画面 —— 摄像头自检没过时不会有帧（先看板子串口）";
    }
  };
}
function camSetLive(on){
  camOn = on;
  if (camTimer) { clearInterval(camTimer); camTimer = null; }
  var btn = $("camlive");
  if (on) {
    camFrame();
    camTimer = setInterval(camFrame, 200);      /* 5 fps，和板端推送节奏对齐 */
  }
  if (btn) btn.textContent = on ? "关闭实时画面" : "开启实时画面";
}
function shotRow(dev, name, bytes, local){
  return '<div class="shot"><img src="' + (local ? local : "/api/shots/" +
      encodeURIComponent(dev) + "/" + encodeURIComponent(name)) + '" alt="">' +
    '<span class="nm">' + esc(name) + '<br><span class="hint">' +
    (bytes ? fmtBytes(bytes) : "") + (local ? " · 本机存档" : " · 板子/服务器") + '</span></span>' +
    '<button data-shot="' + esc(name) + '" data-local="' + (local ? "1" : "") + '">删除</button></div>';
}
function renderShots(){
  var host = $("shotlist");
  if (!host) return;
  var dev = selDevice || "";      /* 空着就交给服务端回退到"最近上报的那台" */
  var who = $("shotwho");
  if (who) who.textContent = dev || "—";
  var server = [];
  var finish = function(){
    /* 服务器上的 + 本机 IndexedDB 里的，合起来展示（本机在前，刷新页面也不丢） */
    idbList(function(local){
      var html = local.map(function(x){ return shotRow(dev, x.name, x.bytes, x.url); }).join("") +
                 server.map(function(x){ return shotRow(x.device, x.name, x.bytes, null); }).join("");
      host.innerHTML = html || '<div class="empty">还没有照片。点上面的「拍照」按钮。</div>';
      Array.prototype.forEach.call(host.querySelectorAll("[data-shot]"), function(btn){
        btn.onclick = function(){
          var nm = btn.getAttribute("data-shot");
          if (!window.confirm("删除 " + nm + " ？无法恢复。")) return;
          if (btn.getAttribute("data-local")) {
            idbDel(nm, renderShots);
          } else {
            fetch("/api/shots/delete", {method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({device: dev, name: nm})}).then(renderShots);
          }
        };
      });
    });
  };
  fetch("/api/shots?device=" + encodeURIComponent(dev)).then(function(r){ return r.json(); })
    .then(function(d){ server = (d && d.shots) || []; finish(); })
    .catch(function(){ finish(); });
}

/* ---- 本机存档：IndexedDB（用户要求"存浏览器里、保持持久性"）----
 * 用 IndexedDB 而不是 localStorage：照片是二进制，localStorage 只能存字符串且只有 5MB。 */
var IDB_NAME = "rw1-photos", IDB_STORE = "shots";
function idbOpen(cb){
  var req = indexedDB.open(IDB_NAME, 1);
  req.onupgradeneeded = function(){
    var db = req.result;
    if (!db.objectStoreNames.contains(IDB_STORE)) {
      db.createObjectStore(IDB_STORE, {keyPath: "name"});
    }
  };
  req.onsuccess = function(){ cb(req.result); };
  req.onerror = function(){ cb(null); };
}
function idbPut(name, blob, cb){
  idbOpen(function(db){
    if (!db) { if (cb) cb(); return; }
    var tx = db.transaction(IDB_STORE, "readwrite");
    tx.objectStore(IDB_STORE).put({name: name, bytes: blob.size, blob: blob,
                                   t: Date.now()});
    tx.oncomplete = function(){ db.close(); if (cb) cb(); };
  });
}
function idbDel(name, cb){
  idbOpen(function(db){
    if (!db) { if (cb) cb(); return; }
    var tx = db.transaction(IDB_STORE, "readwrite");
    tx.objectStore(IDB_STORE).delete(name);
    tx.oncomplete = function(){ db.close(); if (cb) cb(); };
  });
}
function idbList(cb){
  idbOpen(function(db){
    if (!db) { cb([]); return; }
    var out = [];
    var tx = db.transaction(IDB_STORE, "readonly");
    var req = tx.objectStore(IDB_STORE).getAll();
    req.onsuccess = function(){
      db.close();
      (req.result || []).sort(function(a, b){ return b.t - a.t; }).forEach(function(x){
        out.push({name: x.name, bytes: x.bytes, url: URL.createObjectURL(x.blob)});
      });
      cb(out);
    };
    req.onerror = function(){ db.close(); cb([]); };
  });
}
/* 拍照：让板子写 SD 卡；同时把这一帧存进本机 IndexedDB 做持久备份 */
function camShot(){
  var dev = selDevice || "";      /* 空着就交给服务端回退到"最近上报的那台" */
  sendCmd("cam_capture");
  fetch("/api/frame?device=" + encodeURIComponent(dev) + "&t=" + Date.now())
    .then(function(r){ if (!r.ok) throw new Error("no frame"); return r.blob(); })
    .then(function(bl){
      var nm = "local-" + new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19) + ".jpg";
      idbPut(nm, bl, function(){ renderShots(); });
    })
    .catch(function(){ renderShots(); });
}

/* ---------------- 存储管理 ----------------
 * 数据来自最近一条 sd_ls 命令回传的 `note` 字段，格式：
 *     SPACE,<total>,<free>;NAME|<size>;NAME|<size>;...
 * 用 `|` `;` 分隔是安全的 —— 8.3 文件名里不可能出现它们（板端也是这么拼的）。 */
function fmtBytes(n){
  if (!isFinite(n) || n < 0) return "?";
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}
function parseSdNote(note){
  var out = { total:null, free:null, files:[], more:false };
  if (!note) return out;
  note.split(";").forEach(function(part){
    if (!part) return;
    if (part.indexOf("SPACE,") === 0){
      var p = part.split(",");
      out.total = parseInt(p[1], 10);
      out.free  = parseInt(p[2], 10);
    } else if (part === "..."){
      out.more = true;
    } else {
      var i = part.lastIndexOf("|");
      if (i > 0) out.files.push({ name: part.slice(0, i), size: parseInt(part.slice(i + 1), 10) });
    }
  });
  return out;
}
function renderSd(){
  var host = $("sdring"), list = $("sdfiles");   /* 容器 id 是 sdring（圆环） */
  if (!host || !list) return;
  /* 取最近一条**成功**的 sd_ls 结果 */
  var last = null;
  (cmdsSeen || []).forEach(function(c){
    if (c.name === "sd_ls" && c.state === "done" && c.result && c.result.note) last = c;
  });
  if (!last){
    host.innerHTML = '<div class="hint">还没读过 —— 点上面的「读取存储信息」' +
      '（板子离线时命令会排队，等它回来再执行）</div>';
    list.innerHTML = "";
    return;
  }
  var d = parseSdNote(last.result.note);
  if (d.total === null){
    host.innerHTML = '<div class="hint">板端没返回容量信息</div>';
  } else {
    var used = Math.max(0, d.total - d.free);
    var pct = d.total ? (used / d.total * 100) : 0;
    var bar = pct > 90 ? C.red : (pct > 75 ? C.amber : C.green);
    /* 圆环：中间写百分比，比一条横条直观；**点它 = 进入文件管理** */
    var R = 44, CIRC = 2 * Math.PI * R;
    var off = (CIRC * (1 - Math.min(100, pct) / 100)).toFixed(1);
    host.innerHTML =
      '<div class="ring" id="sdringbtn" title="点击进入文件管理">' +
        '<svg width="104" height="104" viewBox="0 0 104 104">' +
          '<circle cx="52" cy="52" r="' + R + '" fill="none" stroke="var(--track)" stroke-width="10"/>' +
          '<circle cx="52" cy="52" r="' + R + '" fill="none" stroke="' + bar + '" stroke-width="10" ' +
            'stroke-linecap="round" stroke-dasharray="' + CIRC.toFixed(1) + '" ' +
            'stroke-dashoffset="' + off + '"/>' +
        '</svg>' +
        '<div class="ringtxt"><b style="color:' + bar + '">' + pct.toFixed(0) + '%</b>' +
        '<span>已用</span></div>' +
      '</div>' +
      '<div class="ringmeta">' +
        '<div><span class="k">已用</span> ' + fmtBytes(used) + '</div>' +
        '<div><span class="k">总共</span> ' + fmtBytes(d.total) + '</div>' +
        '<div><span class="k">剩余</span> ' + fmtBytes(d.free) + '</div>' +
        '<div class="hint" style="margin-top:4px">点圆环进入文件管理 ↓</div>' +
      '</div>';
    var rb = $("sdringbtn");
    if (rb) {
      rb.onclick = function(){
        var list = $("sdfiles");
        if (list) {
          list.scrollIntoView({behavior: "smooth", block: "center"});
          list.style.transition = "box-shadow .3s";
          list.style.boxShadow = "0 0 0 2px " + bar;
          setTimeout(function(){ list.style.boxShadow = "none"; }, 1200);
        }
      };
    }
  }
  if (!d.files.length){
    list.innerHTML = '<div class="empty">卡上没有文件。</div>';
    return;
  }
  list.innerHTML = d.files.map(function(f){
    return '<div class="item"><span class="name">' + esc(f.name) + '</span>' +
      '<span class="meta mono">' + fmtBytes(f.size) + '</span>' +
      '<button data-rm="' + esc(f.name) + '" style="margin-left:auto">删除</button></div>';
  }).join("") + (d.more ? '<div class="hint">（文件较多，只列了前 12 个）</div>' : "");
  Array.prototype.forEach.call(list.querySelectorAll("[data-rm]"), function(b){
    b.onclick = function(){
      var fn = b.getAttribute("data-rm");
      if (!window.confirm("确定删除板子上的 " + fn + " 吗？无法恢复。")) return;
      sendCmd("sd_rm", null, {name: fn});
    };
  });
}

/* ---- 方向档位 oN：显示 + 点选修改 ----
 * 板端长按 BOOT 也能换档，但那是"盲按 N 次"；网页上直接选第几档、还能看到
 * 板子当前在哪一档，标定方向就不用再靠猜了（2026-09-23 用户真机标定时踩过）。 */
function syncOrient(){
  var sel = $("orientsel");
  if (sel && !sel.dataset.built){
    var opts = "";
    for (var i = 0; i < 16; i++) opts += '<option value="' + i + '">o' + i + '</option>';
    sel.innerHTML = opts;
    sel.dataset.built = "1";
  }
  var now = $("orientnow");
  if (!now) return;
  var who = Object.keys(selDevices);
  var ids = who.length ? who : (selDevice ? [selDevice] : Object.keys(devOrient));
  var parts = ids.filter(function(id){ return devOrient[id] !== undefined; })
                 .map(function(id){ return esc(id) + " = o" + devOrient[id]; });
  now.textContent = parts.length ? ("当前 " + parts.join(" / "))
                                 : "（还没收到板端上报的档位）";
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
  renderSd();          /* 存储面板跟着最近一条 sd_ls 的结果走 */
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

/* ---------------- 设备列表（多板） ---------------- */
/* selDevice = null 表示"自动跟随最近上报的那台"（单板场景就是这样，永远不用点）。
   用户点过某台之后就固定看它，直到那台消失。 */
var selDevice = null, devHTML = "";

function devQuery(){
  return selDevice ? "?device=" + encodeURIComponent(selDevice) : "";
}

function renderDevices(list, current){
  var card = $("devcard");
  list = list || [];
  /* 只有一台板时不显示这块 —— 单板课堂不该凭空多出一张卡片 */
  if (list.length <= 1){ card.hidden = true; devHTML = ""; return; }
  card.hidden = false;

  var online = 0;
  var h = list.map(function(d){
    if (d.online) online++;
    var color = d.online ? C.green : C.red;
    var title = d.activity || "";
    /* 记住每台的档位，供"方向档位"控件显示 */
    if (d.orient !== null && d.orient !== undefined) devOrient[d.id] = d.orient;
    var oTxt = (d.orient === null || d.orient === undefined) ? "" : (" · o" + d.orient);
    var checked = selDevices[d.id] ? " checked" : "";
    return '<div class="dev' + (d.id === current ? " sel" : "") + '" data-dev="' + esc(d.id) + '">'
      + '<div class="row1">'
      + '<input type="checkbox" class="devchk" data-chk="' + esc(d.id) + '"' + checked + '>'
      + '<span class="dot' + (d.online ? "" : " off") + '" style="background:' + color + '"></span>'
      + '<span class="did">' + esc(d.id) + '</span></div>'
      + '<div class="meta">' + (d.online ? "在线" : "离线") + ' · ' + ago(d.age)
      + (d.source && d.source !== "-" ? " · " + esc(d.source) : "") + esc(oTxt) + '</div>'
      + '<div class="meta">' + esc(title) + '</div>'
      + '</div>';
  }).join("");

  $("devcount").textContent = list.length + " 台 · 在线 " + online;
  if (h === devHTML) return;
  devHTML = h;
  var host = $("devs");
  host.innerHTML = h;
  Array.prototype.forEach.call(host.querySelectorAll(".dev"), function(el){
    el.onclick = function(ev){
      /* 点复选框是"勾选下发目标"，不该顺带切换查看的设备 */
      if (ev.target && ev.target.classList && ev.target.classList.contains("devchk")) return;
      selectDevice(el.getAttribute("data-dev"));
    };
  });
  Array.prototype.forEach.call(host.querySelectorAll(".devchk"), function(cb){
    cb.onclick = function(ev){
      ev.stopPropagation();
      var id = cb.getAttribute("data-chk");
      if (cb.checked) selDevices[id] = 1; else delete selDevices[id];
      syncDevAll();
    };
  });
  syncDevAll();
  /* 档位显示也要在这里刷：它读的是 `devOrient`，而那个表是**本函数**填的。
   * 只在初始化时调一次 syncOrient() 的话，设备列表后到就永远显示"还没收到档位"
   * （2026-09-23 浏览器 E2E 抓到的时序 bug）。 */
  syncOrient();
}

/* 「全选」按钮：全都勾上 / 全都取消。文案随状态变，避免用户看不出当前是哪种。 */
function syncDevAll(){
  var n = Object.keys(selDevices).length;
  var btn = $("devall");
  if (btn) btn.textContent = n ? ("取消全选（已选 " + n + "）") : "全选";
  var host = $("devs");
  if (host) Array.prototype.forEach.call(host.querySelectorAll(".devchk"), function(cb){
    var id = cb.getAttribute("data-chk");
    cb.checked = !!selDevices[id];
  });
}
function toggleDevAll(){
  var boxes = $("devs") ? $("devs").querySelectorAll(".devchk") : [];
  if (Object.keys(selDevices).length){
    selDevices = {};
  } else {
    Array.prototype.forEach.call(boxes, function(cb){ selDevices[cb.getAttribute("data-chk")] = 1; });
  }
  syncDevAll();
}

function selectDevice(id){
  if (!id || id === selDevice) return;
  selDevice = id;
  devHTML = "";        /* 清掉缓存，让选中态立刻重画 */
  pull();              /* 先拉一次快照，不必等下一帧 SSE */
  stream();            /* SSE 是按设备过滤的，换设备要重连 */
}

/* ---------------- 主刷新 ---------------- */
var aiReply = "";
function render(s){
  /* 服务端实际给的是哪台：若和我们选的不一致（选中的被淘汰/改名了），跟着它走，
     免得页面一直停在一个已经不存在的设备上。 */
  if (selDevice && s.device && s.device !== selDevice){
    selDevice = s.device;
    devHTML = "";
  }
  renderDevices(s.devices, s.device || selDevice);

  devOnline = !!s.device_online;
  var dev = $("dev");
  dev.className = "pill " + (devOnline ? "on" : "off");
  dev.innerHTML = "<i></i>" + (devOnline ? "在线" : "离线");
  if (selDevice) dev.title = selDevice;
  /* **必须在这里也刷一次按钮**：`b.disabled = busy || !devOnline` 只在
   * syncButtons() 里算，而 syncButtons() 原来只在"按钮构建时"和 renderCmds()
   * 末尾被调用 —— 页面加载时设备还没上报，按钮被设成 disabled 之后就**再也没刷新过**，
   * 于是设备上线后按钮一直是灰的（2026-09-23 浏览器 E2E 抓到的真 bug）。 */
  syncButtons();

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
    setRing(mag); setBall(x, y, mag); setTilt(z, mag); update3d(x, y, z);
    setBar(0, x); setBar(1, y); setBar(2, z);
  }
  renderCmds(s.commands);
  renderEvents(s.events);
}

function pull(){
  fetch("/api/latest" + devQuery()).then(function(r){ return r.json(); }).then(function(s){
    samples = s.samples || [];
    drawChart();
    render(s);
  }).catch(function(){});
}

/* 换设备要重连 SSE（服务端按 ?device= 过滤）。用世代号防止旧连接的回调
   把新连接的画面覆盖掉 —— 否则点一下设备，屏幕会闪回上一台的数据。 */
var streamGen = 0, es = null;
function stream(){
  var gen = ++streamGen;
  if (es){ try{ es.close(); }catch(e){} es = null; }
  var s = new EventSource("/api/stream" + devQuery());
  es = s;
  s.onmessage = function(e){
    if (gen !== streamGen) return;
    render(JSON.parse(e.data));
  };
  s.onerror = function(){
    if (gen !== streamGen) return;
    try{ s.close(); }catch(e){}
    if (es === s) es = null;
    setTimeout(stream, 2000);
  };
}

/* ---------------- 启动 ---------------- */
$("host").textContent = location.host;
fitCanvas();
fetch("/api/logs").then(function(r){ return r.json(); }).then(function(l){
  if (l && l.dir) $("logdir").textContent = "落盘 " + l.dir;
}).catch(function(){});
fetch("/api/commands" + devQuery()).then(function(r){ return r.json(); }).then(function(d){
  cmdNames = d.names || ["capture_once"];
  if ($("devall")) $("devall").onclick = toggleDevAll;
  if ($("sdrefresh")) $("sdrefresh").onclick = function(){ sendCmd("sd_ls"); };
  /* 摄像头接线放最后：它依赖 IndexedDB 等浏览器能力，**万一出错也不能
     中断上面的初始化**（曾经插在 init3d() 之前，一出错整个回调就断了，
     #devall / #cmdbts / 3D 全都不执行）。 */
  try {
    if ($("camshot")) $("camshot").onclick = camShot;
    if ($("camlive")) $("camlive").onclick = function(){ camSetLive(!camOn); };
    renderShots();
  } catch (e) {
    if (window.console) console.warn("摄像头初始化失败（不影响其它功能）: " + e);
  }
  init3d();
  /* 板子设置：只把**填了**的项发过去（空着 = 不改那一项）。 */
  if ($("cfgurl")) $("cfgurl").placeholder = location.origin;   /* 提示当前地址 */
  if ($("cfgapply")) $("cfgapply").onclick = function(){
    var p = {};
    var ssid = ($("cfgssid").value || "").trim();
    var pass = $("cfgpass").value || "";
    var url = ($("cfgurl").value || "").trim();
    var per = parseInt($("cfgper").value, 10);
    if (ssid) p.ssid = ssid;
    if (pass) p.pass = pass;
    if (url) p.url = url;
    if (!isNaN(per)) p.period_ms = per;
    if (!Object.keys(p).length){
      $("cfghint").textContent = "什么都没填，不改。";
      return;
    }
    $("cfghint").textContent = (p.ssid ? "下发中…（改 WiFi 会先试连，最多 8 秒）" : "下发中…");
    sendCmd("set_config", null, p);
    $("cfgpass").value = "";     /* 密码不留在这 */
  };
  if ($("orientapply")) $("orientapply").onclick = function(){
    var v = parseInt($("orientsel").value, 10);
    if (isNaN(v)) return;
    sendCmd("set_orient", this, {o: v});
  };
  syncOrient();
  $("names").textContent = cmdNames.join(" / ");
  renderButtons();
  renderCmds(d.commands);
}).catch(function(){
  cmdNames = ["capture_once"];
  renderButtons();
});
pull();
/* 刷新率可调：原来硬编码 2000ms（0.5Hz），3D 看着一顿一顿的。
 * 注意这是"页面从服务器取数"的频率，真正决定跟手程度的是**板端上报周期**
 * （板子设置里那个"灵敏度"，默认 500ms = 2Hz）—— 两个都要够快才跟手。
 * 选 20Hz 时若板端还是 500ms，看到的仍然是 2Hz 的数据，只是取数更勤。 */
var pollTimer = null;
function setPoll(ms){
  if (pollTimer) { clearInterval(pollTimer); }
  pollTimer = setInterval(pull, ms);
  pull();
}
if ($("pollsel")) {
  $("pollsel").onchange = function(){ setPoll(parseInt(this.value, 10) || 100); };
}
setPoll(parseInt(($("pollsel") || {}).value, 10) || 100);
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
    global SHOTS_DIR
    SHOTS_DIR = os.path.join(args.data_dir, "shots")
    LOGGER = JsonlLogger(args.data_dir, retain_days=args.retain_days,
                         log_telemetry=not args.no_log_telemetry, log_hz=args.log_hz)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    # 后台心跳：设备超时判定离线 + 命令超时看护
    def watchdog():
        next_purge = time.time() + 86400
        while True:
            time.sleep(1.0)
            now = time.time()
            with LOCK:
                for dev in list(DEVICES.values()):
                    st = dev.st
                    if st["last_post"] <= 0.0:
                        continue          # 从没上报过的占位设备，不参与在线判定
                    online = (now - st["last_post"]) < DEVICE_TIMEOUT
                    if st["device_online"] and not online:
                        push_event(dev, "info", "设备 %s 连接超时，已标记离线" % dev.id)
                        # P1-12：顺手复位跌落状态。否则掉线期间若正好处在跌落态，
                        # 重新上线后第一次**真实**跌落不会触发事件
                        # （analyze 里的判据是 fall_active and not prev_fall）。
                        st["fall_active"] = False
                        st["activity"] = ACTIVITY_IDLE
                    st["device_online"] = online
            for dev, name in expire_commands():
                push_event(dev, "cmd", "[%s] 命令 %s 超时（%.0f 秒内没有回传结果）"
                           % (dev.id, name, CMD_TIMEOUT_S))
            # P1-11：日志清理原来只在 JsonlLogger 构造时跑一次，README 却写
            # 「保留 7 天」——服务器连续跑一个学期会一直涨。改成每天清一次。
            if LOGGER is not None and time.time() > next_purge:
                LOGGER.purge_old()
                next_purge = time.time() + 86400
    threading.Thread(target=watchdog, daemon=True).start()

    llm = "大模型已配置 (%s)" % os.environ.get("RW1_LLM_MODEL", "?") \
        if os.environ.get("RW1_LLM_API_KEY") else "本地规则AI（可配 RW1_LLM_API_KEY 升级）"
    print("=" * 66)
    print(" AI 交互课 · PC 服务器已启动")
    print("   仪表盘:  http://localhost:%d/" % args.port)
    print("   遥测:    POST http://<本机IP>:%d/api/telemetry" % args.port)
    print("   设备:    GET  http://<本机IP>:%d/api/devices" % args.port)
    print("   指令:    POST http://<本机IP>:%d/api/command   {\"name\":\"capture_once\"}" % args.port)
    print("   AI模式:  %s" % llm)
    print("   指令超时: %.0f 秒" % CMD_TIMEOUT_S)
    print("   多设备:  最多 %d 台；?device=<名字> 可指定，不带则用最近上报的那台" % MAX_DEVICES)
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

# PROBE_MARKER_12345

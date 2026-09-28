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
import socket
import sys
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

# 仪表盘自托管字体白名单（详见 Handler._send_vendor_font）。字体名来自 URL，
# 所以不放正则、只放**逐字写死**的文件名 —— 这是最难被绕过的白名单。
FONT_FILES = ("sora-400.woff2", "sora-600.woff2", "sora-700.woff2", "sora-800.woff2",
              "plexmono-400.woff2", "plexmono-500.woff2", "plexmono-600.woff2")

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
# 实时画面的帧率上限。**必须和板端 transport.c 的 CAM_STREAM_FPS_MAX 一致** ——
# 板子一帧要「等帧 + 开一条 TCP + POST 27KB」，实测能稳定跑到的只有每秒几帧，
# 写大了只是让板端白忙。两边不一致的话，网页上选的值会被静默夹掉、看着像没生效。
CAM_STREAM_FPS_MAX = 10
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
        # 开/关 + **可选**的帧率（网页上的「帧率」选择器）。
        # ⚠️ 这里曾经只放行 on，于是 fps 被静默丢掉、网页上选几档都没用
        # （2026-09-28 实测：四个档位测出来都是 ~1.5 帧/秒）。
        # **不传就不带这个键** —— 板端是"没传就沿用当前值"，老调用方行为不变。
        out = {"on": bool(p.get("on"))}
        if p.get("fps") is not None:
            out["fps"] = clamp_int(p.get("fps"), 1, CAM_STREAM_FPS_MAX, 2)
        return out
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


def port_already_serving(host, port):
    """端口上是不是已经有人在应答了？

    ⚠️ Windows 上 `SO_REUSEADDR` 允许**两个进程绑同一个端口**（Unix 不允许），
    于是"重复启动服务端"不会报错，而是第二个进程静默地抢走一部分请求：
    表现是**启动横幅明明打出来了，页面却连不上、或者数据一半新一半旧**。
    2026-09-26 就被这个坑掉了一轮排查（E2E 报"服务端起不来"，可日志里横幅好端端的）。

    所以启动前先探一下，已经有人就**响亮地退出**，别让它变成玄学问题。
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::", "*") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        s.connect((probe_host, port))
        return True                      # 连得上 = 已经有人在那儿
    except OSError:
        return False
    finally:
        s.close()


class DashboardServer(ThreadingHTTPServer):
    """带大 backlog 的 HTTP 服务器。

    ⚠️ `socketserver` 默认 `request_queue_size = 5`，这对本项目的负载**不够**：
    打开一次仪表盘会**并发**发起十几个连接（6 个字体 + three.min.js + latest +
    stream + commands + logs），而教室里 20 块板子还在同时 POST 遥测。
    backlog 一满，内核直接回 ECONNREFUSED —— 浏览器那边看到的是
    "字体没加载、悄悄退回系统字体"（最阴的一种：页面照样能开，只是字体变了），
    或者 SSE 断连重试。2026-09-26 用真浏览器 + 假板子复现，一次开页就丢 3 个字体。

    `daemon_threads`：Ctrl-C 时不会被 SSE 长连接卡住不退。
    `handle_error`：客户端提前断开是**正常现象**（关页面、切设备重连 SSE），
    不该打一整段 Python 堆栈出来吓用户。
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # 只吞"连接层面的正常中断"。**不要**写成 `OSError` ——
        # ConnectionReset/Aborted/BrokenPipe/Timeout 都是它的子类，
        # 但磁盘写满、文件句柄泄漏这类真错误也是，吞掉就等于把真问题藏了。
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            return                      # 客户端断开，属于正常现象，不打印
        ThreadingHTTPServer.handle_error(self, request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "RW1/1.1"
    protocol_version = "HTTP/1.1"     # 让板端的 esp_http_client 能复用连接

    def log_message(self, fmt, *args):   # 安静一点
        pass

    # ---- helpers ---------------------------------------------------------
    def _send_vendor(self, relpath, ctype):
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
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 内容不会变，缓存久一点；不然每次打开页面都要重下 600KB
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_vendor_font(self, name):
        """仪表盘用的自托管字体（Sora / IBM Plex Mono）。

        为什么非要自己托管、不走 CDN：教室/局域网经常没有外网，引 CDN 必然白屏；
        而 `font-family` 里写 "Sora" 却没把字体送到浏览器，等于**静默退回系统字体** ——
        设计规则明确禁止系统默认无衬线，这种"看着像生效了其实没生效"最要命。

        `name` 来自 URL，所以这里是**真·外部输入**：用白名单卡死，
        只允许仓库里实际存在的这几个文件名（任何 `..`、斜杠、反斜杠都不在名单里）。
        """
        if name not in FONT_FILES:
            self._send(404, json.dumps({"ok": False, "error": "no such font"}))
            return
        self._send_vendor("docs/vendor/fonts/" + name, "font/woff2")

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
            self._send_vendor("docs/vendor/three/build/three.min.js",
                              "text/javascript; charset=utf-8")
        elif path.startswith("/vendor/fonts/"):
            # 同样是白名单。字体必须真送到浏览器，否则 font-family 会静默回落到
            # 系统字体 —— 那正是设计规则明令禁止的东西，而且看不出来。
            self._send_vendor_font(path[len("/vendor/fonts/"):])
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
                # ⚠️ **必须带上设备列表**：网页的 pull() 是每 100ms 一次，
                # 而 renderDevices() 在"列表 ≤ 1 台"时会把「设备」区域藏起来。
                # 早先这里不带 devices，于是多板课堂下那一块会以取数频率疯狂闪烁
                # （SSE 每 500ms 把它显示出来、/api/latest 每 100ms 又把它藏起来），
                # 而且大部分时间是不可见的 —— 2026-09-26 用真浏览器才抓到。
                snap["devices"] = devices_snapshot()
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
<!-- data-theme 写在标记里 = 默认明亮（用户 2026-09-26 要求）。
     下面那个内联脚本会在**首次绘制之前**把它改成用户上次选的那套。 -->
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ego Link · 实时仪表盘</title>
<script>
/* 主题必须在**首次绘制之前**定下来，否则会先闪一下明亮再跳深色（FOUC）。
   所以它必须是 head 里第一段内联脚本，不能等 DOMContentLoaded、也不能放外链。 */
(function(){
  try {
    var t = localStorage.getItem("rw1-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* 隐私模式读不到 localStorage —— 保持标记里的默认值（明亮） */ }
})();
</script>
<!-- 空 favicon：不写这行浏览器会去请求 /favicon.ico，服务端没有这条路由，
     于是每次打开页面都留一条 404 在控制台（2026-09-23 E2E 抓到的）。 -->
<link rel="icon" href="data:,">
<script src="/vendor/three.min.js"></script>
<style>
/* ==========================================================================
   设计令牌（Design Tokens）
   --------------------------------------------------------------------------
   这个页面和开发板屏幕（device/main/ui.c）共用一套**语义色含义**：
   同一台板子、同一时刻，屏幕上和网页上说的是同一件事。
   但两边的**表现形式**不同 —— 板子是 240x240 的 LVGL 面板，这里是浏览器。
   所以这里不照抄板端的像素参数，只共享语义，视觉语言另起一套更严格的规则。

   硬性规则（违反即视为 bug）：
     · 字体：不用 Inter / Roboto / 泛型 sans-serif；标题正文用 Sora，
       数字代码用 IBM Plex Mono，两者都是**仓库内自托管**的 woff2 ——
       教室局域网常常没有外网，引 CDN 必然白屏（配网页当初就是踩过这个坑）。
       中文回落到明确列出的 CJK 字面，而不是让它掉到系统默认字体。
     · 颜色：全部 OKLCH，按角色命名。主色**只有青绿一个**，只出现在
       「可操作 / 系统活着」的地方；活动语义色只用于数据本身。
       底色不是纯黑（纯黑会让所有层级糊成一团），文字不是纯白。
     · 圆角：全站只有 3 个值（6 / 10 / 14）。禁止到处 16px。
       圆形（状态点、气泡球、圆环）是形状不是圆角，不占额度。
     · 间距：8px 基准网格 —— 4/8/12/16/24/32/48/64/96，没有别的值。
     · 字号：正文 15px，标题按 1.25~1.5 倍逐级步进，
       hero 48px = 正文的 3.2 倍（规则要求 ≥ 3 倍）。
     · 阴影：按层级分三档，不是一刀切的大模糊。

   ⚠️ **这一套已冻结**（2026-09-26 用户看过截图后确认："后面的都按照这个来设置"）。
   以后新增页面 / 卡片**照抄这套令牌**，不要另起一套视觉语言。
   改完必须跑 `node tools/e2e_design.js`（19 条断言，含离线空状态与 320px 窄屏）——
   设计规则是硬约束，只靠看截图不算验证。
   细节与踩过的坑见 `.workbuddy-ai/memory/2026-09-26.md`。

   ⚠️ 关于"字号不要太大"（2026-09-26 第二版）：
   第一版老老实实按 15 → 19 → 24 → 32 → 48 铺了五级，结果**九个标题同时在喊**
   （3 个区域标题 32px + 6 个卡片标题 24px），一屏里没有重点，看着就是"乱"。
   现在砍掉 32px 那一级：正文 15 / 卡片标题 19 / 区域标题 24 / hero 48。
   步进 1.267 和 1.263 仍然落在规则要求的 1.25~1.5 里，hero 仍然是正文的 3.2 倍。
   **规则约束的是比例，不是"层级越多越好"。**
   ========================================================================== */

/* ---- 字体：仓库自托管，不走 CDN ---- */
@font-face{font-family:"Sora";src:url("/vendor/fonts/sora-400.woff2") format("woff2");font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:"Sora";src:url("/vendor/fonts/sora-600.woff2") format("woff2");font-weight:600;font-style:normal;font-display:swap}
@font-face{font-family:"Sora";src:url("/vendor/fonts/sora-700.woff2") format("woff2");font-weight:700;font-style:normal;font-display:swap}
@font-face{font-family:"PlexMono";src:url("/vendor/fonts/plexmono-400.woff2") format("woff2");font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:"PlexMono";src:url("/vendor/fonts/plexmono-500.woff2") format("woff2");font-weight:500;font-style:normal;font-display:swap}
@font-face{font-family:"PlexMono";src:url("/vendor/fonts/plexmono-600.woff2") format("woff2");font-weight:600;font-style:normal;font-display:swap}

:root{
  color-scheme:dark;

  /* 中文回落是**逐个点名的**字体，不是泛型 sans-serif */
  --font-display:"Sora","PingFang SC","Microsoft YaHei UI","Microsoft YaHei","Noto Sans CJK SC","Source Han Sans SC",sans-serif;
  --font-body:"Sora","PingFang SC","Microsoft YaHei UI","Microsoft YaHei","Noto Sans CJK SC","Source Han Sans SC",sans-serif;
  --font-mono:"PlexMono","IBM Plex Mono","Cascadia Mono",Consolas,"Microsoft YaHei UI",monospace;

  /* ---- 表面：由深到浅四层，都是低饱和冷中性色（不是蓝黑，也不是纯黑） ---- */
  --color-bg:           oklch(0.170 0.010 258);
  --color-surface:      oklch(0.216 0.011 258);
  --color-surface-2:    oklch(0.252 0.012 258);
  --color-surface-3:    oklch(0.290 0.013 258);
  --color-line:         oklch(0.310 0.013 258);
  --color-line-strong:  oklch(0.400 0.015 258);

  /* ---- 文字：三级，对比度都过 WCAG AA（按最亮的卡片底色算最坏情况） ---- */
  --color-text:         oklch(0.950 0.006 258);
  --color-text-muted:   oklch(0.780 0.013 258);
  --color-text-faint:   oklch(0.655 0.013 258);

  /* ---- 主色：只有一个。出现在主按钮、焦点环、在线指示 ---- */
  --color-action-primary:      oklch(0.800 0.130 178);
  --color-action-primary-hi:   oklch(0.860 0.130 178);
  --color-action-primary-ink:  oklch(0.200 0.030 178);
  --color-action-primary-soft: oklch(0.800 0.130 178 / 0.16);

  /* 危险操作（格式化 SD / 删文件）的按钮底与文字色。
     它不算"主色"，是**语义例外**：破坏性动作必须一眼看出跟别的不一样。 */
  --color-danger:     oklch(0.660 0.200 25);
  --color-danger-hi:  oklch(0.720 0.200 25);
  --color-danger-ink: oklch(0.170 0.030 25);

  /* ---- 数据语义色：只用于数据本身（活动词、圆环、轴条、曲线、事件点） ----
     色相刻意与板端 UI_C_* 保持同一含义，换端不用重新学。 */
  --color-still:  oklch(0.735 0.170 145);   /* 静置 */
  --color-walk:   oklch(0.690 0.165 295);   /* 步行 */
  --color-move:   oklch(0.730 0.150 250);   /* 运动 */
  --color-shake:  oklch(0.810 0.140 80);    /* 晃动 */
  --color-fall:   oklch(0.660 0.200 25);    /* 跌落 */
  --color-idle:   oklch(0.655 0.013 258);   /* 无读数 */

  /* 当前活动的颜色与 RGB 分量：JS 按 classify() 结果注入，驱动 hero 整体联动 */
  --act-color: var(--color-idle);
  --act-rgb: 135,141,148;

  /* ---- 间距：8px 基准网格，没有第五个值 ---- */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-6:24px;
  --sp-8:32px; --sp-12:48px; --sp-16:64px; --sp-24:96px;

  /* ---- 圆角：全站只有这 3 个 ---- */
  --radius-control:6px;   /* 按钮 / 输入框 / 下拉 / 标签 / 状态块 */
  --radius-card:10px;     /* 卡片 / 面板 / 画面 / 图表 */
  --radius-modal:14px;    /* 弹窗 */

  /* ---- 字号：正文 15 → 卡片标题 19 → 区域标题 24 → hero 48 ---- */
  --fs-meta:12.5px;
  --fs-body:15px;
  --fs-h3:19px;    /* 卡片 / 面板标题 */
  --fs-h2:24px;    /* 区域标题 */
  --fs-hero:48px;  /* hero 活动词 = 正文 3.2 倍 */

  /* ---- 阴影：三档，分别对应「浮在页面上」「浮在卡片上」「浮在所有东西上」 ----
     深色底上的阴影靠"更黑"来分层；浅色底上同样的黑度会显得脏，
     所以浅色主题那一份把 alpha 压到 1/5 左右（见下面的 light 块）。 */
  --shadow-1:0 1px 1px oklch(0 0 0/.30), 0 2px 6px oklch(0 0 0/.22);
  --shadow-2:0 1px 1px oklch(0 0 0/.32), 0 4px 10px oklch(0 0 0/.26), 0 12px 28px oklch(0 0 0/.20);
  --shadow-3:0 2px 2px oklch(0 0 0/.36), 0 10px 24px oklch(0 0 0/.32), 0 32px 64px oklch(0 0 0/.34);
  --ring:0 0 0 3px var(--color-action-primary-soft);

  /* ---- 下面这些是「只在个别地方用一次」的表面/色调，抽成令牌是为了能整套换主题 ---- */
  --appbar-bg:oklch(0.170 0.010 258 / .84);              /* 顶栏（半透明 + 毛玻璃） */
  --bg-wash:radial-gradient(1100px 520px at 8% -12%, oklch(0.300 0.020 258 / .42), transparent 70%);
  --stage-veil:linear-gradient(180deg, oklch(0.196 0.011 258), transparent);  /* hero 带子的"抬起感" */
  --ball-from:oklch(0.270 0.013 258);                    /* 姿态球的球面渐变 */
  --ball-to:oklch(0.190 0.010 258);
  --video-bg:oklch(0.140 0.008 258);                     /* 摄像头/照片的取景框底 */
  --modal-backdrop:oklch(0.140 0.008 258 / .70);
  --sheen:oklch(1 0 0 / .05);                            /* 卡片/弹窗顶部 1px 内高光 */
  /* 语义色的"淡底"版本（状态胶囊、选中行、状态块）。深色底上要淡得能透出底色，
     浅色底上要用更深的同色相 —— 所以每个主题各一份，不靠 color-mix()。 */
  --tint-primary:oklch(0.800 0.130 178 / .10);
  --tint-primary-line:oklch(0.800 0.130 178 / .40);
  --tint-danger:oklch(0.660 0.200 25 / .10);
  --tint-danger-line:oklch(0.660 0.200 25 / .40);
  --tint-still:oklch(0.735 0.170 145 / .16);
  --tint-shake:oklch(0.810 0.140 80 / .16);
  --tint-fall:oklch(0.660 0.200 25 / .16);

  --w-max:1440px;
  --ease:cubic-bezier(.16,1,.3,1);
}

/* ==========================================================================
   浅色主题 —— **默认就是这一套**（用户 2026-09-26 要求"默认明亮"）
   --------------------------------------------------------------------------
   只覆盖"角色令牌"的值，不改任何一条结构/尺寸规则 —— 两套主题共用同一份
   圆角、间距、字号阶梯、组件结构。这样"设计规则"只写一遍，两个主题都受约束。

   数值不是眼睛调的，是按**最坏底色**算过 WCAG AA 的（卡片 surface 最亮，
   所以它才是最难过的那个底）：
     正文 15.6 / 次级 7.6 / 弱文字 5.4（对 surface）；弱文字对 surface-2 是 4.7
     主按钮白字对主色 5.3；危险按钮 5.9
     数据语义色（静置/步行/运动/晃动/跌落）对 surface 分别 5.5/6.9/5.8/5.2/5.9
   `tools/e2e_design.js` 会**在两个主题下各跑一遍**这些断言，改坏了会红。

   两条容易翻车的规则，这里都刻意避开了：
     · 禁纯白大面积：底是 oklch(0.962)（≈#f1f2f5），卡片是 oklch(0.990)（≈#fbfcfd），
       都不是 #fff。
     · 主色只有一个：浅色里主色变深（0.500 而不是 0.800）——
       深色的亮青绿放到白底上对比度不够，直接照抄会挂。
   ========================================================================== */
html[data-theme="light"]{
  color-scheme:light;

  --color-bg:           oklch(0.962 0.004 258);
  --color-surface:      oklch(0.990 0.002 258);
  --color-surface-2:    oklch(0.945 0.005 258);
  --color-surface-3:    oklch(0.900 0.006 258);
  --color-line:         oklch(0.905 0.006 258);
  --color-line-strong:  oklch(0.820 0.008 258);

  --color-text:         oklch(0.250 0.012 258);
  --color-text-muted:   oklch(0.440 0.014 258);
  --color-text-faint:   oklch(0.520 0.014 258);

  --color-action-primary:      oklch(0.500 0.115 178);
  --color-action-primary-hi:   oklch(0.450 0.115 178);
  --color-action-primary-ink:  oklch(0.990 0.008 178);
  --color-action-primary-soft: oklch(0.500 0.115 178 / 0.20);

  --color-danger:     oklch(0.520 0.200 25);
  --color-danger-hi:  oklch(0.470 0.200 25);
  --color-danger-ink: oklch(0.990 0.008 25);

  --color-still:  oklch(0.500 0.150 145);
  --color-walk:   oklch(0.480 0.170 295);
  --color-move:   oklch(0.500 0.160 250);
  --color-shake:  oklch(0.530 0.140 80);
  --color-fall:   oklch(0.520 0.200 25);
  --color-idle:   oklch(0.520 0.014 258);

  --tint-primary:oklch(0.500 0.115 178 / .10);
  --tint-primary-line:oklch(0.500 0.115 178 / .34);
  --tint-danger:oklch(0.520 0.200 25 / .10);
  --tint-danger-line:oklch(0.520 0.200 25 / .34);
  --tint-still:oklch(0.500 0.150 145 / .14);
  --tint-shake:oklch(0.530 0.140 80 / .16);
  --tint-fall:oklch(0.520 0.200 25 / .14);

  /* 浅色底上的阴影必须比深色淡得多，否则整页显脏 */
  --shadow-1:0 1px 1px oklch(0 0 0/.05), 0 2px 6px oklch(0 0 0/.05);
  --shadow-2:0 1px 1px oklch(0 0 0/.06), 0 4px 10px oklch(0 0 0/.06), 0 12px 28px oklch(0 0 0/.06);
  --shadow-3:0 2px 2px oklch(0 0 0/.08), 0 10px 24px oklch(0 0 0/.10), 0 32px 64px oklch(0 0 0/.12);

  --appbar-bg:oklch(0.988 0.002 258 / .82);
  --bg-wash:radial-gradient(1100px 520px at 8% -12%, oklch(0.930 0.010 258 / .70), transparent 70%);
  --stage-veil:linear-gradient(180deg, oklch(0.998 0.002 258), transparent);
  --ball-from:oklch(0.998 0.002 258);
  --ball-to:oklch(0.940 0.005 258);
  --video-bg:oklch(0.930 0.004 258);
  --modal-backdrop:oklch(0.300 0.010 258 / .32);
  --sheen:transparent;
}

*{box-sizing:border-box;margin:0}
html{-webkit-text-size-adjust:100%}
/* [hidden] 来自浏览器默认样式表（UA 样式），**任何作者样式里的 display 都能盖掉它**：
   `.card{display:flex}` 一写，#card3d 上那个 hidden 就形同虚设 —— 3D 还没初始化完，
   一张空卡片已经先亮在那里了（本仓库经验库里第 1 类高频真 bug）。
   所以在最前面显式提权一次，后面所有 display 都盖不掉它。 */
[hidden]{display:none !important}

body{
  min-height:100vh;
  background-color:var(--color-bg);
  /* 一处极淡的顶光，给纯色底一点纵深；不用蓝紫渐变 */
  background-image:var(--bg-wash);
  background-attachment:fixed;
  color:var(--color-text);
  font:400 var(--fs-body)/1.6 var(--font-body);
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
}
h1,h2,h3{font-family:var(--font-display);text-wrap:balance}
.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.shell{max-width:var(--w-max);margin:0 auto;padding-inline:var(--sp-8)}
/* 三类辅助文字，权重依次退下去 */
.src{font:400 var(--fs-meta)/1.5 var(--font-mono);color:var(--color-text-faint)}
.hint{font:400 var(--fs-meta)/1.7 var(--font-body);color:var(--color-text-muted)}
.hint b{color:var(--color-text);font-weight:600}
.empty{font:400 var(--fs-meta)/1.6 var(--font-body);color:var(--color-text-faint);padding:var(--sp-3) 0}
.eyebrow{
  display:inline-flex;align-items:center;gap:var(--sp-2);
  font:500 var(--fs-meta)/1 var(--font-mono);
  letter-spacing:.18em;text-transform:uppercase;color:var(--color-text-faint);
}
.eyebrow::before{content:"";width:20px;height:1px;background:var(--color-line-strong)}
.panel-head{display:flex;align-items:baseline;justify-content:space-between;gap:var(--sp-4);flex-wrap:wrap}
.panel-title{font:600 var(--fs-h3)/1.3 var(--font-display);letter-spacing:-.01em}

/* ==========================================================================
   顶栏：一屏里最安静的一层。它只回答"连上没有、跑多快"，不抢注意力。
   ========================================================================== */
.appbar{
  position:sticky;top:0;z-index:40;
  background:var(--appbar-bg);
  backdrop-filter:blur(16px) saturate(140%);
  border-bottom:1px solid var(--color-line);
}
.appbar .shell{
  min-height:56px;display:flex;align-items:center;gap:var(--sp-6);
  flex-wrap:wrap;padding-block:var(--sp-2);
}
.brand{display:flex;align-items:center;gap:var(--sp-3);min-width:0}
.brand .mark{
  width:26px;height:26px;flex:none;
  border-radius:var(--radius-control);
  border:1px solid var(--tint-primary-line);
  background:var(--tint-primary);
  display:grid;place-items:center;color:var(--color-action-primary);
}
.brand .mark svg{width:15px;height:15px;display:block}
.brand b{font:600 var(--fs-body)/1.2 var(--font-display);letter-spacing:-.01em}
.brand span{
  font:500 10.5px/1 var(--font-mono);letter-spacing:.16em;text-transform:uppercase;
  color:var(--color-text-faint);
}
.appbar .gap{flex:1 1 auto}

.status{display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap}
.pill{
  display:inline-flex;align-items:center;gap:var(--sp-2);
  height:28px;padding:0 var(--sp-3);
  border-radius:var(--radius-control);
  border:1px solid var(--color-line);
  background:var(--color-surface);
  font:500 var(--fs-meta)/1 var(--font-mono);
  color:var(--color-text-muted);white-space:nowrap;
}
.pill i{width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
.pill.on{color:var(--color-action-primary);border-color:var(--tint-primary-line);background:var(--tint-primary)}
.pill.off{color:var(--color-fall);border-color:var(--tint-danger-line);background:var(--tint-danger)}
.pill.off i{animation:breathe 1.7s ease-in-out infinite}
@keyframes breathe{0%,100%{opacity:1}50%{opacity:.22}}

/* ==========================================================================
   Hero 舞台：首屏只讲一件事 —— 板子现在在干什么。
   --------------------------------------------------------------------------
   它刻意**不是一张卡片**：是一整条横贯页面的带子，直接坐在页面底色上，
   只有它配得上 48px 的字号和整片活动色染底。
   ⚠️ 带子内部**一个卡片都不放**（第一版把图表和 AI 回复做成了卡片，
   于是"带子里又套卡片"，盒子套盒子就是"乱"的来源）。内部一律用
   发丝线 + 留白来分块。
   ========================================================================== */
.stage{
  border-bottom:1px solid var(--color-line);
  padding-block:var(--sp-16);
  background-image:
    radial-gradient(880px 320px at 4% 0%, rgb(var(--act-rgb) / .10), transparent 64%),
    var(--stage-veil);
  transition:background-image .7s var(--ease);
}
.stage-head{
  display:flex;align-items:baseline;justify-content:space-between;
  gap:var(--sp-6);flex-wrap:wrap;margin-bottom:var(--sp-8);
}

/* 三列：读数 | 姿态 | AI。列宽**刻意不等**，比例按"内容真的填得满"调 ——
   列给太宽，右边会拖出一条比 32px 栅格间距宽得多的空档，三列就不像一行了。 */
.stage-grid{
  display:grid;gap:var(--sp-8);align-items:stretch;
  grid-template-columns:minmax(0,1fr) minmax(0,.82fr) minmax(0,1fr);
}
.hero-readout{display:grid;grid-template-columns:168px minmax(0,1fr);gap:var(--sp-6);align-items:center}
.hero-ring{position:relative;width:168px;height:168px;flex:none}
.hero-ring svg{width:100%;height:100%;display:block;transform:rotate(135deg)}
.hero-ring .track{fill:none;stroke:var(--color-surface-3);stroke-width:9;stroke-linecap:round;stroke-dasharray:263.9 351.9}
.hero-ring .val{
  fill:none;stroke:var(--color-idle);stroke-width:9;stroke-linecap:round;
  stroke-dasharray:0 351.9;
  transition:stroke-dasharray .5s var(--ease),stroke .4s ease;
}
.ring-mid{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:var(--sp-1);
}
.ring-mid b{font:600 var(--fs-h2)/1 var(--font-mono);font-variant-numeric:tabular-nums}
.ring-mid span{font:500 11px/1 var(--font-mono);letter-spacing:.14em;color:var(--color-text-faint)}

/* hero 标题：全页最大的字。48px = 正文 15px 的 3.2 倍。 */
.hero-word{
  font:700 var(--fs-hero)/1.02 var(--font-display);
  letter-spacing:-.02em;color:var(--act-color);
  transition:color .45s ease;
}
.hero-word.pop{animation:pop .45s var(--ease)}
@keyframes pop{from{opacity:.15;transform:translateY(6px)}to{opacity:1;transform:none}}
/* 读数行：三个「小标签 + 大数字」，标签比数字小两级，层级一眼看得出 */
.hero-metrics{display:flex;gap:var(--sp-8);margin-top:var(--sp-6);flex-wrap:wrap}
.hero-metrics>div{display:flex;flex-direction:column;gap:var(--sp-2)}
.hero-metrics dt{
  font:500 10.5px/1 var(--font-mono);letter-spacing:.14em;
  text-transform:uppercase;color:var(--color-text-faint);
}
.hero-metrics dd{font:600 var(--fs-h3)/1.1 var(--font-mono);font-variant-numeric:tabular-nums}
.hero-metrics dd.is-text{font:600 var(--fs-body)/1.3 var(--font-body)}

.hero-attitude{display:grid;gap:var(--sp-6);align-content:start}
/* flex 而不是 grid：AI 那一列要**撑满整行高度**，否则右边留一块空洞 */
.hero-ai{display:flex;flex-direction:column}

/* ---- 姿态球：几何尺寸与旧版同一套手感（BUBBLE_MAX 是按这个半径定的） ----
   刻意**左对齐**（不 margin:auto 居中）：上面那行标题是左对齐的，
   球在中间飘着会和标题错开，三列看起来就不像一行了。 */
.ball{
  position:relative;width:100%;max-width:210px;aspect-ratio:1;
  border-radius:50%;border:1px solid var(--color-line);
  background:radial-gradient(circle at 50% 38%, var(--ball-from), var(--ball-to) 72%);
}
.ball .ring2{
  position:absolute;left:50%;top:50%;width:34%;height:34%;
  transform:translate(-50%,-50%);border-radius:50%;border:1px dashed var(--color-line);
}
.ball .cross::before,.ball .cross::after{content:"";position:absolute;background:var(--color-line)}
.ball .cross::before{left:50%;top:14%;bottom:14%;width:1px}
.ball .cross::after{top:50%;left:14%;right:14%;height:1px}
.ball .cross2{position:absolute;left:50%;top:50%;width:52%;height:52%;transform:translate(-50%,-50%) rotate(45deg)}
.ball .cross2::before,.ball .cross2::after{content:"";position:absolute;background:var(--color-line-strong);opacity:.45}
.ball .cross2::before{left:50%;top:0;bottom:0;width:1px}
.ball .cross2::after{top:50%;left:0;right:0;height:1px}
.ball .dot{
  position:absolute;left:50%;top:50%;width:20px;height:20px;margin:-10px 0 0 -10px;
  border-radius:50%;background:var(--color-still);
  /* 小球短且线性：0.15s linear 让它贴着数据走，没有加速-减速的假动作。
     原来 .3s ease-out 配 500ms 一次的数据更新，观感是"猛冲一下然后停住"。 */
  transition:transform .15s linear,background .4s,box-shadow .4s;
}
.bars{display:flex;flex-direction:column;gap:var(--sp-3)}
.bar{display:grid;grid-template-columns:16px minmax(0,1fr) 56px;gap:var(--sp-3);align-items:center}
.bar em{font:600 var(--fs-meta)/1 var(--font-mono);font-style:normal}
.bar .t{position:relative;height:6px;border-radius:var(--radius-control);background:var(--color-surface-3)}
.bar .t i{position:absolute;top:0;height:100%;border-radius:var(--radius-control);left:50%;width:0;transition:left .3s,width .3s,background .3s}
.bar .t::after{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;transform:translateX(-.5px);background:var(--color-line-strong)}
.bar .v{text-align:right;font:500 var(--fs-meta)/1 var(--font-mono)}

/* ---- AI 回复：**引用块**，不是卡片 ----
   一根左侧竖条 + 缩进就够了。第一版给它套了卡片（边框 + 底色 + 阴影），
   在 hero 带子里就是"卡片里套卡片"，纯属加噪。 */
.reply{
  flex:1 1 auto;position:relative;
  display:flex;flex-direction:column;gap:var(--sp-3);
  padding-left:var(--sp-4);border-left:2px solid var(--color-line-strong);
  min-height:120px;
}
.reply .tag{
  /* 字距收到 .08em：这个标签是"拉丁 + 中文"混排（AI 回复），
     等宽字体上 .16em 的字距会把 A 和 I 拉得像两个词，中文也跟着散。 */
  font:600 11px/1 var(--font-mono);letter-spacing:.08em;text-transform:uppercase;
  color:var(--color-text-faint);
}
.reply .txt{font-size:var(--fs-body);color:var(--color-text-muted);white-space:pre-wrap;word-break:break-word}
.reply.has{border-left-color:var(--color-shake)}
.reply.has .tag{color:var(--color-shake)}
.reply.has .txt{color:var(--color-text)}
.reply-foot{
  margin-top:auto;display:flex;justify-content:space-between;gap:var(--sp-4);flex-wrap:wrap;
  font:400 11.5px/1.5 var(--font-mono);color:var(--color-text-faint);
}
.reply.pending::after{
  content:"";position:absolute;right:0;top:0;
  width:13px;height:13px;border-radius:50%;
  border:2px solid var(--color-surface-3);border-top-color:var(--color-action-primary);
  animation:spin .9s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* hero 带子内部的分块：发丝线 + 留白，不用卡片 */
.stage-block{
  margin-top:var(--sp-8);padding-top:var(--sp-8);
  border-top:1px solid var(--color-line);
  display:flex;flex-direction:column;gap:var(--sp-4);
}
.stage-actions{
  display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap;
  margin-top:var(--sp-8);padding-top:var(--sp-8);border-top:1px solid var(--color-line);
}
.cmdbar{display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap;flex:1 1 auto}
/* 破坏性指令（格式化 SD 卡）与安全指令之间**留一道可伸缩的间隔**，
   把它顶到最右边 —— 一排按钮里挨着「读取存储信息」，误点代价太大。 */
.cmdbar .cmd-gap{flex:1 1 var(--sp-6)}

/* ==========================================================================
   区域：每一块上下各留 64px，块与块之间用一条发丝线分隔。
   宁可页面长一点，也不要挤成一坨 —— 信息密度要能喘气。
   ========================================================================== */
.region{padding-block:var(--sp-16);border-bottom:1px solid var(--color-line)}
.region:last-of-type{border-bottom:0}
.region-head{
  display:flex;align-items:baseline;justify-content:space-between;
  gap:var(--sp-6);flex-wrap:wrap;margin-bottom:var(--sp-8);
}
.region-head h2{font:600 var(--fs-h2)/1.2 var(--font-display);letter-spacing:-.02em}
/* 区域说明用等宽小字，靠右 —— 它是注脚，不该跟标题争大小。
   第一版写成了 15px 正文段落，三段加起来就是三坨噪音。 */
.region-sub{
  font:400 var(--fs-meta)/1.6 var(--font-mono);color:var(--color-text-faint);
  max-width:64ch;text-align:right;
}
.region-tools{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}

/* 用 flex + flex-grow 而不是固定列数：某张卡被 JS 隐藏时，
   同排的卡会自动占满整行，不会留一个空洞。
   align-items:flex-start（不是 stretch）—— 同排两张卡内容量差很多时，
   拉平高度只会让短的那张底下空出一大块，比高低不齐更难看。 */
.cols{display:flex;flex-wrap:wrap;gap:var(--sp-6);align-items:flex-start}
.cols>*{flex:1 1 430px;min-width:0}
.cols>.lead{flex:1.3 1 520px}

/* 卡片：**平面 + 发丝线 + 一档轻阴影**。
   第一版每张卡都带渐变和 shadow-2，六张卡同时喊"我在这一层"，
   结果 hero 反而压不住它们。层次要靠"hero 更重"来给，不是"卡片更重"。 */
.card{
  display:flex;flex-direction:column;gap:var(--sp-4);
  padding:var(--sp-6);
  border:1px solid var(--color-line);
  border-radius:var(--radius-card);
  background:var(--color-surface);
  box-shadow:var(--shadow-1);
}
.card-head{display:flex;align-items:baseline;justify-content:space-between;gap:var(--sp-4);flex-wrap:wrap}
.card-head h3{font:600 var(--fs-h3)/1.2 var(--font-display);letter-spacing:-.015em}
.card-head h3.sm{font-size:var(--fs-body)}

/* ---- 设备列表（多板场景；只有一台时整块不出现） ----
   做成**表格式的行**而不是卡片：设备是重复项，一屏可能有 20 台，
   列对齐比卡片好扫读，也不会出现"很宽的卡片里挤着一行小字、右边空一大片"。
   选中态用左侧 2px 强调条 + 淡底，不用整块高亮（整块高亮会跟 hover 撞车）。 */
.devlist{
  display:flex;flex-direction:column;
  border:1px solid var(--color-line);border-radius:var(--radius-card);
  background:var(--color-surface);overflow:hidden;
}
.dev{
  display:grid;align-items:center;gap:var(--sp-4);
  grid-template-columns:18px 8px minmax(110px,180px) minmax(150px,240px) minmax(0,1fr);
  padding:var(--sp-3) var(--sp-4);
  border-bottom:1px solid var(--color-line);
  cursor:pointer;transition:background .16s;
}
.dev:last-child{border-bottom:0}
.dev:hover{background:var(--color-surface-2)}
.dev.sel{background:var(--tint-primary);box-shadow:inset 2px 0 0 var(--color-action-primary)}
/* 设备行是"可点的 div"，必须自己把键盘可达性补上：
   给它 tabindex/role（见 renderDevices），这里给焦点样式。
   用 outline + 负 offset，不去动 box-shadow（那被选中态的强调条占着）。 */
.dev:focus-visible{outline:2px solid var(--color-action-primary);outline-offset:-2px}
.dev input[type=checkbox]{accent-color:var(--color-action-primary);width:15px;height:15px;cursor:pointer}
.dev .dot{width:8px;height:8px;border-radius:50%}
.dev .dot.off{animation:breathe 1.7s ease-in-out infinite}
.dev .did{font:600 var(--fs-body)/1.3 var(--font-display);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dev .meta{
  font:400 11.5px/1.5 var(--font-mono);color:var(--color-text-faint);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}

/* ==========================================================================
   控件：**两级**。
   ---- 主操作（.primary）：实心青绿，一屏只允许一个 ----
   ---- 其余一律"退下去"：透明底 + 发丝线 + 次级文字色 ----
   第一版所有按钮都是"深底 + 边框 + 文字"，8 个按钮重量完全一样，
   主操作跳不出来，一排看过去就是一团。
   ========================================================================== */
button{
  font:600 var(--fs-body)/1 var(--font-body);
  height:36px;padding:0 var(--sp-4);
  border-radius:var(--radius-control);
  border:1px solid var(--color-line);
  background:transparent;
  color:var(--color-text-muted);
  cursor:pointer;
  transition:background .16s,border-color .16s,color .16s,transform .06s;
}
button:hover:not(:disabled){
  background:var(--color-surface-2);color:var(--color-text);border-color:var(--color-line-strong);
}
button:active:not(:disabled){transform:translateY(1px)}
button:focus-visible{outline:none;box-shadow:var(--ring)}
button:disabled{opacity:.38;cursor:not-allowed}
button.primary{
  background:var(--color-action-primary);
  border-color:var(--color-action-primary);
  color:var(--color-action-primary-ink);
}
button.primary:hover:not(:disabled){
  background:var(--color-action-primary-hi);border-color:var(--color-action-primary-hi);
  color:var(--color-action-primary-ink);
}
button.danger{color:var(--color-fall);border-color:var(--tint-danger-line)}
button.danger:hover:not(:disabled){background:var(--tint-danger);color:var(--color-fall);border-color:var(--color-danger)}
button.sm{height:28px;padding:0 var(--sp-3);font-size:var(--fs-meta)}
/* 图标按钮：和状态胶囊同高（28px），走"退下去"那一档 ——
   主题切换是工具，不该跟主操作抢注意力。 */
button.icon{width:32px;height:28px;padding:0;display:grid;place-items:center;flex:none}
button.icon svg{width:15px;height:15px;display:block}
/* 图标显示的是"点了会变成什么"：明亮时显示月亮（去深色），深色时显示太阳 */
html[data-theme="light"] button.icon .i-sun{display:none}
html[data-theme="dark"] button.icon .i-moon{display:none}

input[type=text],input[type=password],input[type=number],select{
  height:36px;padding:0 var(--sp-3);
  border-radius:var(--radius-control);
  border:1px solid var(--color-line);
  background:var(--color-bg);           /* 输入框比卡片更深 = 凹进去 */
  color:var(--color-text);
  font:400 var(--fs-body)/1 var(--font-body);
}
input::placeholder{color:var(--color-text-faint)}
input:focus,select:focus{outline:none;border-color:var(--color-action-primary);box-shadow:var(--ring)}
select{padding-right:var(--sp-2);font-family:var(--font-mono);font-size:var(--fs-meta)}
.field{display:flex;flex-direction:column;gap:var(--sp-2)}
.field>label{font:500 10.5px/1 var(--font-mono);letter-spacing:.14em;text-transform:uppercase;color:var(--color-text-faint)}
.formrow{display:flex;gap:var(--sp-4);flex-wrap:wrap;align-items:flex-end}
.actions{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}

/* ==========================================================================
   列表：指令记录 / 事件流 / 文件。做成紧凑的"数据表"，
   行与行之间用发丝线，不用卡片套卡片。
   ========================================================================== */
.list{display:flex;flex-direction:column}
.item{
  display:flex;align-items:baseline;gap:var(--sp-3);flex-wrap:wrap;
  padding:var(--sp-3) 0;
  border-bottom:1px solid var(--color-line);
  font-size:var(--fs-meta);color:var(--color-text-muted);
  /* ⚠️ 指令参数是一整串**没有空格**的 JSON（{"n":3,"on_ms":80,"off_ms":80,...}）。
     默认情况下它算一个"不可断词"，flex 子项的 min-width:auto 又不允许收缩到
     min-content 以下 —— 于是它把整页撑宽。实测 320px 下溢出 44px，
     而 set_config 的参数更长（ssid/url/period_ms），390px 手机上也会破。
     overflow-wrap:anywhere 允许在任意位置断行（并且会**降低 min-content**，
     这正是 flex 能收缩的前提）；min-width:0 是配套的那一半。 */
  overflow-wrap:anywhere;
}
.item>*{min-width:0}
.item:last-child{border-bottom:0}
.item .name{color:var(--color-text);font-weight:600;font-family:var(--font-display)}
.item .meta{color:var(--color-text-faint);font-size:11.5px;font-family:var(--font-mono)}
.st{
  font:500 11px/1 var(--font-mono);letter-spacing:.06em;
  padding:4px var(--sp-2);border-radius:var(--radius-control);
  white-space:nowrap;flex:none;
}
.st-queued{background:var(--color-surface-3);color:var(--color-text-muted)}
.st-sent{background:var(--tint-shake);color:var(--color-shake)}
.st-done{background:var(--tint-still);color:var(--color-still)}
.st-failed,.st-timeout{background:var(--tint-fall);color:var(--color-fall)}
#feed{max-height:340px;overflow-y:auto}
#feed .t{color:var(--color-text-faint);font-size:11.5px;flex:none;font-family:var(--font-mono)}
#feed .k{flex:none;width:7px;height:7px;border-radius:50%;margin-top:6px}
.k-info{background:var(--color-text-faint)}
.k-motion{background:var(--color-move)}
.k-shake{background:var(--color-shake)}
.k-fall,.k-alert{background:var(--color-fall)}
.k-ask{background:var(--color-walk)}
.k-cmd{background:var(--color-action-primary)}

/* ---- 曲线 ---- */
#cv{width:100%;height:144px;display:block;border-radius:var(--radius-card)}
.legend{display:flex;gap:var(--sp-6);font:400 11.5px/1 var(--font-mono);color:var(--color-text-faint);flex-wrap:wrap}
.legend i{display:inline-block;width:12px;height:3px;border-radius:var(--radius-control);vertical-align:middle;margin-right:var(--sp-2)}

/* ---- 摄像头 ---- */
.camwrap{display:grid;grid-template-columns:minmax(0,1fr) 224px;gap:var(--sp-6)}
.camview{
  position:relative;aspect-ratio:4/3;max-height:340px;
  display:flex;align-items:center;justify-content:center;
  background:var(--video-bg);border:1px solid var(--color-line);
  border-radius:var(--radius-card);overflow:hidden;
}
.camview img{width:100%;height:100%;object-fit:contain;display:block}
.camview .nosig{color:var(--color-text-faint);font:400 var(--fs-meta)/1.7 var(--font-body);text-align:center;padding:var(--sp-6)}
.camside{display:flex;flex-direction:column;gap:var(--sp-3);align-items:flex-start}
.shot{
  display:flex;align-items:center;gap:var(--sp-3);
  padding:var(--sp-2);border-radius:var(--radius-control);
  border:1px solid var(--color-line);background:var(--color-surface);
}
.shot img{width:56px;height:42px;object-fit:cover;border-radius:var(--radius-control);background:var(--video-bg);flex:none}
.shot .nm{font:400 var(--fs-meta)/1.5 var(--font-mono);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---- 存储占用圆环 ---- */
.ringwrap{display:flex;align-items:center;gap:var(--sp-6);flex-wrap:wrap}
.ring{
  position:relative;width:104px;height:104px;flex:0 0 auto;cursor:pointer;
  border-radius:50%;transition:transform .16s;
}
.ring:hover{transform:scale(1.03)}
.ring:focus-visible{outline:2px solid var(--color-action-primary);outline-offset:2px}
.ring svg{display:block;transform:rotate(-90deg)}
.ring .ringtxt{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;line-height:1.15;
}
.ring .ringtxt b{font:600 var(--fs-h3)/1 var(--font-mono)}
.ring .ringtxt span{font:500 10.5px/1 var(--font-mono);letter-spacing:.14em;color:var(--color-text-faint)}
.ringmeta{font:400 var(--fs-meta)/1.9 var(--font-mono);color:var(--color-text-muted)}
.ringmeta .k{color:var(--color-text-faint)}

footer{
  border-top:1px solid var(--color-line);
  padding-block:var(--sp-8);
  color:var(--color-text-faint);font:400 11.5px/1.9 var(--font-mono);
}

/* ==========================================================================
   弹窗：全站唯一用 14px 圆角的地方。用原生 <dialog>，
   不用 window.confirm() —— 系统弹窗在深色界面上是一块白方块，
   而且会阻塞整个页面（SSE 推送和 3D 渲染全停）。
   ========================================================================== */
dialog.modal{
  width:min(448px,calc(100vw - 32px));
  padding:0;border:1px solid var(--color-line-strong);
  border-radius:var(--radius-modal);
  background:var(--color-surface);color:var(--color-text);
  box-shadow:var(--shadow-3), inset 0 1px 0 var(--sheen);
}
dialog.modal::backdrop{background:var(--modal-backdrop);backdrop-filter:blur(3px)}
.modal-body{padding:var(--sp-8) var(--sp-8) var(--sp-6);display:grid;gap:var(--sp-3)}
.modal-body h3{font:600 var(--fs-h2)/1.25 var(--font-display);letter-spacing:-.015em}
.modal-body p{font-size:var(--fs-body);color:var(--color-text-muted)}
.modal-foot{display:flex;justify-content:flex-end;gap:var(--sp-3);padding:0 var(--sp-8) var(--sp-8)}

/* ==========================================================================
   响应式：窄屏优先保证"读得到、点得到"，不追求信息量
   ========================================================================== */
@media(max-width:1280px){
  .stage-grid{grid-template-columns:minmax(0,1fr) minmax(0,.86fr)}
  .hero-ai{grid-column:1 / -1;margin-top:var(--sp-2)}
  .reply{min-height:0}
}
@media(max-width:1024px){
  .stage-grid{grid-template-columns:minmax(0,1fr)}
  .camwrap{grid-template-columns:minmax(0,1fr)}
  .cols>*,.cols>.lead{flex-basis:100%}
  .region-sub{text-align:left}
}
@media(max-width:760px){
  .shell{padding-inline:var(--sp-4)}
  .stage{padding-block:var(--sp-12)}
  .region{padding-block:var(--sp-12)}
  /* 设备行在窄屏折成三行：复选框+状态点 | 设备名 / 状态 / 活动，都落在第 3 列 */
  .dev{grid-template-columns:18px 8px minmax(0,1fr);gap:var(--sp-2) var(--sp-3)}
  .dev .did,.dev .meta{grid-column:3}
  /* hero 字号在窄屏**不缩**：规则要求 hero ≥ 正文 × 3（15 × 3 = 45），
     原来缩到 --fs-h2(24) 就是 1.6 倍 —— 直接违规（2026-09-26 补空状态断言时抓到）。
     正解不是缩字号，是**改成上下堆叠**：圆环在上、标题在下，
     48px 的标题就有整行宽度可用（最长 4 个汉字 = 192px < 288px，320px 屏也放得下）。 */
  .hero-readout{grid-template-columns:minmax(0,1fr);justify-items:start;gap:var(--sp-4)}
  .hero-ring{width:112px;height:112px}
  /* 环变小了，里面的读数也要跟着小，否则 "0.00 g" 会顶到环的描边上 */
  .ring-mid b{font-size:var(--fs-h3)}
  .hero-metrics{gap:var(--sp-6)}
  #cv{height:140px}
  .card{padding:var(--sp-4)}
  .appbar .shell{min-height:52px;gap:var(--sp-3)}
  /* 手机上输入框小于 16px 时，iOS 聚焦会强制放大页面 —— 很难受 */
  input[type=text],input[type=password],input[type=number],select{font-size:16px}
}

/* ==========================================================================
   动效偏好：系统里开了"减少动态效果"就把所有动画/过渡压到几乎为 0。
   这个页面有呼吸点、活动词淡入、曲线过渡、3D slerp —— 对前庭敏感的人是干扰。
   只压缩**时长**，不删动画本身：布局和最终状态完全不变。
   ========================================================================== */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{
    animation-duration:.01ms !important;
    animation-iteration-count:1 !important;
    transition-duration:.01ms !important;
  }
}
</style>
</head>
<body>

<header class="appbar">
  <div class="shell">
    <div class="brand">
      <span class="mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <path d="M12 2.5 20 7v10l-8 4.5L4 17V7z"></path><path d="M12 12v9.5M12 12 4 7M12 12l8-5"></path>
        </svg>
      </span>
      <div>
        <b>Ego Link</b>
        <span>实时仪表盘</span>
      </div>
    </div>
    <div class="gap"></div>
    <div class="status">
      <span id="dev" class="pill off"><i></i>离线</span>
      <span id="hzp" class="pill">— Hz</span>
      <span id="postsp" class="pill">↑0 帧</span>
      <button id="theme" class="icon" type="button" title="切换主题" aria-label="切换主题">
        <svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4.2"></circle>
          <path d="M12 2.6v2.2M12 19.2v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.6 12h2.2M19.2 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"></path>
        </svg>
        <svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
          <path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z"></path>
        </svg>
      </button>
    </div>
  </div>
</header>

<main class="shell">
  <section class="region" id="devcard" hidden>
    <div class="region-head">
      <h2>设备</h2>
      <div class="region-tools">
        <span class="src" id="devcount"></span>
        <button id="devall" class="sm">全选</button>
      </div>
    </div>
    <div class="devlist" id="devs"></div>
    <p class="hint" style="margin-top:var(--sp-3)">勾选多台后，远程指令会<b>同时</b>下发给它们；一台都不勾 = 只发给当前查看的那台。</p>
  </section>

  <!-- ===================================================================
       Hero 舞台。整页最重要的一块，也是唯一不在卡片里的内容。
       =================================================================== -->
  <section class="stage">
    <div class="stage-head">
      <span class="eyebrow">实时状态 · 最近 8 秒</span>
      <span class="src">数据来源 <span id="src">—</span></span>
    </div>

    <div class="stage-grid">
      <div class="hero-readout">
        <div class="hero-ring">
          <svg viewBox="0 0 140 140" aria-hidden="true">
            <circle class="track" cx="70" cy="70" r="56"></circle>
            <circle class="val" id="ring" cx="70" cy="70" r="56"></circle>
          </svg>
          <div class="ring-mid">
            <b id="abs">0.00 g</b>
            <span>合加速度</span>
          </div>
        </div>
        <div>
          <h1 class="hero-word" id="act">…</h1>
          <dl class="hero-metrics">
            <div><dt>步数 / 8s</dt><dd id="steps">0</dd></div>
            <div><dt>晃动 / 8s</dt><dd id="shakes">0</dd></div>
            <div><dt>AI 来源</dt><dd class="is-text" id="mode">规则AI</dd></div>
          </dl>
        </div>
      </div>

      <div class="hero-attitude">
        <div class="panel-head">
          <span class="panel-title">姿态</span>
          <span class="src mono" id="tilt">倾角 —</span>
        </div>
        <div class="ball">
          <div class="cross"></div>
          <div class="cross2"></div>
          <div class="ring2"></div>
          <div class="dot" id="ball"></div>
        </div>
        <div class="bars">
          <div class="bar"><em style="color:var(--color-fall)">X</em><div class="t"><i id="bx"></i></div><span class="v mono" id="vx">0.00</span></div>
          <div class="bar"><em style="color:var(--color-still)">Y</em><div class="t"><i id="by"></i></div><span class="v mono" id="vy">0.00</span></div>
          <div class="bar"><em style="color:var(--color-move)">Z</em><div class="t"><i id="bz"></i></div><span class="v mono" id="vz">0.00</span></div>
        </div>
      </div>

      <div class="hero-ai">
        <div class="reply" id="replybox">
          <span class="tag">AI 回复</span>
          <div class="txt" id="reply">还没有提问。</div>
          <div class="reply-foot">
            <span>按板子 BOOT 键提问</span>
            <span>回复同时显示在板子屏幕上</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 主操作**放在曲线之前**：整条 hero 带子约 900px 高，
         曲线是"细看"的内容，主操作是"要用的"内容 —— 放在最后会被
         900px 高的笔记本切在折线以下（规则要求主操作必须跳出来）。 -->
    <div class="stage-actions">
      <span class="cmdbar" id="cmdbts"></span>
      <span id="cmdhint" class="hint"></span>
    </div>

    <div class="stage-block">
      <div class="panel-head">
        <span class="panel-title">合加速度 |a|</span>
        <span class="src">最近 8 秒 · 服务端降采样</span>
      </div>
      <canvas id="cv" role="img" aria-label="合加速度 |a| 最近 8 秒的曲线"></canvas>
      <div class="legend">
        <span><i style="background:var(--color-move)"></i>|a| 曲线</span>
        <span><i style="background:var(--color-text-faint)"></i>1g 参考线</span>
        <span><i style="background:var(--color-fall)"></i>失重阈值 0.35g（疑似跌落）</span>
      </div>
    </div>
  </section>

  <section class="region">
    <div class="region-head">
      <h2>远程操作</h2>
      <p class="region-sub">指令搭在「下一帧遥测的响应」里下发，板子在再下一帧回传结果 —— 一次真实的硬件往返。</p>
    </div>
    <div class="cols">
      <div class="card lead" id="cardcmd">
        <div class="card-head">
          <h3>指令记录</h3>
          <span class="src">最近 8 条</span>
        </div>
        <div class="formrow">
          <div class="field">
            <label for="orientsel">方向档位 oN</label>
            <select id="orientsel"></select>
          </div>
          <button id="orientapply">应用档位</button>
          <span id="orientnow" class="hint"></span>
        </div>
        <div class="list" id="cmds"></div>
      </div>

      <div class="card" id="cardfeed">
        <div class="card-head">
          <h3>事件流</h3>
          <span class="src" id="evcount"></span>
        </div>
        <div id="feed"></div>
      </div>
    </div>
  </section>

  <section class="region">
    <div class="region-head">
      <h2>设备与画面</h2>
      <p class="region-sub">板子是 HTTP 客户端，没有自己的服务端，所以画面由板子推上来、网页从这里取。</p>
    </div>
    <div class="cols">
      <div class="card lead" id="cardcam">
        <div class="card-head">
          <h3>摄像头</h3>
          <span class="src" id="camwho">—</span>
        </div>
        <div class="camwrap">
          <div class="camview">
            <!-- 一开始就 hidden：没有 src 时浏览器会画一个"图片裂了"的破图标，
                 比什么都不显示还难看。收到第一帧再让它出现。 -->
            <img id="camimg" alt="" hidden>
            <div class="nosig" id="camnosig">点右侧「开启实时画面」<br>
              （板子离线 / 摄像头自检没过时不会有画面）</div>
          </div>
          <div class="camside">
            <button id="camshot">拍照 → 存进板子 SD 卡</button>
            <button id="camlive">开启实时画面</button>
            <div class="field">
              <label for="camfps">帧率</label>
              <select id="camfps" title="板子推帧的速度，改这里会下发命令给板子。调低省 WiFi 带宽、更稳；调高更跟手。注意板子一帧要「等帧 + 开一条 TCP + POST 27KB」，实测到不了很高 —— 选「丝滑」也可能只有每秒几帧，这是链路上限，不是设置没生效。">
                <option value="1">省流 · 1 帧/秒</option>
                <option value="2" selected>标准 · 2 帧/秒</option>
                <option value="4">流畅 · 4 帧/秒</option>
                <option value="6">丝滑 · 6 帧/秒</option>
              </select>
            </div>
            <div class="hint">
              实时画面是板子<b>推</b>上来的，帧率<b>由板子决定</b>（在上面选）。
              关掉可省 WiFi 带宽（OV3660 的 JPEG 是 1280x720，一帧约 27KB）。
            </div>
            <div class="hint" id="camstat">—</div>
          </div>
        </div>
        <div class="card-head" style="margin-top:var(--sp-2)">
          <h3 class="sm">本机照片存档</h3>
          <span class="src" id="shotwho">—</span>
        </div>
        <div class="list" id="shotlist"></div>
      </div>

      <div class="card" id="card3d" hidden>
        <div class="card-head">
          <h3>板子姿态 · 3D</h3>
          <span class="src" id="d3src">—</span>
        </div>
        <div class="field">
          <label for="pollsel">页面取数频率</label>
          <select id="pollsel" title="页面从服务器取数的频率。想更跟手就选 60Hz —— 但前提是板端上报周期也调到 50ms（板子设置里的「灵敏度」），两个都够快才真跟手">
            <option value="16">60 Hz</option>
            <option value="33">30 Hz</option>
            <option value="50">20 Hz</option>
            <option value="100" selected>10 Hz</option>
            <option value="200">5 Hz</option>
            <option value="500">2 Hz</option>
            <option value="2000">0.5 Hz</option>
          </select>
        </div>
        <canvas id="board3d" role="img" aria-label="板子姿态 3D 示意" style="width:100%;height:280px;display:block"></canvas>
        <div class="hint">橙色小条 = 板子顶边（远的那条）；地面网格固定不动，板子跟着实时姿态转。
          画面按 60Hz 平滑（中间做 slerp），所以取数慢一点也不会一跳一跳。</div>
      </div>
    </div>
  </section>

  <section class="region">
    <div class="region-head">
      <h2>板子管理</h2>
      <p class="region-sub">改完通过「下一帧遥测的响应」下发到板子并写入 NVS；空着 = 不改那一项。</p>
    </div>
    <div class="cols">
      <div class="card lead" id="cardcfg">
        <div class="card-head">
          <h3>板子设置</h3>
        </div>
        <div class="formrow">
          <div class="field">
            <label for="cfgssid">WiFi 名称</label>
            <input id="cfgssid" type="text" placeholder="不改就留空" autocomplete="off">
          </div>
          <div class="field">
            <label for="cfgpass">密码</label>
            <input id="cfgpass" type="password" placeholder="不改就留空" autocomplete="new-password">
          </div>
        </div>
        <div class="formrow">
          <div class="field" style="flex:1 1 240px">
            <label for="cfgurl">服务器地址</label>
            <input id="cfgurl" type="text" placeholder="http://192.168.x.x:8000" autocomplete="off">
          </div>
          <div class="field">
            <label for="cfgper">上报周期 ms</label>
            <input id="cfgper" type="number" min="50" max="2000" step="10" placeholder="50~2000" style="width:120px">
          </div>
        </div>
        <div class="actions">
          <button id="cfgapply">下发到板子</button>
          <span id="cfghint" class="hint"></span>
        </div>
        <div class="hint">
          <b>改 WiFi 会先试连、连上了才保存</b>（最多 8 秒）—— 密码填错不会把板子弄失联。
          <b>上报周期就是「灵敏度」</b>：50ms=20Hz（最跟手）/ 100ms=10Hz / 500ms=2Hz（默认）。
          越小越跟手，但 WiFi 压力越大；50ms 已接近单次往返的量级，跑不到也正常。
        </div>
      </div>

      <div class="card" id="cardsd">
        <div class="card-head">
          <h3>存储管理</h3>
          <span class="src" id="sdwho">—</span>
        </div>
        <div class="actions">
          <button id="sdrefresh">读取存储信息</button>
        </div>
        <div id="sdring" class="ringwrap"></div>
        <div class="list" id="sdfiles"></div>
      </div>
    </div>
  </section>
</main>

<footer>
  <div class="shell">
    服务器 <span id="host"></span> · <span id="logdir"></span> ·
    页面只用标准库 SSE 推送，不依赖外网；指令白名单 <span id="names"></span>
  </div>
</footer>

<dialog class="modal" id="cfm">
  <div class="modal-body">
    <h3 id="cfm-title">确认操作</h3>
    <p id="cfm-body"></p>
  </div>
  <div class="modal-foot">
    <button id="cfm-cancel" type="button">取消</button>
    <button id="cfm-ok" type="button" class="danger">确定</button>
  </div>
</dialog>

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

/* ---------------- 确认弹窗 ----------------
 * 用原生 <dialog>，不用 window.confirm()：系统弹窗在深色界面上是一块白方块，
 * 而且会**阻塞整个页面**（SSE 推送、3D 渲染全停）。
 * 这里是 Promise 风格，调用方写 .then(function(ok){...})。
 * 浏览器不支持 showModal() 时退回 window.confirm —— 功能不能因为 UI 新特性而丢。 */
var cfmDlg = $("cfm");
function confirmDialog(title, body, okLabel){
  if (!cfmDlg || typeof cfmDlg.showModal !== "function"){
    return Promise.resolve(window.confirm(title + "：" + body));
  }
  $("cfm-title").textContent = title;
  $("cfm-body").textContent = body;
  $("cfm-ok").textContent = okLabel || "确定";
  return new Promise(function(resolve){
    var settled = false;
    function finish(ok){
      if (settled) return;
      settled = true;
      cfmDlg.removeEventListener("close", onClose);
      $("cfm-ok").onclick = null;
      $("cfm-cancel").onclick = null;
      resolve(ok);
    }
    function onClose(){ finish(cfmDlg.returnValue === "ok"); }
    cfmDlg.addEventListener("close", onClose);
    /* 两个按钮**必须自己 close()**：它们不在 <form method="dialog"> 里，
       浏览器不会替我们把弹窗关掉（第一版就是这样，点"取消"弹窗纹丝不动）。
       用 close(value) 直接定 returnValue，Esc 关闭时它保持空 = 取消。 */
    $("cfm-ok").onclick = function(){ cfmDlg.close("ok"); };
    $("cfm-cancel").onclick = function(){ cfmDlg.close(""); };
    cfmDlg.returnValue = "";
    cfmDlg.showModal();
  });
}

/* ---------------- 颜色：一律从 CSS 令牌取，不在 JS 里抄第二份 ----------------
 * canvas 拿不到 CSS 变量，而页面有**两套主题**（浅色/深色）。
 * 早先这里是一张写死的 hex 表 —— 加主题那一刻它就成了第二个真相来源，
 * 迟早不同步（而且"看起来没坏"，只是某个主题下图表颜色不对）。
 * 现在用 1x1 canvas 把任意 CSS 颜色落成一个像素读回来：
 * oklch() / color() / rgba() 都能拿到确定的 sRGB 分量。
 * 结果按「主题 + 变量名」缓存，避免每帧都做一次 getImageData。 */
var _probeCtx = null, _varCache = {}, _varCacheTheme = "";
function _themeName(){
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}
function _resolve(css){
  if (!_probeCtx){
    var pc = document.createElement("canvas"); pc.width = pc.height = 1;
    _probeCtx = pc.getContext("2d", {willReadFrequently: true});
  }
  _probeCtx.fillStyle = "#000"; _probeCtx.fillRect(0, 0, 1, 1);
  _probeCtx.fillStyle = css;    _probeCtx.fillRect(0, 0, 1, 1);
  var d = _probeCtx.getImageData(0, 0, 1, 1).data;
  return [d[0], d[1], d[2]];
}
function varRGB(name){
  var th = _themeName();
  if (th !== _varCacheTheme){ _varCache = {}; _varCacheTheme = th; }   /* 换主题自动失效 */
  if (_varCache[name]) return _varCache[name];
  var raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  var rgb = raw ? _resolve(raw) : [128, 128, 128];
  _varCache[name] = rgb;
  return rgb;
}
function cssVar(name, alpha){
  var c = varRGB(name);
  return (alpha === undefined)
    ? "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")"
    : "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + alpha + ")";
}
/* "r,g,b" 三元组：CSS 里用 rgb(var(--act-rgb) / .10) 染色时要用 */
function varTriplet(name){ var c = varRGB(name); return c[0] + "," + c[1] + "," + c[2]; }

/* ---------------- 主题 ----------------
 * 默认明亮（写在 <html data-theme> 上），用户选过的存在 localStorage，
 * head 里那段内联脚本会在首次绘制前应用 —— 所以这里不需要再管"初始值"。
 * 切换后要**强制重画**：canvas 的像素是画上去的，CSS 换了它不会自己变。 */
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t === "dark" ? "dark" : "light");
  /* 清掉"上次是什么"的缓存，否则 setRing/setBall/setTilt 会认为值没变而跳过重绘 */
  lastRing = -1; lastAct = ""; lastBall = ""; lastBallColor = ""; lastTilt = "";
  fitCanvas();
  pull();
  var btn = $("theme");
  if (btn){
    var toDark = t !== "dark";
    btn.title = toDark ? "切换到深色主题" : "切换到明亮主题";
    btn.setAttribute("aria-label", btn.title);
  }
}

/* 服务器文案 → 活动词 + 颜色（与板端 classify() 同一套规则） */
/* 服务器文案 → 活动词 + 颜色。这里存的是**CSS 变量名**而不是色值 ——
   语义色的含义与板端 device/main/ui.c 的 UI_C_* 一致，具体色值由主题决定。 */
var ACT = [
  { k:["跌落","失重"], word:"跌落", v:"--color-fall"  },
  { k:["晃动"],        word:"晃动", v:"--color-shake" },
  { k:["步行"],        word:"步行", v:"--color-walk"  },
  { k:["运动"],        word:"运动", v:"--color-move"  },
  { k:["静置"],        word:"静置", v:"--color-still" }
];
function actColor(t){
  return { word: t.word, color: cssVar(t.v), rgb: varTriplet(t.v) };
}
function classify(s){
  for (var i=0;i<ACT.length;i++){
    for (var j=0;j<ACT[i].k.length;j++){
      if (s.indexOf(ACT[i].k[j]) >= 0) return actColor(ACT[i]);
    }
  }
  return actColor({ word: s ? s.slice(0,4) : "等待", v:"--color-idle" });
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
var lastActColor = cssVar("--color-idle");   /* 曲线读数胶囊描边用，随活动色更新 */

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

  var padL = 34*dpr, padR = 10*dpr, padT = 12*dpr, padB = 16*dpr;
  var plotW = W - padL - padR, plotH = H - padT - padB;
  var maxG = 2.4;
  /* 颜色全部走令牌（见 cssVar）：两套主题下图表要跟着变 */
  var cGrid = cssVar("--color-text-faint", .22), cFaint = cssVar("--color-text-faint"),
      cMove = cssVar("--color-move"), cDim = cssVar("--color-text-muted");

  function yOf(g){ return padT + plotH - Math.min(g, maxG)/maxG*plotH; }

  /* 网格 + 刻度 */
  ctx.strokeStyle = cGrid; ctx.lineWidth = 1*dpr;
  ctx.fillStyle = cFaint; ctx.font = (10*dpr)+"px PlexMono,Consolas,monospace";
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
  hline(0.35, cssVar("--color-fall", .55), [4,4]);
  hline(1.0,  cssVar("--color-text-faint", .40), [2,5]);

  var n = samples.length;
  if (n < 2){
    ctx.fillStyle = cFaint; ctx.textAlign = "left";
    ctx.fillText("等待开发板上报…", padL+6*dpr, padT+14*dpr);
    return;
  }

  /* 面积 + 折线 */
  var pts = samples.map(function(s, i){
    var g = Math.sqrt(s[1]*s[1] + s[2]*s[2] + s[3]*s[3]);
    return [padL + i/(n-1)*plotW, yOf(g), g];
  });

  var grad = ctx.createLinearGradient(0, padT, 0, padT+plotH);
  grad.addColorStop(0, cssVar("--color-move", .34));
  grad.addColorStop(1, cssVar("--color-move", .02));
  ctx.beginPath(); ctx.moveTo(pts[0][0], padT+plotH);
  pts.forEach(function(p){ ctx.lineTo(p[0], p[1]); });
  ctx.lineTo(pts[n-1][0], padT+plotH); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();

  ctx.beginPath();
  pts.forEach(function(p, i){ i ? ctx.lineTo(p[0],p[1]) : ctx.moveTo(p[0],p[1]); });
  ctx.strokeStyle = cMove; ctx.lineWidth = 2*dpr;
  ctx.lineJoin = "round"; ctx.stroke();

  /* 末端点 */
  var last = pts[n-1];
  ctx.beginPath(); ctx.arc(last[0], last[1], 4*dpr, 0, 6.2832);
  ctx.fillStyle = cMove; ctx.fill();
  ctx.beginPath(); ctx.arc(last[0], last[1], 8*dpr, 0, 6.2832);
  ctx.fillStyle = cssVar("--color-move", .20); ctx.fill();

  /* 末端读数胶囊：半透明深色底 + 当前活动色描边，贴在末端点上方，不出右界 */
  var label = last[2].toFixed(2) + " g";
  ctx.font = (11*dpr)+"px PlexMono,Consolas,monospace";
  var tw = ctx.measureText(label).width;
  var lx = Math.max(padL + 4*dpr, Math.min(last[0] - tw/2, W - padR - tw - 12*dpr));
  var ly = Math.max(padT, padT - 2*dpr);
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(lx - 6*dpr, ly, tw + 12*dpr, 16*dpr, 6*dpr);
  else ctx.rect(lx - 6*dpr, ly, tw + 12*dpr, 16*dpr);
  ctx.fillStyle = cssVar("--color-surface", .92);
  ctx.fill();
  ctx.lineWidth = 1*dpr;
  ctx.strokeStyle = lastActColor.replace(")", ",.45)").replace("rgb", "rgba");
  ctx.stroke();
  ctx.fillStyle = cDim; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(label, lx, ly + 8*dpr);
}
addEventListener("resize", fitCanvas);

/* ---------------- 姿态球与三轴条 ---------------- */
/* 1g 对应的像素偏移。这个值**必须跟着球的大小走**：球半径 105px（max-width 210），
   偏移 68px + 小球半径 10px = 78 < 105，小球永远压在球面之内；
   比例沿用旧版的 65% 左右，所以"倾斜多少、球跑多远"的手感没变。 */
var BUBBLE_MAX = 68;
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
  var cv3 = $("board3d");
  if (!cv3) return;
  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({canvas: cv3, antialias: true, alpha: true});
  } catch (e) {
    return;                    /* 没有 WebGL 就不显示这张卡，别留个空白框 */
  }
  var W = Math.max(200, cv3.clientWidth || 320), H = 280;
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
  /* 顶边标记：做成**跨在板子顶边上、两面都露出来的一条棱**，
   * 而不是贴在某一面上的薄片 —— 原来贴在 +z 面（z=0.09），从背面看被板子挡住。
   * 现在 z 方向做到 ±0.14（板厚 ±0.1），正反面都看得到；
   * +y 是"下"（屏幕系），所以顶边在 -y。
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
    var w = Math.max(200, cv3.clientWidth || 320);
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
  var color = (mag < 0.05) ? cssVar("--color-idle")
            : (mag < 0.35) ? cssVar("--color-fall")
            : (horiz < 0.15) ? cssVar("--color-still")
            : (horiz < 0.7) ? cssVar("--color-shake") : cssVar("--color-fall");
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
  if (mag < 0.05){ t = "无读数"; color = cssVar("--color-idle"); lastDeg = null; }
  else if (mag < 0.35){ t = "失重"; color = cssVar("--color-fall"); lastDeg = null; }
  else {
    var c = Math.min(1, Math.abs(z)/mag);
    var raw = Math.acos(c)*180/Math.PI;
    /* 只对**角度**做低通：acos 在接近平放时对噪声极敏感（0.03g → 14°），
       板端踩过同一个坑。姿态球不动它 —— 球要跟手。 */
    lastDeg = (lastDeg === null) ? raw : (lastDeg + 0.4*(raw - lastDeg));
    var deg = Math.round(lastDeg);
    t = "倾角 " + deg + "°";
    color = deg === 0 ? cssVar("--color-still") : (deg < 40 ? cssVar("--color-shake") : cssVar("--color-fall"));
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
  var ac = [cssVar("--color-fall"), cssVar("--color-still"), cssVar("--color-move")][i];
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
  /* 分两组渲染：安全指令在左，**破坏性指令单独顶到最右**，中间用可伸缩间隔隔开。
     原来它们按服务端给的顺序挨着排 —— 「格式化 SD 卡」紧邻「读取存储信息」，
     误点一次就是整张卡的数据，而这两个按钮长得一模一样。 */
  var safe = [], risky = [];
  cmdNames.forEach(function(n){
    var ui = CMD_UI[n] || {};
    if (ui.hidden) return;                     /* hidden：只走程序内部触发，不出按钮 */
    var html = '<button data-cmd="' + n + '"' + (ui.primary ? ' class="primary"' : '') + '>'
             + (ui.label || n) + '</button>';
    (ui.danger ? risky : safe).push(html);
  });
  host.innerHTML = (safe.length || risky.length)
    ? safe.join("") + (risky.length ? '<span class="cmd-gap"></span>' + risky.join("") : "")
    : '<span class="empty">服务端没有开放任何远程指令</span>';
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
/* 破坏性指令（目前只有 sd_format）先弹确认 —— 一键清掉整张卡，点错了没有撤销。
   confirmDialog 是异步的，所以真正的下发逻辑挪到 doSendCmd 里。 */
function sendCmd(name, btn, overrideParams){
  var ui0 = CMD_UI[name] || {};
  if (ui0.danger){
    confirmDialog("格式化 SD 卡",
                  "板子上的 SD 卡会被整张清空，卡里原有内容全部丢失，无法恢复。",
                  "格式化").then(function(ok){
      if (ok) doSendCmd(name, btn, overrideParams);
    });
    return;
  }
  doSendCmd(name, btn, overrideParams);
}
function doSendCmd(name, btn, overrideParams){
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
    im.hidden = false;          /* 收到第一帧才让它出现，之前别露出"图片裂了"的图标 */
    var ns = $("camnosig");
    if (ns) ns.style.display = "none";
    var st = $("camstat");
    if (st) st.textContent = "画面 " + im.naturalWidth + "×" + im.naturalHeight +
                             "（约 2 帧/秒）";
  };
  im.onerror = function(){
    camFails++;
    im.hidden = true;
    var ns = $("camnosig");
    if (ns) ns.style.display = "";
    /* 连续取不到就别一直重试了，提示一次即可 */
    if (camFails === 3) {
      var st = $("camstat");
      if (st) st.textContent = "还没有画面 —— 摄像头自检没过时不会有帧（先看板子串口）";
    }
  };
}
/* 当前选的帧率（1..10，和板端 CAM_STREAM_FPS_MAX 对齐）。 */
function camFps(){
  var el = $("camfps");
  var n = el ? parseInt(el.value, 10) : 2;
  return (isFinite(n) && n >= 1 && n <= 10) ? n : 2;
}
function camSetLive(on){
  camOn = on;
  if (camTimer) { clearInterval(camTimer); camTimer = null; }
  var btn = $("camlive");
  var fps = camFps();
  if (on) {
    camFrame();
    /* 取帧间隔跟着**板端的推帧间隔**走 —— 板子只推 2 帧/秒时页面每 200ms 取一次
     * 纯属白问（三次里两次拿到同一帧，白占连接）。下限 100ms 兜住离谱输入。 */
    camTimer = setInterval(camFrame, Math.max(100, Math.round(1000 / fps)));
  }
  if (btn) btn.textContent = on ? "关闭实时画面" : "开启实时画面";
  /* ⚠️ **必须把命令下发给板子**，光起本地定时器没用。
   *
   * 板子是 HTTP 客户端、只会"推"：它不推，服务端 FRAMES 里就没有帧，
   * 页面每 200ms 轮询到的永远是 404，表现成"实时画面点了没反应、只有拍照才有图"
   * —— 2026-09-28 用户反馈的正是这个。原来的实现只切了本地状态与定时器，
   * 注释里写着"由这个按钮触发 cam_stream"，但**那一句 sendCmd 从来没写**。
   * （当时从服务端看，点过按钮却收不到任何 cam_stream 命令，就是这个原因。）
   *
   * fps 一起下发：省流/丝滑是**板子**的事（真正决定占多少 WiFi 带宽的是它），
   * 页面只负责按同样的节奏取。 */
  sendCmd("cam_stream", null, {on: on, fps: fps});
}
function shotRow(dev, name, bytes, local){
  return '<div class="shot"><img src="' + (local ? local : "/api/shots/" +
      encodeURIComponent(dev) + "/" + encodeURIComponent(name)) + '" alt="">' +
    '<span class="nm">' + esc(name) + '<br><span class="hint">' +
    (bytes ? fmtBytes(bytes) : "") + (local ? " · 本机存档" : " · 板子/服务器") + '</span></span>' +
    '<button class="sm" data-shot="' + esc(name) + '" data-local="' + (local ? "1" : "") + '">删除</button></div>';
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
          confirmDialog("删除照片", "将永久删除 " + nm + "，无法恢复。", "删除").then(function(ok){
            if (!ok) return;
            if (btn.getAttribute("data-local")) {
              idbDel(nm, renderShots);
            } else {
              fetch("/api/shots/delete", {method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({device: dev, name: nm})}).then(renderShots);
            }
          });
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
/* 只在内容真的变了才重写 DOM。
   renderCmds / renderSd 都跟着 10Hz 轮询跑，而内容绝大多数时候没变 ——
   无脑 `innerHTML =` 等于每秒把同一段 HTML 重写十遍：白干活，还会打断
   键盘焦点、文本选中、以及正在跑的过渡动画（点存储圆环时那个高亮闪烁就中招）。
   用元素上的一个私有属性记"上次写进去的 HTML"，比再养一堆模块级变量干净。 */
function setHTML(el, html){
  if (!el || el.__html === html) return;
  el.__html = html;
  el.innerHTML = html;
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
    setHTML(host, '<div class="hint">还没读过 —— 点上面的「读取存储信息」' +
      '（板子离线时命令会排队，等它回来再执行）</div>');
    setHTML(list, "");
    return;
  }
  var d = parseSdNote(last.result.note);
  if (d.total === null){
    setHTML(host, '<div class="hint">板端没返回容量信息</div>');
  } else {
    var used = Math.max(0, d.total - d.free);
    var pct = d.total ? (used / d.total * 100) : 0;
    var bar = pct > 90 ? cssVar("--color-fall") : (pct > 75 ? cssVar("--color-shake") : cssVar("--color-still"));
    /* 圆环：中间写百分比，比一条横条直观；**点它 = 进入文件管理** */
    var R = 44, CIRC = 2 * Math.PI * R;
    var off = (CIRC * (1 - Math.min(100, pct) / 100)).toFixed(1);
    setHTML(host,
      '<div class="ring" id="sdringbtn" role="button" tabindex="0" title="点击进入文件管理">' +
        '<svg width="104" height="104" viewBox="0 0 104 104">' +
          '<circle cx="52" cy="52" r="' + R + '" fill="none" stroke="' + cssVar("--color-surface-3") + '" stroke-width="10"/>' +
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
        '<div class="hint" style="margin-top:4px">点圆环跳到下面的文件列表 ↓</div>' +
      '</div>');
    var rb = $("sdringbtn");
    if (rb) {
      var jump = function(){
        var fl = $("sdfiles");
        if (fl) {
          fl.scrollIntoView({behavior: "smooth", block: "center"});
          fl.style.transition = "box-shadow .3s";
          fl.style.boxShadow = "0 0 0 2px " + bar;
          setTimeout(function(){ fl.style.boxShadow = "none"; }, 1200);
        }
      };
      rb.onclick = jump;
      /* 圆环也是"可点的 div"，同样要补键盘可达性 */
      rb.tabIndex = 0;
      rb.setAttribute("role", "button");
      rb.onkeydown = function(ev){
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar"){
          ev.preventDefault();
          jump();
        }
      };
    }
  }
  if (!d.files.length){
    setHTML(list, '<div class="empty">卡上没有文件。</div>');
    return;
  }
  setHTML(list, d.files.map(function(f){
    return '<div class="item"><span class="name">' + esc(f.name) + '</span>' +
      '<span class="meta">' + fmtBytes(f.size) + '</span>' +
      '<button class="sm" data-rm="' + esc(f.name) + '" style="margin-left:auto">删除</button></div>';
  }).join("") + (d.more ? '<div class="hint">（文件较多，只列了前 12 个）</div>' : ""));
  Array.prototype.forEach.call(list.querySelectorAll("[data-rm]"), function(b){
    b.onclick = function(){
      var fn = b.getAttribute("data-rm");
      confirmDialog("删除板子上的文件", "将永久删除 SD 卡里的 " + fn + "，无法恢复。", "删除")
        .then(function(ok){ if (ok) sendCmd("sd_rm", null, {name: fn}); });
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
  if (!cmds || !cmds.length){
    setHTML(el, '<div class="empty">还没有下发过指令。</div>');
    return;
  }
  setHTML(el, cmds.slice(0,8).map(function(c){
    var r = c.result || {}, extra = "";
    if (c.state === "done" && r.x !== undefined){
      extra = '<span class="meta">x=' + (+r.x).toFixed(3) + ' y=' + (+r.y).toFixed(3)
            + ' z=' + (+r.z).toFixed(3) + ' · ' + (r.n||0) + ' 样本 · '
            + Math.round(r.ms||0) + ' ms · σ=' + (+(r.std||0)).toFixed(4) + ' g</span>';
    } else if (c.state === "failed"){
      extra = '<span class="meta">' + esc(r.err || "") + '</span>';
    }
    var lat = (c.sent && c.done)
      ? '<span class="meta">往返 ' + Math.round((c.done-c.sent)*1000) + ' ms</span>' : "";
    var ps = (c.params && Object.keys(c.params).length)
      ? '<span class="meta">' + esc(JSON.stringify(c.params)) + '</span>' : "";
    return '<div class="item"><span class="st st-' + esc(c.state) + '">' + esc(STNAME[c.state]||c.state) + '</span>'
         + '<span class="name">' + esc(c.name) + '</span>' + ps
         + '<span class="meta">' + esc(c.id) + '</span>' + lat + extra + '</div>';
  }).join(""));

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
         + '<span>' + esc(ev.text) + '</span></div>';
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
  /* 这一帧没带设备列表就**什么都别做**，尤其别动可见性。
     曾经因为 `/api/latest` 不带 devices，这里被当成"0 台"处理 →
     多板场景下这块区域以取数频率（100ms）反复藏/显，而且大部分时间不可见。
     现在服务端两个通道都带 devices 了，这条只是**防御性兜底**：
     以后再加新通道、忘了带 devices 时，页面不会再闪。 */
  if (!Array.isArray(list)) return;
  /* 只有一台板时不显示这块 —— 单板课堂不该凭空多出一张卡片 */
  if (list.length <= 1){ card.hidden = true; devHTML = ""; return; }
  card.hidden = false;

  var online = 0;
  var h = list.map(function(d){
    if (d.online) online++;
    var color = d.online ? cssVar("--color-still") : cssVar("--color-fall");
    var title = d.activity || "";
    /* 记住每台的档位，供"方向档位"控件显示 */
    if (d.orient !== null && d.orient !== undefined) devOrient[d.id] = d.orient;
    var oTxt = (d.orient === null || d.orient === undefined) ? "" : (" · o" + d.orient);
    var checked = selDevices[d.id] ? " checked" : "";
    return '<div class="dev' + (d.id === current ? " sel" : "") + '" data-dev="' + esc(d.id) + '">'
      + '<input type="checkbox" class="devchk" data-chk="' + esc(d.id) + '"' + checked + '>'
      + '<span class="dot' + (d.online ? "" : " off") + '" style="background:' + color + '"></span>'
      + '<span class="did">' + esc(d.id) + '</span>'
      + '<span class="meta">' + (d.online ? "在线" : "离线") + ' · ' + ago(d.age)
      + (d.source && d.source !== "-" ? " · " + esc(d.source) : "") + esc(oTxt) + '</span>'
      + '<span class="meta">' + esc(title) + '</span>'
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
    /* 键盘可达：这一行是 div，不给 tabindex 的话键盘用户根本选不了设备。
       Enter / 空格 都要能用（div 不像 <button> 会自己处理空格）。 */
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.onkeydown = function(ev){
      if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar"){
        ev.preventDefault();
        selectDevice(el.getAttribute("data-dev"));
      }
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
  /* 只在"在线状态真的翻转"时才重写这个胶囊。
     无脑 10Hz 重写会**每秒把里面的 <i> 重建十遍** ——
     `.pill.off i` 的呼吸动画（breathe 1.7s）于是每次都被从头开始，
     看起来完全不像在呼吸，像卡住了。 */
  if (dev.__on !== devOnline){
    dev.__on = devOnline;
    dev.className = "pill " + (devOnline ? "on" : "off");
    dev.innerHTML = "<i></i>" + (devOnline ? "在线" : "离线");
  }
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
    /* hero 舞台的底色与活动色联动（CSS 变量），活动词淡入一次 */
    var stage = document.querySelector(".stage");
    if (stage) {
      stage.style.setProperty("--act-rgb", a.rgb);
      stage.style.setProperty("--act-color", a.color);
    }
    lastActColor = "rgb(" + a.rgb + ")";
    e.classList.remove("pop");
    void e.offsetWidth;
    e.classList.add("pop");
  }

  /* 有真回复才高亮并加 has；空回复回到引导语（dim），与板端一致 */
  if (s.ai_reply && s.ai_reply !== aiReply){
    aiReply = s.ai_reply;
    $("reply").textContent = aiReply;
  } else if (!s.ai_reply && aiReply !== ""){
    aiReply = "";
    $("reply").textContent = "还没有提问。";
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
/* 主题：初始值已由 head 里那段内联脚本在首次绘制前定好（默认明亮）。
   这里只负责把按钮文案对齐、以及接上点击切换。 */
applyTheme(_themeName());
if ($("theme")) $("theme").onclick = function(){
  var next = _themeName() === "dark" ? "light" : "dark";
  try { localStorage.setItem("rw1-theme", next); } catch (e) { /* 存不了也照样切换 */ }
  applyTheme(next);
};
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
    /* 正在推流时改帧率 → 立刻重发一次命令，不用先关再开。
     * 没在推流就什么都不做：等用户点「开启实时画面」时会带上新帧率。 */
    if ($("camfps")) $("camfps").onchange = function(){ if (camOn) camSetLive(true); };
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

    if port_already_serving(args.host, args.port):
        print("!! 端口 %d 上已经有一个服务在应答了，不重复启动。" % args.port)
        print("   多半是上次没关干净的进程。先关掉它，或换个端口：--port 8001")
        print("   （Windows 允许两个进程绑同一端口，硬起会静默抢请求，非常难查。）")
        return 2

    srv = DashboardServer((args.host, args.port), Handler)

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

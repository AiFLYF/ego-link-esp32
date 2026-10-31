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
import hashlib
import hmac
import io
import json
import math
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

# --------------------------------------------------------------------------
# 同目录的两个模块（2026-11-01 从本文件拆出：analysis / jsonl_log）
#
# ⚠️ **必须先把本文件所在目录放进 sys.path。** 本文件有两条加载路径：
#   1. `python server/server.py` —— 此时脚本目录自动在 sys.path[0]，没问题；
#   2. `tools/test_units.py` / `verify_server.py` 用 importlib **按路径**加载
#      —— 此时 sys.path 里没有 server/，只写 `import analysis` 会 ImportError。
# 放在这里而不是文件更靠前的位置，是因为下面的 `MAX_WINDOW` 马上要用
# `analysis.WINDOW_S`（窗口秒数 → deque 容量），必须在它之前完成导入。
#
# 为什么拆：依赖图量出来的结果是这两个模块**不碰任何模块级可变状态**
# （JsonlLogger 的传递依赖是 0 个），把它们留在 2627 行的文件里只有坏处。
# --------------------------------------------------------------------------
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from analysis import ACTIVITY_IDLE, WINDOW_S, analyze, tilt_direction  # noqa: E402
from jsonl_log import JsonlLogger  # noqa: E402

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------
# 统计/计步/曲线用的时间窗 `WINDOW_S` 与瞬时短窗 `MOTION_WINDOW_S`
# 已经搬到 `server/analysis.py`（它们只被那边的分类函数用）。
# 本文件在顶部 import 了 `WINDOW_S`，所以下面这行照样成立。
# 滑动窗口样本数。**上限必须按"服务端愿意信任的最快采样率"算，不是按标称值算。**
# 窗口本身是按时间过滤的（`now - ts <= WINDOW_S`），deque 只是它的容量上限：
#   - 标称 100 Hz × 8 s = 800；
#   - 但 dt 是**推断**出来的，DT_TRUST_MIN=8 ms 也在可信区间内（125 Hz），
#     那种情况下 8 秒里有 1000 个样本 —— 容量给 800 就会把窗口悄悄截成 6.4 秒，
#     而 `ai_summary()` 的文案仍写"最近 8 秒内约计步 N 次"，计步会系统性偏低。
# 所以取 1000 + 64 的余量。代价是每批多遍历两百来个样本（实测 analyze
# 8.15 ms@1200 → 约 7.1 ms@1064，仍比原来省 13%）—— 正确性优先于这点收益。
# 真正的根治是"按时间索引 + 追加时缓存 |a|"，留待下一批。
MAX_WINDOW = int(WINDOW_S / 0.008) + 64      # = 1064

DEVICE_TIMEOUT = 5.0      # 超过该秒数没有新遥测则视为离线
LLM_TIMEOUT = 25.0        # 大模型请求超时（后台线程里等，不影响板端）
# 同时打大模型的请求上限。一个班 20 块板可能同时提问，没有上限时 20 个线程
# 一起砸过去，对方一限流就**全部**超时 —— 课堂里表现为"全班的 AI 同时傻了"，
# 且每个人都要干等 LLM_TIMEOUT 秒。语义：最多 N 个在飞，其余在信号量上排队；
# 排队超过 LLM_QUEUE_TIMEOUT 就明说"人太多，稍后再试"并回退本地 AI，
# 不装死、也不无限占着线程。
LLM_MAX_CONCURRENT = int(os.environ.get("RW1_LLM_MAX_CONCURRENT", "4"))
LLM_QUEUE_TIMEOUT = float(os.environ.get("RW1_LLM_QUEUE_TIMEOUT", "30"))
_LLM_SEM = threading.BoundedSemaphore(max(1, LLM_MAX_CONCURRENT))

# ---- 访问密钥（可选，默认关）------------------------------------------------
# 威胁模型：教室里任何连上同一 WiFi 的人。破坏性动作（格式化 SD、删照片）与
# 花钱的动作（问大模型）**只能从网页来** —— 板子只会上报遥测/帧/音频，
# 命令永远是服务端**下发**给板子的。所以门这几个网页写入口**不需要动固件**，
# 老固件照常上报（后向兼容的关键证据：无密钥环境下整套回归全绿）。
# 不配 RW1_API_TOKEN = 完全关闭，行为与以前逐字节相同。
# 配了则下面这些接口要求 `X-RW1-Token` 请求头（或 `?token=` 查询参数）。
API_TOKEN = os.environ.get("RW1_API_TOKEN", "").strip()
WEB_WRITE_PATHS = ("/api/command", "/api/ask", "/api/ask/cancel", "/api/shots/delete")
# 写入口的同源校验（CSRF）总开关。默认开启；只有在反向代理会改写 Host
# 导致误判时才需要设 RW1_ALLOW_CROSS_ORIGIN=1 关掉。详见 Handler._origin_ok。
ALLOW_CROSS_ORIGIN = os.environ.get("RW1_ALLOW_CROSS_ORIGIN", "").strip() == "1"

# ---- 语音（第 4 周：按键说话 → 识别 → 大模型 → 屏显回复）---------------------
# 识别两条路**自动选**：配了云端 key 走云端，没配就用本地 whisper；
# 两条都没有时明确回一句可读的原因，而不是静默失败 ——
# 和「不配大模型也能完整跑通」是同一个路子。
AUDIO_MAX_BYTES = 512 * 1024   # 单次上传上限（3 s @16 kHz/16 bit 单声道 ≈ 96 KB，留足余量）
AUDIO_RATE = 16000             # 板端录音采样率；云端与本地都按这个解
AUDIO_DIR = None               # 录音留档目录（main() 里按 --data-dir 设）
AUDIO_KEEP = 50                # 每台设备最多留多少条录音
# 同一台板两次录音之间的最小间隔。`/api/audio` 是**唯一一个"一被调用就花钱"**
# 的未鉴权入口（云端 ASR + 一次大模型），而它按设计不能加 token（老固件不带头）。
# 所以在这里做一道限流：连续刷只会把间隔拉长，不会每次都能触发一次付费调用。
# 顺带也挡掉"按键抖动导致连发两段录音"这种误触。
AUDIO_MIN_GAP_S = 1.5
AUDIO_LAST = {}                # device -> 上次受理的时刻（内部限流用，不进快照）
ASR_TIMEOUT = 30.0             # 云端识别超时（后台线程里等，不阻塞板端）
VOICE_TEXT_MAX = 200           # 识别出的文字长度上限（与 ask 的 q 一致）
CHAT_HISTORY = 6               # 多轮上下文保留的问答轮数（澄清类追问要用）

MAX_BATCH = 400           # 单次 POST 最多接受的样本数（防止畸形/恶意负载）
VALID_G_MAX = 16.0        # 单轴可信上限（g）。固件 sanitize3 是 ±8g，这里留一倍余量；
                          # 超出的一律当坏帧丢弃，见 Handler._parse_points
BOARD_REPLY_MAX = 512     # 回传板端的回复字节上限（UTF-8 安全截断）

# 仪表盘自托管字体白名单（详见 Handler._send_vendor_font）。字体名来自 URL，
# 所以不放正则、只放**逐字写死**的文件名 —— 这是最难被绕过的白名单。
FONT_FILES = ("sora-400.woff2", "sora-600.woff2", "sora-700.woff2", "sora-800.woff2",
              "plexmono-400.woff2", "plexmono-500.woff2", "plexmono-600.woff2")

DT_NOMINAL = 0.01         # 标称采样间隔（对应板端 CONFIG_RW1_SAMPLE_PERIOD_MS=10）
DT_TRUST_MAX = 0.05       # 超过这个推断间隔就认为该帧晚到了、时间轴不可信
# 下界：dt_est = 两批到达间隔 / 本批样本数，**批越小这个推算越不可信**。
# 板端上报解耦后（2026-09-28），网络一慢就丢帧、下一批样本数很少，
# (间隔 / 样本数) 能掉到几毫秒 —— sample_hz 直接飘到 311（板子标称才 100）。
# 它是网页上直接显示的字段，飘高会误导人，所以两头都要钳。
DT_TRUST_MIN = 0.008      # 8 ms → 125 Hz，给标称的 100 Hz 留 25% 余量
# 计步 / 晃动 / 跌落的判定阈值（STEP_* / MOTION_STD / SHAKE_* / WALK_ZCR_MIN /
# FREEFALL_*）全部搬到 `server/analysis.py` 了 —— 它们是那边的判据参数，
# 改阈值去那个文件，别在这里加。

# ---- 远程命令（第 2 周：Web 下发「采集一次」，按 request_id 反馈结果）----
# 第 3 周在此基础上加了两个「物理反馈」指令：led_blink / led_set —— 板载 LED 在 GPIO3。
CMD_TIMEOUT_S = 10.0      # 命令下发后多久没收到结果就判超时
CMD_MAX_HISTORY = 20      # 保留最近多少条命令供网页显示
# SSE 每帧真正发出去的条数。**必须与前端渲染上限一致**（renderEvents 的
# `slice(0,40)`、renderCmds 的前 8 条）—— 多发的字节到浏览器就被 DOM 丢掉了。
# 保留的历史仍然在 dev.events / dev.commands 里（各 200/20 条），
# /api/events 与 /api/commands 要全量时能拿到。
SSE_EVENTS_MAX = 40
SSE_COMMANDS_MAX = 8
# /api/latest 每帧带多少个曲线点。画布 CSS 宽约 240 px，120 点已经超过可分辨
# 像素数；240 点时多出来的一半在 drawChart 里落到同一列像素上，是纯字节。
CURVE_POINTS = 120
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
# ⚠️ **画质功能已撤掉**（2026-09-29）。曾经这里放过 JPEG 质量的合法范围，
# 板端 `camera_set_quality()` 用 `V4L2_CID_JPEG_COMPRESSION_QUALITY` 去设 ——
# 真机实测**会让摄像头一帧都不出**（自检 `取帧 ESP_ERR_NOT_FOUND`），
# 整块去掉就立刻恢复（`自检 PASS，第 1 帧 27193 B`）。挪到 REQBUFS 之后也一样坏，
# 所以是"写这个控制项"本身把 esp_video 管线搞坏的，跟位置无关。
# 详见 device/main/camera.c 里 CAM_JPEG_QUALITY_* 的注释与手册 T56。
FALL_AUTO_ALERT = True    # 判定跌落时自动下发 LED 告警（远端物理反馈）

# `ACTIVITY_IDLE`（"等待数据…"）在 server/analysis.py 定义，本文件顶部已 import ——
# 它是**分类器的输出词汇**，归那边管；`Device` 只是拿它当初始值。

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
                 "queue", "seq", "chat", "st")

    def __init__(self, did):
        self.id = did
        self.samples = deque(maxlen=MAX_WINDOW)   # [(ts, x, y, z)]，x/y 为屏幕坐标
        self.shake_times = deque(maxlen=256)      # 晃动事件时间戳（窗口内计数用）
        self.events = deque(maxlen=200)           # 事件流（网页显示）
        self.commands = {}                        # request_id -> 命令记录
        self.queue = deque()                      # 待下发的 request_id（FIFO）
        self.seq = 0                              # 生成可读 request_id 的递增序号
        # 对话历史（第 4 周「澄清」）：存 (role, content)，user/assistant 交替。
        # 按设备隔离 —— A 板的追问绝不能串到 B 板，课堂里 20 块板共用一台服务器。
        self.chat = deque(maxlen=CHAT_HISTORY * 2)
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
            # 「停止」用：每次取消都 +1，后台线程回来时发现令牌变了就丢弃结果。
            # 比"设个 flag 再清掉"可靠 —— 不依赖 worker 有没有及时看到 flag。
            #
            # 第 6 周起它同时是**回合令牌**（第 6 周「迟到结果隔离」）：每开始一轮
            # 新的 AI 交互就 +1，任何异步产物（大模型回答、自然语言指令的执行结果、
            # 语音识别）回来时都要对一次令牌 —— 对不上说明用户早就改口了，直接丢弃。
            # 否则「先说拍张照 → 再问一个问题」时，那条指令的旧结果会把新答案盖掉。
            "ai_cancel": 0,
            # 计时（第 6 周）。板端屏幕只显示 ai_reply，看不到"等了多久"，
            # 所以耗时在网页上给：识别多久、生成多久、整个语音任务多久。
            # `voice_t0` 是**本轮语音任务**的起点（收到录音那一刻），回复落地时清 0；
            # 为 0 表示当前这轮不是语音发起的（比如网页打字）。
            "voice_t0": 0.0,
            "ai_t0": 0.0,
            "voice_ms": 0,                        # 识别耗时
            "ai_ms": 0,                           # 生成耗时（提问 → 回答就绪）
            "total_ms": 0,                        # 端到端（录音 → 最终回复就绪）
            # 语音链路（第 4 周）。idle / recording / uploading / transcribing，
            # 网页和板端屏幕都照这个显示进度，不需要各猜各的。
            "voice_state": "idle",
            "voice_text": "",                     # 最近一次识别出的文字
            "voice_err": "",                      # 最近一次失败原因（给人看的）
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
# `FRAMES` 的**基数上限**。`MAX_DEVICES` 只管 `DEVICES`（遥测注册的那张表），
# 管不到这里：任何人不带密钥、拿 400 KB 的帧乘以无限的设备名就能把内存灌满
# （旧代码：`FRAMES[dev] = {...}` 永不淘汰）。按 ts 淘汰最旧的那台，
# 64 台 × 400 KB ≈ 25 MB 封顶，课堂上够用。
FRAME_MAX_DEVICES = 64
SHOTS_DIR = None           # 拍照留档目录（main() 里按 --data-dir 设）
SHOT_MAX = 200             # 每台设备最多留多少张（免得把磁盘塞满）
# 照片目录的**设备数上限**：每台设备 200 张，但设备名是外部输入，
# 64 个目录 × 200 张 ≈ 磁盘可控。超了就不存（返回 None，板端只当没留档）。
SHOT_MAX_DEVICES = 64


def safe_name(s):
    """把设备名变成安全的目录/文件名：只留字母数字和 - _ .，其余换成 _。

    ⚠️ 无脑截断到 48 字符会**撞名**：前 48 字符相同的两台板（课堂上最常见的
    「第三组-07-xxxx…」式命名）会拿到同一个文件名，照片/记录互相覆盖且看不出来。
    2026-10-12 修：截断发生时追加一个 6 位短哈希（源字符串 md5 前 6 位）——
    总长仍是 48，但不同全名几乎不可能撞到同一个短哈希。只有"被截断"才付这个
    代价，普通短名字的输出与以前逐字节一致。
    """
    keep = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(s))
    if len(keep) > 48:
        digest = hashlib.md5(str(s).encode("utf-8")).hexdigest()[:6]
        keep = keep[:41] + "_" + digest          # 41 + 1 + 6 = 48，总长不变
    return keep or "unknown"


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
    # 兜底设备可能**已经被 LRU 淘汰掉**：`"-"` 的 last_post 恒为 0，是
    # `get_device()` 淘汰时最优先的受害者。而 `pick_device()` 又被
    # `/api/audio`（它不写 last_post）之类的路径调用，于是存在这个序列：
    # 33 个不同设备名的 audio 上报 → `"-"` 被淘汰 → 这里 KeyError → 500。
    # 用 setdefault 重造一台即可（它就是"老固件的默认桶"，重建无副作用）。
    return DEVICES.setdefault(DEFAULT_DEVICE, Device(DEFAULT_DEVICE))


def snapshot(dev, with_events=True, with_commands=True):
    """一台设备的完整快照。**调用方必须已持有 LOCK。**

    `with_events` / `with_commands` 让调用方只取**这一通道真正负责**的部分：
    `/api/latest`（网页 10Hz 轮询）只要活状态与曲线样本，事件流和命令历史
    是 SSE 的独通道（每 500ms 一帧）。10Hz 全量轮询在锁内序列化 ~25KB 的
    events+commands，跟 20 块板的上报抢同一把 LOCK —— 课堂场景下这是实打实
    的 contention。⚠️ 前端因此**不许**从 /api/latest 推导事件/命令状态
    （renderEvents/renderCmds 对缺失字段直接跳过，不清空、不画空态）。
    """
    snap = dict(dev.st)
    if with_events:
        snap["events"] = list(dev.events)
    if with_commands:
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
    """把外部传来的数字夹进 [lo, hi]；不是数字就用默认值。

    ⚠️ `NaN` / `inf` 必须在这里挡掉：`int(float("inf"))` 抛 OverflowError，
    `int(float("nan"))` 抛 ValueError —— 两者都不在原来的 `except` 里，
    于是网页传个 `"inf"`（或者 JSON 里直接写 Infinity）就是一个 500 + 一段
    traceback。而 `float()` 本身是**接受**这两个字面量的，所以必须显式判有限性。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return dflt
    if not math.isfinite(v):
        return dflt
    return max(lo, min(hi, int(v)))


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
        # JPEG 画质。**这是唯一能改帧大小的旋钮** —— OV3660 的 JPEG 分辨率
        # 固定 1280x720（驱动格式表里 JPEG 仅一档），所以"调分辨率"做不到，
        # 只能调画质：调低 → 帧变小 → 上传更快、更省带宽。
        # 画质（quality）**已撤掉**：板端写 V4L2_CID_JPEG_COMPRESSION_QUALITY
        # 会让摄像头一帧都不出（见文件顶部 CAM_JPEG_QUALITY 那段注释）。
        # 这里不再放行，网页上也没有那个控件了。
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
            # 下限 100ms = 10Hz（与板端 transport.c 的 run_set_config 夹同一范围）。
            # **曾经放到 50ms（20Hz），是个陷阱**：每批只剩 5 个样本，"批量上报"
            # 的设计红利消失；而 32 块板 × 20 POST/s = 640 连接/秒，配上
            # `Connection: close`（服务端先关）会在服务端侧堆出数万个 TIME_WAIT。
            # 三处（网页选择器 / 这里 / 固件）必须一起改，否则会出现"网页选了
            # 100 但板端还是 50"这种对不上的静默偏差。
            out["period_ms"] = clamp_int(p.get("period_ms"), 100, 2000, 500)
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


def _finish_turn(dev, now=None):
    """一轮**语音任务**收尾：算出端到端耗时并清掉起点。**调用方必须已持有 LOCK。**

    只在 `voice_t0 > 0`（本轮是语音发起的）时才算 —— 网页打字没有"录音时刻"，
    硬算会把上一个语音任务的起点拿来用，数字是错的。
    """
    now = time.time() if now is None else now
    t0 = dev.st.get("voice_t0") or 0.0
    if t0 > 0:
        dev.st["total_ms"] = int(max(0.0, now - t0) * 1000)
        dev.st["voice_t0"] = 0.0
    return dev.st.get("total_ms", 0)


def new_command(dev, name, params=None, source="web", turn=None):
    """给某台设备建一条待下发的命令，返回 request_id。

    `source` 记录这条命令是谁下的：`web`（网页按钮）或 `nl`（自然语言请求）。
    **自然语言那条执行完要把结果回显到板端屏幕**（见 apply_command_result）——
    用户是对着板子说的，眼睛也在板子上，结果只出现在网页上等于没说。

    `turn` 是下发这条命令时的**回合令牌**（第 6 周「迟到结果隔离」）。结果回显时
    要对一次：对不上就说明用户在这条命令跑完之前已经开始了新一轮交互（又问了句话、
    或说了别的指令），这条**迟到的结果不该再往屏幕上写** —— 那会把新的回答盖掉。
    """
    with LOCK:
        dev.seq += 1
        cid = "c-%d-%d" % (int(time.time()), dev.seq)
        dev.commands[cid] = {
            "id": cid,
            "name": name,
            "params": sanitize_params(name, params),
            "source": source,
            "nl_turn": turn,        # 只有 source=="nl" 才有意义；回显前要核对
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
        # 自然语言请求：把结果回显到板端屏幕 —— 用户是对着板子说的，眼睛也在板子上，
        # 结果只出现在网页上等于没说。
        #
        # 第 6 周「迟到结果隔离」：**只有令牌还对上才写**。用户在这条指令跑完之前
        # 已经又问了句话 / 说了别的指令的话，这条迟到的结果属于上一轮，
        # 写上去只会把新的回答盖掉（屏幕上会莫名其妙跳回一个旧结果）。
        if rec.get("source") == "nl":
            if rec.get("nl_turn") == dev.st["ai_cancel"]:
                dev.st["ai_reply"] = _nl_result_reply(name, rec["state"] == "done", r)
                dev.st["ai_mode"] = "指令"
                _finish_turn(dev)            # 语音任务的话，这就是它的终点
            else:
                push_event(dev, "warn", "指令 %s 的结果来晚了，已丢弃（用户已开始新一轮）"
                           % name)
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
                    if rec.get("source") == "nl":
                        # 自然语言触发的命令超时：板端屏幕别一直停在"好，让板子…"
                        # 同样要核对令牌 —— 用户早就改口的话，这条超时提示也是过期的。
                        if rec.get("nl_turn") == dev.st["ai_cancel"]:
                            dev.st["ai_reply"] = "板子没回话（%s 超时）" % rec["name"]
                            dev.st["ai_mode"] = "指令"
                            _finish_turn(dev)
                    expired.append((dev, rec["name"]))
    return expired


def commands_snapshot(dev):
    """按时间倒序返回该设备最近若干条命令的副本（**调用方必须已持有 LOCK**）。

    ⚠️ `dict(r)` 只是**浅拷贝**：`history` 列表和 `result` 字典仍与活记录共享。
    `_mark()` 会往 `history` 里追加（命令状态每秒都在迁移），而序列化发生在
    **锁外**（`/api/latest` 与 SSE 都是先出快照再 `json.dumps`）—— 于是这一帧
    可能混上"拍完之后"才发生的迁移，UI 上表现为时间线抖一下，或同一次轮询里
    同一条命令一会儿 queued 一会儿 sent。所以这里把可变字段**各自再拷一份**，
    快照从这一刻起与活记录没有任何共享对象（2026-10-12 修）。
    """
    out = []
    for r in sorted(dev.commands.values(), key=lambda r: r["created"], reverse=True):
        c = dict(r)
        c["history"] = [list(h) for h in r["history"]]
        if isinstance(r.get("result"), dict):
            c["result"] = dict(r["result"])
        out.append(c)
    return out


# --------------------------------------------------------------------------
# 落盘：JSONL 追加写 —— 已搬到 `server/jsonl_log.py`（本文件顶部 import 了它）
#
# 那个类和 HTTP / IMU / 指令通道一点关系都没有，依赖图算下来它引用的模块级
# 名字是 **0 个**。留在 2600 行的文件里唯一的效果是让文件更长、让它没法被
# 单独测试。设计要点（丢最旧而非最新、close() 排空队列、purge_old 必须是
# 公开名）都写在那个文件的模块 docstring 里。
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 运动分析：本地规则"AI" —— 已搬到 `server/analysis.py`
#
# 那 5 个函数（tilt_direction / count_steps / max_freefall_run /
# zero_cross_rate / analyze）加上它们的 11 个阈值常量整体搬走了：
# 依赖图显示它们只互相引用，模块级可变状态一个都不碰。
# **改计步/晃动/跌落的阈值去那个文件**，别在这里加常量。
#
# 本文件顶部 import 了 analyze 与 tilt_direction（后者被下面的 ai_summary 用）。
# 坐标契约也一并搬过去了：板端上报的 x/y 已是屏幕坐标系（+x 右、+y 下）。
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# 语音识别（第 4 周）—— 云端优先，本地 whisper 兜底
# --------------------------------------------------------------------------
def _pcm_to_wav(pcm, rate=AUDIO_RATE, channels=1, bits=16):
    """给裸 PCM 套一个 WAV 头。

    板子没有文件系统，也不想在 MCU 上拼 RIFF，所以直接推裸 PCM；
    而两端的识别接口都只吃容器格式，缺的那一层在这里补。
    """
    byte_rate = rate * channels * bits // 8
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                 byte_rate, channels * bits // 8, bits)
    hdr += b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm


def _asr_cloud_cfg():
    """云端识别配置；没配 key 返回 None。"""
    key = os.environ.get("RW1_ASR_API_KEY")
    if not key:
        return None
    return {
        "key": key,
        "base": os.environ.get("RW1_ASR_BASE_URL",
                               "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("RW1_ASR_MODEL", "whisper-1"),
    }


_WHISPER = None            # 本地模型句柄；首次真正要用时才加载（加载要几秒）
_WHISPER_TRIED = False     # 只尝试加载一次，失败不要每次请求都重试


def _asr_local_model():
    """本地 whisper 模型；没装 faster-whisper 就返回 None。

    懒加载：不配语音的场景不该为它付启动时间。失败也**只试一次** ——
    否则每个请求都会重新 import 一遍再失败，日志会刷屏。
    """
    global _WHISPER, _WHISPER_TRIED
    if _WHISPER_TRIED:
        return _WHISPER
    _WHISPER_TRIED = True
    if os.environ.get("RW1_WHISPER_DISABLE"):
        return None
    name = os.environ.get("RW1_WHISPER_MODEL", "base")
    try:
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel(name, device="cpu", compute_type="int8")
        print("  本地 whisper 已加载：%s" % name)
    except Exception as exc:  # noqa: BLE001 —— 没装就是"没有本地识别"，不是错误
        print("  本地 whisper 不可用（%s）。需要离线识别请 pip install faster-whisper"
              % exc.__class__.__name__)
        _WHISPER = None
    return _WHISPER


def asr_backend():
    """当前会走哪条识别路：'cloud' / 'local' / None。"""
    if _asr_cloud_cfg():
        return "cloud"
    return "local" if _asr_local_model() is not None else None


def _transcribe_cloud(cfg, wav):
    """调 OpenAI 兼容的 /audio/transcriptions。返回 (text, err)。

    multipart 是手搓的 —— 项目至今只用标准库，不想为一个上传引入 requests。
    分隔串带随机数，避免音频字节里恰好出现分隔符把报文切坏。
    """
    boundary = "----rw1asr" + os.urandom(8).hex()
    body = b"".join([
        ("--%s\r\n" % boundary).encode(),
        b'Content-Disposition: form-data; name="model"\r\n\r\n',
        cfg["model"].encode() + b"\r\n",
        ("--%s\r\n" % boundary).encode(),
        b'Content-Disposition: form-data; name="language"\r\n\r\nzh\r\n',
        ("--%s\r\n" % boundary).encode(),
        b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n',
        b"Content-Type: audio/wav\r\n\r\n",
        wav + b"\r\n",
        ("--%s--\r\n" % boundary).encode(),
    ])
    req = urllib.request.Request(
        cfg["base"] + "/audio/transcriptions", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                 "Authorization": "Bearer " + cfg["key"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=ASR_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (str(data.get("text") or "").strip() or None), ""
    except Exception as exc:  # noqa: BLE001
        return None, "云端识别失败(%s)" % exc.__class__.__name__


def transcribe(audio_bytes):
    """语音转文字。返回 (text, err)；成功时 err 为空字符串。

    **云端优先**：不占本机 CPU、延迟稳定。本地作为离线兜底 ——
    教室没外网时它就是唯一能用的那条路。两条都没有时，明确说清缺什么，
    而不是让板端对着一个空回复干等。
    """
    if not audio_bytes:
        return None, "录音是空的"
    wav = audio_bytes if audio_bytes[:4] == b"RIFF" else _pcm_to_wav(audio_bytes)

    cfg = _asr_cloud_cfg()
    if cfg:
        text, err = _transcribe_cloud(cfg, wav)
        if text:
            return text, ""
        # 云端失败**不立刻放弃**：本地能用就顶上，别把断网当致命错
        if _asr_local_model() is None:
            return None, err

    model = _asr_local_model()
    if model is None:
        return None, ("没配语音识别：设 RW1_ASR_API_KEY 走云端，"
                      "或 pip install faster-whisper 用本地")
    try:
        segs, _info = model.transcribe(io.BytesIO(wav), language="zh")
        return (("".join(s.text for s in segs)).strip() or None), ""
    except Exception as exc:  # noqa: BLE001
        return None, "本地识别失败(%s)" % exc.__class__.__name__


def ask_llm(dev, question):
    """可选：调用 OpenAI 兼容大模型。未配置/失败返回 None → 回退本地模板。

    第 4 周「澄清」：带上该设备最近的对话历史，这样"再详细点""那刚才呢"
    这类追问能被理解。历史**按设备隔离**（课堂 20 块板共用一台服务器），
    而且只放问答文本 —— 每轮都重塞一遍遥测摘要既费 token 又会把
    上下文淹没在重复内容里。
    """
    key = os.environ.get("RW1_LLM_API_KEY")
    if not key:
        return None
    base = os.environ.get("RW1_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("RW1_LLM_MODEL", "gpt-4o-mini")
    with LOCK:
        history = list(dev.chat)
    # ai_summary() 内部要取 LOCK，必须在 with LOCK 之外调用（否则自锁）
    messages = [{"role": "system",
                 "content": "你是嵌入式课堂助手。回答要简短（120 字以内），口语化，用中文，"
                            "不要用 Markdown 标题或列表。"}]
    messages.extend({"role": r, "content": c} for r, c in history)
    messages.append({"role": "user",
                     "content": "开发板传感器情况：%s\n同学想问：%s" % (ai_summary(dev), question)})
    payload = {"model": model, "messages": messages, "max_tokens": 300}
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


def _llm_worker(dev, question, token):
    """后台线程：调大模型，完成后把结果写回该设备的状态。

    注意 ai_summary() 内部会取 LOCK，必须在进入 with LOCK 之前调用，否则自锁。

    `token` 是发起这次请求时的取消令牌。回来时若令牌已变（用户按了「停止」），
    结果就**直接丢弃** —— 而不是先写进去再被清掉，那样屏幕上会闪一下旧答案。

    并发上限见 `_LLM_SEM`（LLM_MAX_CONCURRENT）：全班同时提问时排队，
    排太久就明说并回退本地 AI，不让请求烂在队列里。
    """
    reply = None
    if _LLM_SEM.acquire(timeout=LLM_QUEUE_TIMEOUT):
        try:
            reply = ask_llm(dev, question)
        finally:
            _LLM_SEM.release()
    else:
        push_event(dev, "warn", "大模型请求排队超过 %gs，已用本地 AI 回复"
                                % LLM_QUEUE_TIMEOUT)
    mode = "大模型" if reply else "规则AI"
    if not reply:
        reply = ai_summary(dev)
    with LOCK:
        if dev.st["ai_cancel"] != token:
            dev.st["voice_state"] = "idle"    # 被停止：别把板端留在"思考中"
            return                       # 已被取消：这次结果作废
        now = time.time()
        dev.st["ai_pending"] = False
        dev.st["ai_reply"] = reply
        dev.st["ai_mode"] = mode
        dev.st["voice_state"] = "idle"
        t0 = dev.st["ai_t0"] or now          # 计时：生成本身花了多久
        dev.st["ai_ms"] = int(max(0.0, now - t0) * 1000)
        dev.st["ai_t0"] = 0.0
        _finish_turn(dev, now)               # 语音任务的话，再补一个端到端耗时
        # 记进对话历史（第 4 周「澄清」的原料）
        dev.chat.append(("user", question))
        dev.chat.append(("assistant", reply))
    push_event(dev, "ai" if mode == "大模型" else "warn",
               "%s回复：%s" % (mode, reply[:60]))


# --------------------------------------------------------------------------
# 自然语言「请求」（第 4 周）
#
# 课程第 4 周原文：「用自然语言查询与请求采集」——把自然语言分别映射为
# **读取已有记录** 或 **请求一次新采集**，并把自建接口封装成**受限工具**。
# 「查询」那一半第 4 周就做了（提问 → 大模型/规则AI → 屏显回答）；
# 这里补的是「请求」那一半：用户说一句祈使句，服务端把它翻成一条**已有**的
# 远程指令（第 2/3 周那套 request_id 机制），板子真的去执行，结果回显到屏幕上。
#
# 为什么用规则而不是大模型做意图识别：
#   1. 教室可能没外网、也没配 key —— 而"让它拍张照"恰恰最该离线可用；
#   2. 规则是确定的：同一句话永远解析成同一条指令，"它怎么听懂的"讲得清；
#   3. 为一句"拍张照"付一次 API 往返，反而更慢。
#
# **安全边界（硬约束）**：自然语言只开放"安全、可逆、看得见"的指令。
# 格式化 SD 卡、改 WiFi、清配置、重启这些破坏性或会让板子失联的动作**一律不接**
# —— 语音识别错一个字就可能把卡格了，代价不对等。
# --------------------------------------------------------------------------
NL_MAX_LEN = 40            # 超过这个长度的话不当作设备请求（多半是在聊天）
# 自然语言可触发的指令白名单。**必须都在 CMD_NAMES 里**（tools/verify_server.py 会验）。
NL_SAFE_COMMANDS = ("capture_once", "cam_capture", "cam_stream",
                    "led_blink", "led_set", "set_orient", "sd_ls")
# 明确拒绝的危险动作：给一句"去哪儿做"的指路，而不是假装没听懂。
NL_DANGEROUS = (
    ("sd_format",    ("格式化", "格卡", "清空卡", "清空存储", "清一下卡")),
    ("set_config",   ("wifi", "wi-fi", "ssid", "无线网", "改密码", "换密码",
                      "改服务器", "服务器地址", "连别的网")),
    ("clear_config", ("清除配置", "清配置", "恢复出厂", "重置配置", "清空配置")),
    ("reboot",       ("重启", "重新启动", "重启板", "重启设备")),
)
_NL_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                 "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _nl_number(text, lo, hi, dflt):
    """从一句话里取一个数字：阿拉伯数字优先，其次中文数字（支持 0–99）。"""
    m = re.search(r"\d+", text)
    if m:
        return max(lo, min(hi, int(m.group())))
    s = text
    for i, ch in enumerate(s):
        if ch not in _NL_CN_DIGITS:
            continue
        if ch == "十":                                  # 十 / 十二
            nxt = _NL_CN_DIGITS.get(s[i + 1]) if i + 1 < len(s) else None
            v = 10 + (nxt if nxt is not None and nxt < 10 else 0)
            return max(lo, min(hi, v))
        v = _NL_CN_DIGITS[ch]
        if i + 1 < len(s) and s[i + 1] == "十":         # 二十 / 二十三
            v *= 10
            nxt = _NL_CN_DIGITS.get(s[i + 2]) if i + 2 < len(s) else None
            if nxt is not None and nxt < 10:
                v += nxt
        return max(lo, min(hi, v))
    return dflt


def parse_intent(text):
    """把一句话解析成 (指令名, 参数, 回话)；不是设备请求就返回 None。

    `回话` 会直接写进 ai_reply —— 板子屏幕上显示的就是它，所以要短、口语化。
    危险动作返回指令名 `__refuse__`：调用方只回话、不下发。

    规则是"宁可漏判、不可错判"的顺序：先拦危险动作，再按"最不可能被别的词
    顺带命中"的顺序匹配（画面 → 拍照 → 采集 → 存储 → 灯 → 档位）。
    """
    q = (text or "").strip()
    if not q or len(q) > NL_MAX_LEN:
        return None
    # 去空格与常见标点：语音识别出来的句子标点位置不固定，匹配前先抹平
    low = q.lower().replace(" ", "").replace("，", "").replace("。", "") \
           .replace("！", "").replace("？", "").replace(",", "")

    # 0) 危险动作先拦：宁可"多拒绝一句正常话"，也不能让语音把卡格了
    for name, words in NL_DANGEROUS:
        if any(w in low for w in words):
            return ("__refuse__", name, "这个动作太危险，请到网页的指令区手动操作")

    # 1) 实时画面开关（先于"拍照"：「开实时画面」和「拍张照」都可能带"画面/照"）
    if any(w in low for w in ("实时画面", "画面", "推流", "视频", "直播", "监控")):
        if any(w in low for w in ("关", "停", "别", "不要", "闭", "撤")):
            return ("cam_stream", {"on": False}, "好，关掉实时画面")
        if any(w in low for w in ("开", "启", "看", "要", "来")):
            return ("cam_stream", {"on": True}, "好，打开实时画面")

    # 2) 拍照
    if any(w in low for w in ("拍照", "拍张照", "拍一张", "拍个照", "拍一下",
                              "照一张", "照个相", "照相", "拍张相")):
        return ("cam_capture", {}, "好，让板子拍一张照…")

    # 3) 采集一次。**必须区分"重新采集"和"查看上次数据"** —— 这是课程第 4 周
    #    的当堂验证项：同一份旧数据，前者要等一个**新结果**，后者要保留**旧时间**。
    #    分不清的话，「上次采的数据是多少」会被当成新采集请求，屏幕回一句
    #    「让板子采集一次…」，而用户其实只想看已有记录（该走查询路径）。
    if any(w in low for w in ("采集", "测一下", "量一下", "测一测",
                              "量一量", "采一次", "采个数")):
        look_back = any(w in low for w in ("上次", "之前", "刚才", "历史",
                                           "已有", "原来", "旧的"))
        fresh = any(w in low for w in ("重新", "再", "新", "重"))
        if look_back and not fresh:
            return None          # 交给查询路径（带着已有数据回答，不产生新采集）
        return ("capture_once", {}, "好，让板子采集一次…")

    # 4) 看存储卡
    if any(w in low for w in ("卡里", "存储", "sd卡", "文件列表", "存了")):
        if any(w in low for w in ("看", "列", "查", "有", "多少", "什么", "读")):
            return ("sd_ls", {}, "好，看一下存储卡…")

    # 5) 灯：闪 / 开 / 关
    if "闪" in low:
        n = _nl_number(q, 1, LED_MAX_BLINKS, 3)
        return ("led_blink", {"n": n, "pattern": "ack"}, "好，让板子闪 %d 下" % n)
    if "灯" in low:
        if any(w in low for w in ("关", "灭", "熄")):
            return ("led_set", {"on": False}, "好，把灯关掉")
        if any(w in low for w in ("开", "亮", "点")):
            return ("led_set", {"on": True}, "好，把灯点亮")

    # 6) 方向档位
    if any(w in low for w in ("档", "校准", "方向")):
        if any(w in low for w in ("切", "调", "换", "设", "改", "第", "到")):
            o = _nl_number(q, 0, 15, None)
            if o is not None:
                return ("set_orient", {"o": o}, "好，把方向档位切到 %d" % o)

    return None


def _nl_result_reply(name, ok, res):
    """指令执行完后，回给板端屏幕的一句人话。"""
    if not ok:
        return "指令没做成：%s" % (res.get("err") or "未说明")
    if name == "cam_capture":
        return "拍好了，存进存储卡了"
    if name == "capture_once":
        return "采好了：x=%+.2f y=%+.2f z=%+.2f" % (res.get("x", 0.0), res.get("y", 0.0),
                                                   res.get("z", 0.0))
    if name == "sd_ls":
        n = max(0, res.get("note", "").count(";") - 1)
        return "存储卡里有 %d 个文件" % n
    if name == "led_blink":
        return "闪好了"
    if name == "led_set":
        return "灯已切换"
    if name == "cam_stream":
        return "实时画面已切换"
    if name == "set_orient":
        return "档位已切好"
    return "指令完成"


def run_intent(dev, question, intent):
    """执行一条自然语言请求：下发指令，并**立刻**回一句"正在做…"。

    立刻回话很重要 —— 指令要走两个遥测周期才回来（默认 1 秒），
    这期间板端屏幕不该停在上一句回答上，用户会以为没听懂。
    """
    name, params, spoken = intent
    with LOCK:
        # **作废可能还在后台跑的那次大模型生成**（与「停止」用同一个令牌机制）。
        # 不作废的话：用户刚问完一个问题（模型要几秒），紧接着说「拍张照」，
        # 指令结果先写到屏幕上、几秒后被那次**旧回答**盖掉 —— 用户看到的是
        # "这是刚才那个问题的答案"，而他明明已经改口了。
        dev.st["ai_cancel"] += 1
        turn = dev.st["ai_cancel"]            # 本轮令牌：这条指令的结果必须带着它回来
        dev.st["ai_pending"] = False          # 不走大模型，别让板端一直转圈
        dev.st["ai_reply"] = spoken
        dev.st["ai_mode"] = "指令"
        dev.st["ai_ms"] = 0                   # 规则解析，没有"生成"这一步
        dev.st["ai_t0"] = 0.0
        dev.chat.append(("user", question))
        dev.chat.append(("assistant", spoken))
    if name == "__refuse__":
        push_event(dev, "warn", "自然语言请求「%s」被拒：%s 不接自然语言（危险动作）"
                   % (question, params))
        with LOCK:
            _finish_turn(dev)                 # 没有后续指令了，这一轮到此为止
        return spoken
    # new_command 内部要取 LOCK，必须在上面的 with 之外调用
    cid = new_command(dev, name, params, source="nl", turn=turn)
    push_event(dev, "cmd", "自然语言请求「%s」→ 指令 %s（%s）" % (question, name, cid))
    return spoken


def request_ai(dev, question):
    """处理一次板端提问，**立即**返回文本；大模型在后台生成，稍后自动生效。

    板端每个遥测周期都会带回最新的 ai_reply，所以后台结果下一帧就会显示，
    不需要板端等待，也就彻底消除了"板端 3s 超时 vs 服务端 8s 等待"的死结。

    `ai_pending` 是**按设备**的：A 板在等大模型不该让 B 板的提问被吞掉。

    第 4 周（「请求」那一半）：**先看这是不是一句"设备请求"**（「拍张照」「开实时画面」…）。
    是的话直接翻成远程指令下发，不走大模型 —— 快、离线可用、结果确定。
    """
    intent = parse_intent(question)
    if intent is not None:
        return run_intent(dev, question, intent)

    if os.environ.get("RW1_LLM_API_KEY"):
        with LOCK:
            if dev.st["ai_pending"]:
                return dev.st["ai_reply"] or "我还在想上一个问题，稍等一下…"
            # 新一轮开始：令牌 +1，让所有"上一轮"的异步产物作废
            # （迟到的指令结果、迟到的语音识别都靠它对不上号而自行丢弃）。
            dev.st["ai_cancel"] += 1
            token = dev.st["ai_cancel"]      # 记下本次的令牌
            dev.st["ai_pending"] = True
            dev.st["ai_reply"] = "正在思考…"
            dev.st["ai_mode"] = "大模型"
            dev.st["ai_t0"] = time.time()    # 计时：生成从这一刻算起
            dev.st["ai_ms"] = 0
        push_event(dev, "ask", "板端提问「%s」→ 已转交大模型" % question)
        threading.Thread(target=_llm_worker, args=(dev, question, token), daemon=True,
                         name="llm").start()
        return "正在思考…"

    reply = ai_summary(dev)
    with LOCK:
        dev.st["ai_reply"] = reply
        dev.st["ai_mode"] = "规则AI"
        dev.st["ai_ms"] = 0                  # 规则AI 是本地模板，没有"生成"耗时
        dev.st["ai_t0"] = 0.0
        _finish_turn(dev)                    # 本轮到此结束，记下端到端耗时
        dev.chat.append(("user", question))
        dev.chat.append(("assistant", reply))
    push_event(dev, "ask", "板端提问「%s」→ %s" % (question, reply[:40]))
    return reply


def cancel_ai(dev):
    """「停止」：作废正在生成的那次回答。返回是否真的停掉了什么。

    只递增令牌，不依赖 worker 有没有及时看到某个 flag —— 无论它什么时候回来，
    对不上令牌就自己丢弃。这样"停止"在慢网络/慢模型下也是确定的。

    第 6 周补上：**还没回话的自然语言指令也算"有事可停"**。用户对着板子说
    「拍张照」，板子还在拍的时候按停止，屏幕不该一直停在「好，拍张照…」——
    递增令牌会让那条指令的结果回来时被丢弃，屏幕上就停在「已停止」。
    """
    with LOCK:
        was = dev.st["ai_pending"]
        inflight = any(r.get("source") == "nl"
                       and r.get("nl_turn") == dev.st["ai_cancel"]
                       and r["state"] in ("queued", "sent")
                       for r in dev.commands.values())
        dev.st["ai_cancel"] += 1
        dev.st["ai_pending"] = False
        if was or inflight:
            dev.st["ai_reply"] = "已停止"
            dev.st["ai_mode"] = "规则AI"
            dev.st["voice_state"] = "idle"
            dev.st["ai_t0"] = 0.0
            _finish_turn(dev)
    if was or inflight:
        push_event(dev, "warn", "已停止正在生成的回复")
    return was or inflight


def _voice_worker(dev, data, saved, turn=None):
    """后台线程：识别 → 交给大模型 → 结果由后续遥测帧带回板端。

    识别失败**必须**把 voice_state 复位并留下原因 —— 否则板端会永远停在
    「识别中」，用户只会觉得板子死了。

    `turn` 是收到这段录音时的回合令牌（第 6 周「迟到结果隔离」）。识别要几秒
    （云端更久），这期间用户完全可能已经在网页上打字问了别的问题 ——
    那种情况下这段**迟到的识别结果属于上一轮**，不能再拿它去调 request_ai，
    否则会用一个旧问题顶掉用户刚得到的新答案。
    """
    t0 = dev.st.get("voice_t0") or 0.0
    text, err = transcribe(data)
    now = time.time()
    with LOCK:
        if turn is not None and dev.st["ai_cancel"] != turn:
            # 用户已经改口了：这段识别结果作废，但状态要复位，别让板端卡在「识别中」
            dev.st["voice_state"] = "idle"
            dev.st["voice_err"] = "已忽略（这期间你又问了别的）"
            dev.st["voice_ms"] = int(max(0.0, now - t0) * 1000) if t0 > 0 else 0
            dev.st["voice_t0"] = 0.0
            late = True
        else:
            late = False
            dev.st["voice_ms"] = int(max(0.0, now - t0) * 1000) if t0 > 0 else 0
    if late:
        push_event(dev, "warn", "语音识别结果来晚了（用户已开始新一轮），已丢弃")
        return
    if not text:
        with LOCK:
            dev.st["voice_state"] = "idle"
            dev.st["voice_err"] = err or "没听清"
            # 屏幕上不能一直停在「正在识别…」—— 那看起来就像板子死了
            dev.st["ai_reply"] = "没听清，再说一次？"
            _finish_turn(dev, now)
        push_event(dev, "warn", "语音识别失败：%s" % (err or "没听清"))
        return
    with LOCK:
        dev.st["voice_text"] = text
        dev.st["voice_err"] = ""
        dev.st["voice_state"] = "thinking"
    push_event(dev, "ask", "识别到「%s」%s" % (text, ("（%s）" % saved) if saved else ""))
    request_ai(dev, text)
    # 规则 AI 是同步返回的，这会儿已经出结果了；大模型还要等后台线程，
    # 那种情况下 voice_state 留在 "thinking"，等 _llm_worker 收尾时自然回落。
    with LOCK:
        if not dev.st["ai_pending"]:
            dev.st["voice_state"] = "idle"


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
    # 套接字超时。不设的话，一个"连上但不说话"的客户端（或睡着的笔记本挂着的
    # SSE 半开连接）能永久占住一个线程 —— ThreadingHTTPServer 每连接一线程、
    # 且没有上限，几十个睡着的浏览器标签就够把线程数堆起来。
    # SSE 那边每 500 ms 一写，正常客户端永远不会触发；写超时被 `except OSError`
    # 接住并退出循环，是干净的收尾路径。
    timeout = 30

    def log_message(self, fmt, *args):   # 安静一点
        pass

    # ---- helpers ---------------------------------------------------------
    def _origin_ok(self):
        """写入口的同源校验（CSRF 防护）。

        威胁模型换个角度看就成立了：**同学/老师浏览任意网页时，那个网页可以
        `fetch('http://<本机IP>:8000/api/command', {mode:'no-cors'})`** ——
        CORS 只挡"读响应"，**挡不住请求真的被执行**。配合默认无 token，
        一条表单就能把 SD 卡格了。

        规则：浏览器会自动带 `Origin`（跨站 POST 一定带）。带了就必须和本次
        请求的 Host 一致；不带（curl / 板端 / 老工具）一律放行 —— 那些都不是
        浏览器自动发起的跨站请求，挡住了只会误伤脚本。
        反向代理把 Host 改掉的场景，可以用 `RW1_ALLOW_CROSS_ORIGIN=1` 关掉这道检查。
        """
        if ALLOW_CROSS_ORIGIN:
            return True
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            origin = (self.headers.get("Referer") or "").strip()
        if not origin:
            return True
        netloc = urlsplit(origin).netloc.strip().lower()
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(netloc) and netloc == host

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
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # `extra` 里给了同名头就以 extra 为准。原来是无条件先写 no-store 再追加，
        # 于是 /api/shots/<dev>/<file> 的响应同时带着
        # `no-store` 和 `max-age=31536000` 两个互相矛盾的值（实测确认）——
        # 浏览器按最严的处理，照片于是永远不缓存，每翻一次列表就重下一遍。
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        # 每个请求一连接就关：板端本来就是一次 POST 一个 client，浏览器这边请求也
        # 很少。省掉 keep-alive 的状态机，行为对两端都最好预测。
        # ⚠️ 代价见 README「性能」：32 板 × 2 Hz 时服务端侧会堆 TIME_WAIT，
        # 这也是 period_ms 下界必须卡在 100ms 的原因（见 sanitize_params）。
        self.send_header("Connection", "close")
        for k, v in extra.items():
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

    def _drain(self, n):
        """把请求体读掉再回绝。

        不读就直接响应、又带 `Connection: close`，客户端拿到的常常是
        **连接被重置（WinError 10054）而不是我们精心写的那个 400** ——
        因为服务端关连接时内核还有未读数据，会发 RST 把响应丢掉。
        拒绝也要把话说完整。上限 4 倍是防止一个超大 body 把我们拖在这里。
        """
        if 0 < n <= 4 * AUDIO_MAX_BYTES:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    # ---- routing ---------------------------------------------------------
    def do_OPTIONS(self):                       # CORS 预检
        self._send(204, b"")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, dashboard_html(), "text/html; charset=utf-8")
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
                # 瘦身通道：只带**活状态**（设备名/在线/活动/计数/AI/语音/计时）
                # + 曲线样本 + 设备列表。events 与 commands 是 SSE 的独通道 ——
                # 见 snapshot() 的 docstring：10Hz 在锁内序列化全量历史会跟
                # 20 块板的上报抢锁，而同一个浏览器页面 500ms 后就会从 SSE
                # 拿到它们。**不要再往这里加历史字段**（T47 的教训反过来用：
                # 两条通道可以分工，但同一份数据只能有一个权威来源）。
                snap = snapshot(dev, with_events=False, with_commands=False)
                # ⚠️ **必须带上设备列表**：网页的 pull() 是每 100ms 一次，
                # 而 renderDevices() 在"列表 ≤ 1 台"时会把「设备」区域藏起来。
                # 早先这里不带 devices，于是多板课堂下那一块会以取数频率疯狂闪烁
                # （SSE 每 500ms 把它显示出来、/api/latest 每 100ms 又把它藏起来），
                # 而且大部分时间是不可见的 —— 2026-09-26 用真浏览器才抓到。
                snap["devices"] = devices_snapshot()
                # 曲线点数：**按画布宽度取，不按窗口长度取**。
                # 那块 canvas 的 CSS 宽度约 240 px（高分屏 480 物理像素），
                # 120 个点已经超过可分辨的像素数；原来发 240 个点，多出来的一半
                # 在 drawChart 里直接落在同一列像素上 —— 纯字节。实测载荷
                # 16.3 KB → 约 8.6 KB。
                snap["samples"] = [[round(t, 2), x, y, z]
                                   for (t, x, y, z) in decimate(dev.samples, CURVE_POINTS)]
                # 序列化也必须在锁内：events/commands 的快照与活记录共享可变字段，
                # 锁外 json.dumps 会把"拍完之后"的迁移混进这一帧（mixed snapshot）。
                # 只把**字节**先在锁内做好；socket 发送留在锁外 —— 持锁写一个
                # 慢客户端会连累全部板子的上报（T46 同款道理：别在锁里做 IO）。
                payload = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif path == "/api/commands":
            want = query_param(query, "device")
            with LOCK:
                dev = pick_device(clean_device_id(want) if want else None)
                cmds = commands_snapshot(dev)
                did = dev.id
            self._send(200, json.dumps({"ok": True, "device": did, "commands": cmds,
                                        "names": list(CMD_NAMES)}, ensure_ascii=False))
        elif path == "/api/events":
            # 事件流的"全量"出口。SSE 每帧只发最近 SSE_EVENTS_MAX 条（那是前端
            # 真正会画出来的量），想翻更早的历史就走这里 —— 服务端保留 200 条。
            want = query_param(query, "device")
            limit = clamp_int(query_param(query, "limit"), 1, 200, 200)
            with LOCK:
                dev = pick_device(clean_device_id(want) if want else None)
                evts = list(dev.events)[:limit]
                kept = len(dev.events)
                did = dev.id
            self._send(200, json.dumps({"ok": True, "device": did,
                                        "events": evts, "kept": kept},
                                       ensure_ascii=False))
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
        # CSRF：写入口必须是"同源"发起的（见 _origin_ok 的威胁模型）。
        # 放在密钥门之前 —— 跨站请求连"试一次 token"的机会都不该有。
        if path in WEB_WRITE_PATHS and not self._origin_ok():
            self._send(403, json.dumps(
                {"ok": False, "error": "cross-origin blocked",
                 "hint": "写操作只接受同源请求（用仪表盘页面操作，或去掉 Origin 头）"},
                ensure_ascii=False))
            return
        # 网页写入口的访问密钥门（API_TOKEN 为空时整段跳过，行为与以前一样）。
        # 故意**不门** /api/telemetry、/api/frame、/api/audio：那是板子的上报
        # 通道，老固件没有密钥可带，门了就等于把全班挡在门外（后向兼容 > 洁癖）。
        if API_TOKEN and path in WEB_WRITE_PATHS:
            got = (self.headers.get("X-RW1-Token")
                   or query_param(query, "token") or "")
            if not hmac.compare_digest(got.encode("utf-8"),
                                       API_TOKEN.encode("utf-8")):
                self._send(401, json.dumps(
                    {"ok": False, "error": "bad token",
                     "hint": "服务端开启了访问密钥：网页页脚点「🔑 密钥」填写"},
                    ensure_ascii=False))
                return
        if path == "/api/telemetry":
            self._telemetry()
        elif path == "/api/command":
            self._command()
        elif path == "/api/frame":
            self._frame(query)
        elif path == "/api/audio":
            self._audio(query)
        elif path == "/api/ask":
            self._ask(query)
        elif path == "/api/ask/cancel":
            self._ask_cancel(query)
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
            self._drain(n)          # 同上：不读掉 body 客户端会收到 RST 而非 400
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
            # 基数封顶：设备名是外部输入，不封的话"无限设备名 × 400 KB"就是
            # 一条不需要任何凭据的内存耗尽路径（见 FRAME_MAX_DEVICES 的注释）。
            while len(FRAMES) > FRAME_MAX_DEVICES:
                victim = min(FRAMES, key=lambda k: FRAMES[k]["ts"])
                if victim == dev:
                    break                    # 刚写进来的这台永远是"最新"，不会选中
                FRAMES.pop(victim, None)
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
            # 设备目录数封顶：每台设备 200 张是"每台的上限"，但设备名是外部输入，
            # 没有这一道就能用无限个设备名造出无限个目录（磁盘写满）。
            # 已经在册的设备照常写；新设备到顶就不存 —— 板端只是少一份留档，
            # 网页上那张照片本来就是"顺手留的"，不影响拍照本身。
            if not os.path.isdir(d) and self._shot_dev_dirs() >= SHOT_MAX_DEVICES:
                return None
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

    @staticmethod
    def _shot_dev_dirs():
        """`SHOTS_DIR` 下已经有多少个设备目录（用于 SHOT_MAX_DEVICES 封顶）。"""
        try:
            with os.scandir(SHOTS_DIR) as it:
                return sum(1 for e in it if e.is_dir())
        except OSError:
            return 0

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

    # ---- voice (板 → 服务器 → 识别 → 大模型 → 板/网页) ----------------------
    def _audio(self, query):
        """板子 POST 上来的一段录音（裸 PCM，或已经带头部的 WAV）。

        `?device=X`。识别丢到**后台线程**做 —— 云端识别要好几秒，
        绝不能让板端等（这正是当初大模型踩过的坑：板端 3s 超时 vs 服务端 8s 等待）。
        板端拿到的是"收到了"，真正的文字与回复由后续遥测帧带回。
        """
        dev_id = clean_device_id(query_param(query, "device"))
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > AUDIO_MAX_BYTES:
            self._drain(n)
            self._send(400, json.dumps({"ok": False, "error": "bad audio size"},
                                       ensure_ascii=False))
            return
        data = self.rfile.read(n)
        # 太短的不可能是人话（16 kHz 下 64 字节 = 2 ms），直接拒掉省一次识别
        if len(data) < 64:
            self._send(400, json.dumps({"ok": False, "error": "audio too short"},
                                       ensure_ascii=False))
            return
        # 限流（见 AUDIO_MIN_GAP_S）：**必须在读完 body 之后**再回绝，否则客户端
        # 拿到的是连接重置而不是 429（手册 T58 的老坑）。被拒的也不更新时刻，
        # 这样"连发 10 次"只会全部被拒、不会积攒下一次的额度。
        with LOCK:
            now_a = time.time()
            # `AUDIO_LAST` 的键是外部输入（设备名），会随"无限设备名"长大；
            # 到 256 条就顺手清掉 60 秒前的记录，保证这张表本身有界。
            if len(AUDIO_LAST) > 256:
                for k in [k for k, t in AUDIO_LAST.items() if now_a - t > 60.0]:
                    AUDIO_LAST.pop(k, None)
            too_soon = (now_a - AUDIO_LAST.get(dev_id, 0.0)) < AUDIO_MIN_GAP_S
            if not too_soon:
                AUDIO_LAST[dev_id] = now_a
        if too_soon:
            self._send(429, json.dumps(
                {"ok": False, "error": "too soon", "hint": "同一台设备 %.1f 秒内只收一段录音"
                 % AUDIO_MIN_GAP_S}, ensure_ascii=False))
            return
        with LOCK:
            dev = get_device(dev_id)
            busy = dev.st["voice_state"] in ("transcribing", "thinking")
            if not busy:
                dev.st["voice_state"] = "transcribing"
                dev.st["voice_err"] = ""
                # 计时（第 6 周）：这一轮语音任务从"收到录音"这一刻算起。
                dev.st["voice_t0"] = time.time()
                dev.st["voice_ms"] = 0
                dev.st["total_ms"] = 0
                # 记下此刻的回合令牌：识别期间用户要是又问了一句，令牌会变，
                # 这段迟到的识别结果就该丢弃（第 6 周「迟到结果隔离」）。
                turn = dev.st["ai_cancel"]
                # 板端立刻有反馈：识别要几秒（云端更久），这段时间屏幕不该一动不动。
                # 但如果上一轮大模型还在生成，就别把它挤掉 —— 那会把快出结果的那次弄丢。
                if not dev.st["ai_pending"]:
                    dev.st["ai_reply"] = "正在识别…"
        if busy:
            # 429 的发送必须在 LOCK 之外：持锁写 socket，一个卡住的客户端会连累
            # 全部板子的上报 —— /api/latest 与 SSE 早就按这条规矩把序列化留在
            # 锁内、发送挪到锁外，这里不能做例外。
            self._send(429, json.dumps(
                {"ok": False, "error": "上一段还在处理"}, ensure_ascii=False))
            return
        saved = self._audio_save(dev, data)
        push_event(dev, "ask", "收到 %d 字节录音，开始识别" % len(data))
        threading.Thread(target=_voice_worker, args=(dev, data, saved, turn),
                         daemon=True, name="asr").start()
        self._send(200, json.dumps({"ok": True, "bytes": len(data), "saved": saved},
                                   ensure_ascii=False))

    def _audio_save(self, dev, data):
        """录音留档成 .wav，返回文件名（失败 None）。"""
        if not AUDIO_DIR:
            return None
        try:
            d = os.path.join(AUDIO_DIR, safe_name(dev.id))
            os.makedirs(d, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-%03d.wav" % (int(time.time() * 1000) % 1000)
            wav = data if data[:4] == b"RIFF" else _pcm_to_wav(data)
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(wav)
            files = sorted(f for f in os.listdir(d) if f.endswith(".wav"))
            for f in files[:-AUDIO_KEEP]:
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
            return name
        except OSError as e:
            print("  录音存盘失败: %s" % e)
            return None

    def _ask(self, query):
        """文字入口（第 5 周）：网页上打字，走**和说话完全相同**的那条链路。

        课程第 5 周要求「用同一句任务对照文字和语音入口」—— 对照的前提是两条入口
        真的走同一套处理，而不是各写一份。所以这里只做一件事：把文字交给
        `request_ai()`，剩下的（意图解析 / 大模型 / 指令下发 / 结果回显）与
        `_voice_worker` 里一字不差。改一处，两条入口一起变。

        大模型那条路是异步的：这里返回的 `reply` 只是「正在思考…」，
        真正的回答会通过后续 SSE 推送过来（网页不用轮询）。
        """
        msg = self._read_json()
        text = str(msg.get("text", "")).strip()[:200]
        if not text:
            self._send(400, json.dumps({"ok": False, "error": "empty text"},
                                       ensure_ascii=False))
            return
        # 设备可以走 query（与 /api/ask/cancel 一致，网页直接复用 devQuery()）
        want = query_param(query, "device") or msg.get("device")
        with LOCK:
            dev = pick_device(clean_device_id(want) if want else None)
            # 打字进来的这一轮不是"语音任务"，没有录音起点 —— 清掉，
            # 免得待会儿拿上一个语音任务的起点去算端到端耗时（数字会是错的）。
            dev.st["voice_t0"] = 0.0
        # request_ai 内部会取 LOCK，必须在 with 之外调用
        reply = request_ai(dev, text)
        with LOCK:
            out = {"ok": True, "device": dev.id, "reply": reply,
                   "mode": dev.st["ai_mode"], "pending": dev.st["ai_pending"],
                   "voice_ms": dev.st["voice_ms"], "ai_ms": dev.st["ai_ms"],
                   "total_ms": dev.st["total_ms"]}
        self._send(200, json.dumps(out, ensure_ascii=False))

    def _ask_cancel(self, query):
        """「停止」：作废正在生成的那次回答（板端手势或网页按钮都能调）。"""
        want = query_param(query, "device")
        with LOCK:
            dev = pick_device(clean_device_id(want) if want else None)
        stopped = cancel_ai(dev)
        self._send(200, json.dumps({"ok": True, "stopped": stopped, "device": dev.id},
                                   ensure_ascii=False))

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

        # ⚠️ **写接口不许"猜"目标**：只要调用方明确点了设备名，就必须真存在。
        #
        # 原来这里对"不在册的名字"会退回到"最近上报过的那台"（`pick_device` 的
        # 读接口语义），于是把设备名拼错一个字（比如「第三组-07」打成「第三组-7」）
        # 就会把 sd_format 发给**另一块板**，页面上还显示"已下发"。
        # 读接口（/api/latest）该容错，写接口不该 —— 破坏性动作的代价不对等。
        #
        # 判据用"不在 DEVICES 里"而不是"离线"：离线的板子仍在册，命令排队等它
        # 回来是**设计行为**（网页上明确提示了"等它们回来才会执行"）。
        # 全有或全无：只要有一台不认识就一条都不发，避免"20 台里格了 19 台"。
        with LOCK:
            missing = [t for t in targets if t is not None and t not in DEVICES]
            if missing:
                known = sorted(d.id for d in DEVICES.values() if d.st["last_post"] > 0)
        if missing:
            self._send(404, json.dumps(
                {"ok": False, "error": "device not found", "missing": missing,
                 "known": known,
                 "hint": "设备名要以仪表盘列表里显示的为准；不在册的一律不下发"},
                ensure_ascii=False))
            return

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
            # dt 由"本批样本数 / 两批到达的间隔"自校准，不依赖板端上报。但**晚到的帧不可信**：
            # 若某帧因网络抖动晚到 3 秒，50 个样本的推断间隔会被算成 60 ms，
            # 这批样本就被摊到 3 秒的时间轴上 —— MOTION_WINDOW_S(0.6 s) 的短窗里
            # 只剩最后 1~2 个样本，晃动/跌落全部漏检，sample_hz 也会跳变。
            # 所以推断值明显偏大时沿用上一帧的可信值，不让它污染时间轴（P1-6）。
            if prev_post > 0 and len(pts) > 1:
                dt_est = (now - prev_post) / float(len(pts))
                dt_est = min(0.6, max(0.002, dt_est))
                if dt_est > DT_TRUST_MAX or dt_est < DT_TRUST_MIN:
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
        """支持两种载荷：新协议 batch=[[x,y,z],...]，以及单帧 x/y/z（向后兼容）。

        **每个点都要过 isValidSample()**：`json.loads` 默认接受 `NaN` / `Infinity`
        这两个非法 JSON 字面量，`float()` 也照收。一旦放进去：

          - `analyze()` 里 `tilt_direction()` 的 `int(nan)` 抛 ValueError，
            而且抛在 `_send()` **之前** —— 客户端拿到的是"连接被掐断"，不是 500；
          - 更糟的是 NaN 已经进了 `dev.samples`，**接下来 8 秒内每一帧正常数据**
            都会在同一个 `int(nan)` 上再次抛异常（窗口里还留着它）；
          - `/api/latest` 再把 NaN 序列化出去（`json.dumps` 允许，RFC 8259 不允许），
            浏览器 `JSON.parse` 抛错 → 整页停在旧数据上。

        固件侧早就有等价的一道（`transport.c` 的 `sanitize3`：任一轴越界整帧丢弃、
        **绝不填 0**，因为 0 恰好是失重特征）。服务端一直漏着，这里补上。
        """
        pts = []

        def take(item):
            """一个点要么完整可用，要么整点丢弃 —— 不做部分填充。"""
            try:
                x, y, z = float(item[0]), float(item[1]), float(item[2])
            except (TypeError, ValueError, IndexError, KeyError):
                return
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                return
            # 量程兜底：加速度计满量程 ±8g（固件 sanitize3 同一口径），
            # 16g 是给三轴合成留的余量。超出的一律当坏帧 —— 否则网页曲线
            # 的纵轴会被一个 1e300 直接压成一条线。
            if max(abs(x), abs(y), abs(z)) > VALID_G_MAX:
                return
            pts.append((x, y, z))

        batch = msg.get("batch")
        if isinstance(batch, list):
            for item in batch[:MAX_BATCH]:
                take(item)
        if not pts:
            try:
                take((msg["x"], msg["y"], msg["z"]))
            except (KeyError, TypeError):
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
                    snap = snapshot(dev, with_events=False, with_commands=False)
                    snap["devices"] = devices_snapshot(now)
                    snap["sample"] = list(dev.samples[-1]) if dev.samples else None
                    snap["now"] = now
                    # ⚠️ 只发"前端真的会画出来的那些"，别发整本历史。
                    #
                    # 原来这里发的是 snapshot(dev) 的全量：200 条事件 + 20 条命令，
                    # 单帧 35.3 KB、2 Hz = 69 KB/s —— 而 renderEvents() 只画
                    # `evts.slice(0,40)`、renderCmds() 只画前 8 条。也就是说
                    # **八成以上的字节从服务端搬到浏览器，然后在 DOM 里被丢掉**。
                    # 现在按渲染上限裁剪：40 + 8，单帧降到约 9 KB。
                    # 事件仍然是"最近 40 条"的滑动窗口（前端本来就是覆盖式重绘，
                    # 不需要增量协议）；要更多历史时另有 /api/events 可查。
                    snap["events"] = list(dev.events)[:SSE_EVENTS_MAX]
                    # 事件总条数（缓冲区里实际有多少，最多 200）。前端标题写着
                    # "最近 N 条" —— 裁剪之后如果还用 `evts.length`，N 会**永远
                    # 卡在 40**，看起来像"事件只有 40 条"，其实只是没发全。
                    # 多发一个整数换一句不说谎的文案。
                    snap["ev_total"] = len(dev.events)
                    snap["commands"] = commands_snapshot(dev)[:SSE_COMMANDS_MAX]
                    # 与 /api/latest 同修：序列化在锁内做，字节发出去在锁外。
                    # SSE 是长连接、每 500ms 一帧；持锁期间客户端网络卡一下，
                    # 全校板子的上报都要等它 —— 那正是第一需求场景下最怕的事。
                    frame = (b"data: " + json.dumps(snap, ensure_ascii=False).encode("utf-8")
                             + b"\n\n")
                self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # 简化：用默认线程池即可，SSE 长连接靠 ThreadingHTTPServer 每连接一线程


# --------------------------------------------------------------------------
# 网页仪表盘
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 网页仪表盘：从 server/web/index.html 读（不再内嵌成三引号字符串）
#
# 2026-10-30 拆出来的三个理由：
#   1. 2952 行 HTML/CSS/JS 住在 Python 字符串里 —— **没有语法高亮、没有 ESLint、
#      没有格式化**，改一行 JS 只能靠肉眼；
#   2. 它逼出一条古怪的规矩"页面里禁止出现反斜杠"（`\n` 会被 Python 先吃成真换行），
#      新人第一次写必然踩，而且报错位置指不到真正的原因；
#   3. 字符串在 import 时就固化了，**改样式必须重启服务端**。
#
# 现在按 mtime 缓存：改完刷新浏览器就生效。读取用文本模式，所以 git 在 Windows 上
# 检出成 CRLF 也不影响（Python 的 universal newlines 会归一成 \n）。
# --------------------------------------------------------------------------
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_DASH_CACHE = {"mtime": None, "html": ""}


def dashboard_html():
    """返回仪表盘页面；文件改动后自动重新加载。"""
    path = os.path.join(_WEB_DIR, "index.html")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        # 文件丢了要**说清楚**，而不是抛 500 让页面白屏 —— 这个仓库的原则是
        # 出错也要给人一句能读的话。
        return ('<!doctype html><meta charset="utf-8"><title>缺文件</title>'
                '<h1>仪表盘文件缺失</h1><p>预期位置：%s</p>'
                '<p>跑 <code>git status</code> 看是不是被删了。</p>' % path)
    if mtime != _DASH_CACHE["mtime"]:
        with open(path, encoding="utf-8") as fh:
            _DASH_CACHE["html"] = fh.read()
        _DASH_CACHE["mtime"] = mtime
    return _DASH_CACHE["html"]


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
    global SHOTS_DIR, AUDIO_DIR
    SHOTS_DIR = os.path.join(args.data_dir, "shots")
    # 录音留档：和照片一样按设备分目录、超出上限删最旧（第 4 周语音链路）
    AUDIO_DIR = os.path.join(args.data_dir, "audio")
    LOGGER = JsonlLogger(args.data_dir, retain_days=args.retain_days,
                         log_telemetry=not args.no_log_telemetry, log_hz=args.log_hz)

    if port_already_serving(args.host, args.port):
        print("!! 端口 %d 上已经有一个服务在应答了，不重复启动。" % args.port)
        print("   多半是上次没关干净的进程。先关掉它，或换个端口：--port 8001")
        print("   （Windows 允许两个进程绑同一端口，硬起会静默抢请求，非常难查。）")
        return 2

    try:
        srv = DashboardServer((args.host, args.port), Handler)
    except (PermissionError, OSError) as e:
        # WinError 10013：**端口号被别的程序当成了某条出站连接的本地端口**（详见
        # .agents/TROUBLESHOOTING.md T54）。这种情况 netstat 里**看不到 LISTENING**，
        # 所以上面的 port_already_serving() 探不出来，只有一串 traceback 抛出来 ——
        # 2026-09-28 我照着"端口冲突"的方向查了保留段、防火墙、代码，全查错方向。
        # 把话说清楚，别让下一个人再走一遍。
        print("!! 绑不上端口 %d：%s" % (args.port, e))
        print("   ⚠️ 这**不一定**是「端口被监听占用」—— Windows 上很常见的是")
        print("   **别的程序把 %d 当成了某条出站连接的本地端口**（实测占用者是" % args.port)
        print("   wpscloudsvr.exe，连腾讯云），那条连接卡在 CLOSE_WAIT 不释放。")
        print("   查法（★ **不要加 LISTENING 过滤**，加了就看不到）：")
        print("       netstat -ano | grep \":%d\"" % args.port)
        print("   拿到 PID 后 taskkill //PID <pid> //F；若杀完仍绑不上（socket 会在")
        print("   内核里滞留一会儿），**重启电脑**最干净。")
        print("   ★ 也可以**不重启**：绑 0.0.0.0 才冲突，绑具体地址是好的 ——")
        print("     实测 0.0.0.0 失败(10048) 而 127.0.0.1 / 本机 LAN IP 都能绑成功。")
        print("     所以用 `--host <本机无线网卡 IP>`（板子要连的那个）即可绕过。")
        print("   不要轻易换端口：板子 NVS 里配的就是这个端口，换端口要重新配网。")
        return 2

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
    # SIGTERM（systemd / docker stop / 关机）也走"优雅收尾"这条路：
    # 默认行为是直接杀进程，队列里没落盘的记录就没了。
    def _on_sigterm(*_args):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError, AttributeError):
        pass          # 非主线程 / 平台不支持：保持默认行为，不影响启动

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()
        if LOGGER is not None:
            written = LOGGER.close()
            print("   日志已收尾：本次共写 %d 行，丢弃 %d 行" % (written, LOGGER.dropped))


if __name__ == "__main__":
    main()

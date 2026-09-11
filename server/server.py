#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 交互课 · 第 1 周任务 —— PC 服务器（无需 VPS，自己的电脑即服务器）

职责：
  1. 接收 ESP32-S3-EYE 上报的 IMU 遥测数据（HTTP POST /api/telemetry）
  2. 对运动数据做实时分析（本地规则"AI"；配置了大模型 API 时自动升级为真实 LLM 回复）
  3. 向开发板返回交互结果（当前姿态/活动 + AI 回复文本），开发板在屏幕上显示
  4. 提供网页仪表盘（SSE 实时推送），在浏览器里看到板子的实时姿态、事件流与 AI 对话

仅依赖 Python 标准库，直接运行：
    python server.py                # 默认监听 0.0.0.0:8000
    python server.py --port 9000    # 自定义端口

可选：接入 OpenAI 兼容大模型（不配也能完整跑通流程）
    set RW1_LLM_API_KEY=sk-xxx
    set RW1_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    set RW1_LLM_MODEL=qwen-turbo
"""

import argparse
import json
import math
import os
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_WINDOW = 120          # 滑动窗口样本数（2Hz 上报 ≈ 1 分钟）
DEVICE_TIMEOUT = 5.0      # 超过该秒数没有新遥测则视为离线
LLM_TIMEOUT = 8.0         # 大模型请求超时（秒），超时回退到本地模板

# --------------------------------------------------------------------------
# 全局状态（GIL + 一把锁保护即可，数据量极小）
# --------------------------------------------------------------------------
LOCK = threading.Lock()
SAMPLES = deque(maxlen=MAX_WINDOW)        # [(ts, x, y, z)]
EVENTS = deque(maxlen=100)                # 事件流（网页显示）
STATE = {
    "device_online": False,
    "last_post": 0.0,
    "source": "-",
    "latest": None,                        # 最近一帧原始数据
    "activity": "等待数据…",
    "step_count": 0,
    "shake_count": 0,
    "ai_reply": "",
    "ai_mode": "规则AI",
    "boot_time": time.time(),
}


def push_event(kind, text):
    EVENTS.appendleft({"ts": time.time(), "kind": kind, "text": text})


# --------------------------------------------------------------------------
# 运动分析：本地规则"AI"
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


def analyze(now, x, y, z):
    """更新滑动窗口并返回 (活动标签, 事件列表)。"""
    events = []
    SAMPLES.append((now, x, y, z))
    win = [(t, ax, ay, az) for (t, ax, ay, az) in SAMPLES if now - t <= 8.0]
    if not win:
        return "等待数据…", events

    mags = [math.sqrt(ax * ax + ay * ay + az * az) for (_, ax, ay, az) in win]
    mean = sum(mags) / len(mags)
    var = sum((m - mean) ** 2 for m in mags) / len(mags)
    std = math.sqrt(var)
    peak = max(mags)

    # 计步：对模长序列做简单峰值检测（高于均值+0.25g，间隔≥0.3s）
    steps = 0
    last_peak_t = 0.0
    for (t, ax, ay, az) in win:
        m = math.sqrt(ax * ax + ay * ay + az * az)
        if m > mean + 0.25 and m > last_peak_t + 0.3:
            steps += 1
            last_peak_t = t
    STATE["step_count"] = steps
    if std > 0.28:
        STATE["shake_count"] += 1
        if time.time() - STATE.get("last_shake_evt", 0) > 3:
            STATE["last_shake_evt"] = time.time()
            events.append(("shake", "检测到晃动/敲击"))

    if min(mags[-3:] if len(mags) >= 3 else mags) < 0.35:
        activity = "疑似跌落(失重)!"
    elif std > 0.28:
        activity = "剧烈晃动"
    elif std > 0.10 or steps >= 2:
        activity = f"运动/步行 (峰值 {peak:.1f}g, 约{steps}步)"
    else:
        d = tilt_direction(x, y)
        activity = f"静置·向{d}倾斜" if d else "静置·水平"
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
        f"你好！我是表盘开发板的小助手。当前传感器({src})读数 "
        f"X={x:+.2f}g Y={y:+.2f}g Z={z:+.2f}g，合加速度 {g_mag:.2f}g，"
        f"正在进行的动作是「{act}」。最近 8 秒内约计步 {steps} 次、晃动 {shakes} 次。"
    )
    hint = tilt_direction(x, y)
    if act.startswith("静置") and hint:
        base += f" 你现在把板子向{hint}侧倾斜。"
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
            {"role": "system", "content": "你是嵌入式课堂助手，用简洁友好的中文回答。"},
            {"role": "user", "content": f"开发板传感器情况：{ai_summary()}\n同学想问：{question}"},
        ],
        "max_tokens": 200,
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
        push_event("warn", f"LLM 调用失败({exc.__class__.__name__})，已用本地AI回复")
        return None


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "RW1/1.0"

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
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
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
                snap["samples"] = [[round(t, 2), x, y, z] for (t, x, y, z) in SAMPLES]
                snap["events"] = list(EVENTS)
            self._send(200, json.dumps(snap, ensure_ascii=False))
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
        try:
            x, y, z = float(msg["x"]), float(msg["y"]), float(msg["z"])
        except (KeyError, TypeError, ValueError):
            self._send(400, json.dumps({"ok": False, "error": "bad payload"}))
            return
        now = time.time()
        with LOCK:
            was_online = STATE["device_online"]
            STATE["device_online"] = True
            STATE["last_post"] = now
            STATE["source"] = str(msg.get("source", "-"))
            STATE["latest"] = (now, x, y, z)
            if not was_online:
                push_event("info", "开发板已连接 ✔")
            activity, events = analyze(now, x, y, z)
            STATE["activity"] = activity
            for kind, text in events:
                push_event(kind, text)
            push_event("data", f"{activity}  x={x:+.2f} y={y:+.2f} z={z:+.2f}")
            last_reply = STATE["ai_reply"]

        reply = ""
        if msg.get("ask"):                  # 板子 BOOT 键 → 请求一次 AI 交互
            # 注意：必须在 LOCK 之外调用 ai_summary()（内部会再取 LOCK）。
            question = str(msg.get("q", "我现在的状态怎么样？"))
            reply = ask_llm(question) or ai_summary()
            with LOCK:
                STATE["ai_reply"] = reply
                STATE["ai_mode"] = "大模型" if os.environ.get("RW1_LLM_API_KEY") else "规则AI"
                push_event("ask", f"板端提问「{question}」→ {reply[:40]}…")

        out = {"ok": True, "activity": activity, "reply": reply or last_reply}
        self._send(200, json.dumps(out, ensure_ascii=False))

    # ---- SSE (服务器 → 浏览器) ---------------------------------------------
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
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
.grid{display:grid;grid-template-columns:240px 1fr;gap:14px;max-width:960px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px}
.on{background:#1f6feb33;color:#79c0ff}.off{background:#f8514933;color:#f85149}
canvas{width:100%;height:200px;display:block}
.row{display:flex;gap:10px;align-items:baseline;margin-top:8px}
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
</style>
</head>
<body>
<h1>AI交互课 · 第1周 · 开发板 ⇄ 电脑服务器</h1>
<div class="sub">ESP32-S3-EYE（板载IMU）→ HTTP → 本机Python服务器 → 规则/大模型AI分析 → 回传开发板显示 + 本页实时推送</div>
<div class="grid">
  <div class="card">
    <div class="row"><span class="lbl">设备状态</span><span id="dev" class="badge off">离线</span>
      <span id="src" class="lbl"></span></div>
    <div class="row"><span class="lbl">当前活动</span></div>
    <div class="big" id="act">…</div>
    <div class="row"><span class="lbl">AI来源</span><span id="mode" class="lbl"></span>
      <span class="lbl">步数</span><span id="steps">0</span></div>
    <div class="gbars">
      <div class="gbar"><span class="lbl">X</span><i><b id="bx"></b></i><span id="vx"></span></div>
      <div class="gbar"><span class="lbl">Y</span><i><b id="by"></b></i><span id="vy"></span></div>
      <div class="gbar"><span class="lbl">Z</span><i><b id="bz"></b></i><span id="vz"></span></div>
    </div>
    <div id="reply">按板子 BOOT 键即可向服务器AI提问。</div>
  </div>
  <div class="card">
    <canvas id="cv"></canvas>
    <div class="lbl" style="margin-top:6px">上=倾斜示意图（球随重力滚动）；下方为最近8秒 |a| 曲线</div>
  </div>
</div>
<div class="card" style="max-width:960px;margin-top:14px">
  <span class="lbl">事件流</span>
  <div id="feed"></div>
</div>
<footer>服务器: <span id="host"></span> · 页面仅用标准库SSE推送，无需外网</footer>
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
  document.getElementById('mode').textContent=s.ai_mode+' (LLM可选)';
  document.getElementById('steps').textContent=s.step_count;
  if(s.latest){bar('bx',s.latest[1],'vx');bar('by',s.latest[2],'vy');bar('bz',s.latest[3],'vz')}
  if(s.ai_reply)document.getElementById('reply').textContent='AI：'+s.ai_reply;
  const f=document.getElementById('feed');
  if(s.events.length){const h=s.events.slice(0,30).map(ev=>
    `<div><span class="t">${new Date(ev.ts*1000).toLocaleTimeString()}</span>${ev.kind} · ${ev.text}</div>`).join('');
   if(h!==evts){evts=h;f.innerHTML=h}}
  draw(s)};
 src.onerror=()=>{src.close();setTimeout(es,2000)};
}
// SSE 里不带 samples 全量，拉一次用于曲线
fetch('/api/latest').then(r=>r.json()).then(s=>{last=s;draw(s)});
setInterval(()=>{if(last){last.samples=(last.samples||[]);fetch('/api/latest').then(r=>r.json()).then(s=>{s.ai_reply=last.ai_reply;last=s;draw(s)})}},2000);
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
    args = ap.parse_args()

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
    print("=" * 62)
    print(" AI交互课 第1周 · PC 服务器已启动")
    print(f"   仪表盘:  http://localhost:{args.port}/")
    print(f"   遥测:    POST http://<本机IP>:{args.port}/api/telemetry")
    print(f"   AI模式:  {llm}")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()

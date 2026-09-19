#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
渲染开发板屏幕（240x240）的预览图，用于在没有硬件的情况下验证界面美观度与布局正确性。

**单一事实来源**：本脚本不重新定义任何尺寸或颜色，而是**解析 `device/main/ui.c` 里的
`UI_*` 宏**（几何）与 `UI_C_*` 宏（配色），连活动词的语义色都从 `ACT_COLOR` / `ACT_WORD`
两个数组里抠出来。所以改 ui.c 的布局常量后重跑本脚本，预览图会自动跟着变——
不会出现「代码改了、预览图还是旧的」这种验证失真。

另外这里把 `reply_page()` 的分页算法**照搬**了一份（同样 30 列、中文记 2 列），
所以预览图上的 AI 回复就是板子上真正会显示的那一页。

用法：
    python tools/ui_preview.py                 # 输出到 docs/ui-preview/
    python tools/ui_preview.py --out D:\\tmp    # 换输出目录
    python tools/ui_preview.py --scale 3        # 拼接图放大倍数（单屏图始终 1:1）

需要 Pillow（`pip install pillow`）。源字体默认取本机 SimHei，与 tools/gen_font.py 一致；
可用 RW1_FONT 环境变量覆盖。

输出：
    docs/ui-preview/screen-<状态>.png   每张 240x240，与板端 1:1
    docs/ui-preview/contact-sheet.png   全部状态 + 改造前后对比（默认 2 倍放大）
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI_C_PATH = os.path.join(ROOT, "device", "main", "ui.c")

FONT_CANDIDATES = [
    os.environ.get("RW1_FONT") or "",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
NUM_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


# ==========================================================================
# 1. 从 ui.c 解析布局常量（唯一事实来源）
# ==========================================================================
def _strip_comment(value: str) -> str:
    return value.split("/*")[0].strip()


def _c_to_py(expr: str) -> str:
    """把 C 表达式里 Python 不认的写法去掉：2.0f 的 f 后缀、((float)x) 转型。

    注意 f 后缀只在**带小数点的十进制**上剥，避免把 0x1F 这类十六进制弄坏。
    """
    expr = re.sub(r"(\d+\.\d+)[fF](?![0-9A-Za-z_])", r"\1", expr)
    expr = re.sub(r"\((?:float|double|int|unsigned|uint\d+_t|int\d+_t)\)", "", expr)
    return expr


def parse_constants(path: str) -> dict:
    """抠出所有 `#define UI_*`，按依赖顺序展开成具体数值。

    支持 `#define UI_STATUS_X UI_MARGIN`、`(UI_SCR_W - 2 * UI_MARGIN)` 这类引用。
    """
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    raw: dict[str, str] = {}
    for m in re.finditer(r"^#define\s+(UI_[A-Z0-9_]+)\s+([^\\\n]+)$", src, re.M):
        raw[m.group(1)] = _strip_comment(m.group(2))
    if not raw:
        raise SystemExit("在 %s 里没找到 UI_* 宏，解析规则要跟着 ui.c 一起改" % path)

    env: dict[str, object] = {}
    pending = dict(raw)
    for _ in range(len(pending) + 2):
        if not pending:
            break
        for name in list(pending):
            expr = pending[name]
            # 先把已解析的宏替换成字面量，剩下的标识符说明还没轮到它
            replaced = re.sub(r"\bUI_[A-Z0-9_]+\b",
                              lambda mm: repr(env[mm.group(0)]) if mm.group(0) in env else mm.group(0),
                              expr)
            if re.search(r"\bUI_[A-Z0-9_]+\b", replaced):
                continue
            try:
                val = eval(_c_to_py(replaced), {"__builtins__": {}}, {})   # noqa: S307 受控表达式
            except Exception:
                continue
            env[name] = val
            del pending[name]
    if pending:
        raise SystemExit("这些宏没法展开（是不是引用了未定义的宏？）：%s" % sorted(pending))
    return env


def parse_activity_table(path: str, env: dict) -> tuple[list[int], list[str]]:
    """抠出 ACT_COLOR / ACT_WORD 两个数组，保证预览图的活动语义色和板端一致。"""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    m = re.search(r"ACT_COLOR\[ACT_COUNT\]\s*=\s*\{([^}]*)\}", src)
    colors = [env[n.strip()] for n in m.group(1).split(",") if n.strip()] if m else []
    m = re.search(r'ACT_WORD\[ACT_COUNT\]\s*=\s*\{([^}]*)\}', src)
    words = re.findall(r'"([^"]*)"', m.group(1)) if m else []
    if not colors or not words:
        raise SystemExit("解析 ACT_COLOR / ACT_WORD 失败")
    return colors, words


# ==========================================================================
# 2. 绘制工具（模拟 LVGL 的控件语义）
# ==========================================================================
def rgb(v: int) -> tuple[int, int, int]:
    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


def box(draw: ImageDraw.ImageDraw, x, y, w, h, fill=None, outline=None,
        radius=0, width=1, alpha=255):
    """LVGL 的 lv_obj_create 语义：圆角矩形 + 可选 1px 描边。"""
    w, h = int(round(w)), int(round(h))
    if w < 1 or h < 1:
        return
    xy = [x, y, x + w - 1, y + h - 1]
    if radius >= min(w, h) // 2:
        draw.ellipse(xy, fill=fill, outline=outline, width=width)
        return
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def circle(draw, cx, cy, r, fill=None, outline=None, width=1):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=outline, width=width)


def arc(draw, cx, cy, r, start_deg, end_deg, color, width):
    """PIL 与 LVGL 的角度定义一致：0° 在 3 点钟方向，顺时针增大。"""
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=start_deg, end=end_deg,
             fill=color, width=width)


def text(draw, x, y, w, h, s, font, color, align="left", line_gap=0):
    """在 (x,y,w,h) 的框里画文字；垂直居中，水平按 align。返回占用的行数。"""
    lines = s.split("\n")
    lh = font.size + line_gap
    total = lh * len(lines)
    ty = y + max(0, (h - total) // 2)
    for line in lines:
        tw = draw.textlength(line, font=font)
        if align == "center":
            tx = x + (w - tw) / 2
        elif align == "right":
            tx = x + w - tw
        else:
            tx = x
        draw.text((tx, ty), line, font=font, fill=color)
        ty += lh
    return len(lines)


def fit_text(draw, s, font, max_w, max_lines=1, dot=True):
    """模拟 LV_LABEL_LONG_DOT：按宽度断行，超出就截断并补 …。"""
    lines: list[str] = []
    cur = ""
    for ch in s:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
        else:
            cur += ch
    if len(lines) < max_lines and cur:
        lines.append(cur)

    full_w = draw.textlength(s.replace("\n", ""), font=font)
    if dot and len(lines) >= max_lines:
        joined = "".join(lines)
        if len(joined) < len(s.replace("\n", "")) or full_w > max_w * max_lines:
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_w:
                last = last[:-1]
            lines[-1] = last + "…"
    return "\n".join(lines)


# 与 ui.c 的 reply_page() 同一套算法（30 列、中文 2 列、优先在空格处断）
def reply_page(src: str, page: int, cols: int) -> tuple[str, int]:
    out, total, p = "", 0, 0
    while p < len(src):
        start, brk, c = p, None, 0
        while p < len(src) and c < cols:
            ch = src[p]
            w = 1 if ord(ch) < 0x80 else 2
            if c + w > cols:
                break
            c += w
            p += 1
            if ord(ch) < 0x80 and ch in " ,.;":
                brk = p
        if brk is not None and brk > start + 10 and p < len(src):
            p = brk
        if p == start:
            p += 1
        if total == page:
            out = src[start:p]
        total += 1
    return out, max(total, 1)


# ==========================================================================
# 3. 渲染一屏
# ==========================================================================
class Renderer:
    def __init__(self, env, colors, words):
        self.e = env
        self.act_color = [rgb(c) for c in colors]
        self.act_word = words
        self.cjk = {n: self._font(FONT_CANDIDATES, n) for n in (18, 14, 12)}
        self.num = {n: self._font(NUM_FONT_CANDIDATES, n) for n in (14, 12)}
        self.C = {k.replace("UI_C_", ""): rgb(v)
                  for k, v in env.items() if k.startswith("UI_C_")}

    @staticmethod
    def _font(cands, size):
        for c in cands:
            if c and os.path.isfile(c):
                try:
                    return ImageFont.truetype(c, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _measure(s: str, font) -> float:
        """量一段文字的像素宽度（自检用）。"""
        return ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(s, font=font)

    # ---- 背景 ----
    def _bg(self):
        e = self.e
        img = Image.new("RGB", (e["UI_SCR_W"], e["UI_SCR_H"]), self.C["BG"])
        top, bot = self.C["BG"], self.C["BG_DEEP"]
        d = ImageDraw.Draw(img)
        for y in range(e["UI_SCR_H"]):
            t = y / max(1, e["UI_SCR_H"] - 1)
            d.line([(0, y), (e["UI_SCR_W"], y)],
                   fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
        return img

    def _classify(self, activity):
        for i, w in enumerate(self.act_word):
            if w != "等待" and w in activity:
                return i
        return 0

    # ---- 主渲染 ----
    def render(self, st) -> Image.Image:
        e, C = self.e, self.C
        img = self._bg()
        d = ImageDraw.Draw(img)

        kind = self._classify(st["activity"])
        act_c = self.act_color[kind]

        # ---------------- 状态胶囊 ----------------
        box(d, e["UI_STATUS_X"], e["UI_STATUS_Y"], e["UI_STATUS_W"], e["UI_STATUS_H"],
            fill=C["CARD"], outline=C["LINE"], radius=e["UI_STATUS_H"] // 2)
        if not st["wifi"]:
            link, lc = "连WiFi", C["AMBER"]
        elif st["online"]:
            link, lc = "在线", C["GREEN"]
        else:
            link, lc = "无服务", C["RED"]

        r = e["UI_DOT_D"] // 2
        circle(d, e["UI_DOT_X"] + r, e["UI_STATUS_Y"] + e["UI_STATUS_H"] // 2, r, fill=lc)

        sy = e["UI_STATUS_Y"]
        text(d, e["UI_LINK_X"], sy + (e["UI_STATUS_H"] - 14) // 2, e["UI_LINK_W"], 14,
             link, self.cjk[12], lc, "left")
        text(d, e["UI_HZ_X"], sy + (e["UI_STATUS_H"] - 14) // 2, e["UI_HZ_W"], 14,
             "%dHz" % st["hz"], self.num[14], C["DIM"], "center")
        text(d, e["UI_POSTS_X"], sy + (e["UI_STATUS_H"] - 14) // 2, e["UI_POSTS_W"], 14,
             "↑%d" % st["posts"], self.num[14], C["DIM"], "center")

        if st["cmd"] == 0:
            text(d, e["UI_INFO_X"], sy + (e["UI_STATUS_H"] - 12) // 2, e["UI_INFO_W"], 12,
                 "%s o%d" % (st["source"], st["orient"]), self.cjk[12], C["FAINT"], "right")
        else:
            bx = e["UI_BADGE_X"]
            by = sy + (e["UI_STATUS_H"] - e["UI_BADGE_H"]) // 2
            bc = [C["AMBER"], C["AMBER"], C["GREEN"], C["RED"]][st["cmd"]]
            bt = ["", "···", "OK", "NG"][st["cmd"]]
            box(d, bx, by, e["UI_BADGE_W"], e["UI_BADGE_H"], fill=bc,
                radius=e["UI_BADGE_H"] // 2)
            text(d, bx, by, e["UI_BADGE_W"], e["UI_BADGE_H"], bt, self.num[14],
                 (10, 14, 20), "center")

        # ---------------- 左面板：活动环 ----------------
        px, py = e["UI_PANEL_LX"], e["UI_PANEL_Y"]
        box(d, px, py, e["UI_PANEL_W"], e["UI_PANEL_H"], fill=C["CARD"],
            outline=C["LINE"], radius=16)

        if kind == 5:      # 跌落：环外呼吸红光（预览图取呼吸中段的不透明度）
            gd = e["UI_RING_D"] + 14
            gx = px + (e["UI_PANEL_W"] - gd) // 2
            gy = py + e["UI_RING_Y"] - 7
            ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(ov).ellipse([gx, gy, gx + gd - 1, gy + gd - 1],
                                       outline=act_c + (150,), width=2)
            img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"), (0, 0))
            d = ImageDraw.Draw(img)

        rr = e["UI_RING_D"] // 2
        cx = px + e["UI_PANEL_W"] // 2
        cy = py + e["UI_RING_Y"] + rr
        arc(d, cx, cy, rr - e["UI_RING_W"] // 2, 135, 405, C["TRACK"], e["UI_RING_W"])
        sweep = max(0.0, min(1.0, st["mag"] / e["UI_ABS_FULL_G"]))
        if sweep > 0.005:
            arc(d, cx, cy, rr - e["UI_RING_W"] // 2, 135, 135 + 270 * sweep,
                act_c, e["UI_RING_W"])

        text(d, px + (e["UI_PANEL_W"] - e["UI_WORD_W"]) // 2, py + e["UI_WORD_Y"],
             e["UI_WORD_W"], e["UI_WORD_H"], self.act_word[kind], self.cjk[18],
             act_c, "center")
        text(d, px + (e["UI_PANEL_W"] - e["UI_ABS_W"]) // 2, py + e["UI_ABS_Y"],
             e["UI_ABS_W"], e["UI_ABS_H"], "%.2fg" % st["mag"], self.num[14],
             C["DIM"], "center")
        text(d, px + (e["UI_PANEL_W"] - e["UI_PANEL_CAP_W"]) // 2, py + e["UI_PANEL_CAP_Y"],
             e["UI_PANEL_CAP_W"], e["UI_PANEL_CAP_H"], st["detail"], self.cjk[12],
             C["DIM"], "center")

        # ---------------- 右面板：姿态球 ----------------
        px = e["UI_PANEL_RX"]
        box(d, px, py, e["UI_PANEL_W"], e["UI_PANEL_H"], fill=C["CARD"],
            outline=C["LINE"], radius=16)

        bd = e["UI_BALL_D"]
        bx0 = px + (e["UI_PANEL_W"] - bd) // 2
        by0 = py + e["UI_BALL_Y"]
        box(d, bx0, by0, bd, bd, fill=C["BG"], outline=C["LINE"], radius=bd // 2)
        bcx, bcy = bx0 + bd // 2, by0 + bd // 2
        half = e["UI_CROSS_LEN"] // 2
        d.line([(bcx, bcy - half), (bcx, bcy + half)], fill=C["LINE"])
        d.line([(bcx - half, bcy), (bcx + half, bcy)], fill=C["LINE"])
        circle(d, bcx, bcy, e["UI_LEVEL_D"] // 2, outline=C["TRACK"])

        ox = int(max(-1.0, min(1.0, st["x"])) * e["UI_BUBBLE_MAX"])
        oy = int(max(-1.0, min(1.0, st["y"])) * e["UI_BUBBLE_MAX"])
        horiz = (st["x"] ** 2 + st["y"] ** 2) ** 0.5
        if st["mag"] < 0.05:          # 没有有效读数（开机/断链），不是失重
            ball_c = C["FAINT"]
        elif st["mag"] < 0.35:
            ball_c = C["RED"]
        elif horiz < 0.15:
            ball_c = C["GREEN"]
        elif horiz < 0.7:
            ball_c = C["AMBER"]
        else:
            ball_c = C["RED"]
        br = e["UI_BUBBLE_D"] // 2
        circle(d, bcx + ox, bcy + oy, br + 4, fill=tuple(int(c * 0.18) for c in ball_c))
        circle(d, bcx + ox, bcy + oy, br, fill=ball_c)

        if st["mag"] < 0.05:
            tilt = "无读数"
        elif st["mag"] < 0.35:
            tilt = "失重"
        else:
            import math
            tilt = "倾角 %d°" % round(math.degrees(math.acos(min(1.0, abs(st["z"]) / st["mag"]))))
        text(d, px + (e["UI_PANEL_W"] - e["UI_PANEL_CAP_W"]) // 2, py + e["UI_PANEL_CAP_Y"],
             e["UI_PANEL_CAP_W"], e["UI_PANEL_CAP_H"], tilt, self.cjk[12], C["DIM"], "center")

        # ---------------- 三轴对称条 ----------------
        for i, (name, val) in enumerate(zip("XYZ", (st["x"], st["y"], st["z"]))):
            ry = e["UI_AXIS_Y"] + i * (e["UI_AXIS_ROW_H"] + e["UI_AXIS_GAP"])
            ac = [C["AXIS_X"], C["AXIS_Y"], C["AXIS_Z"]][i]
            text(d, e["UI_AXIS_LBL_X"], ry, e["UI_AXIS_LBL_W"], e["UI_AXIS_ROW_H"],
                 name, self.num[14], ac, "left")
            byy = ry + (e["UI_AXIS_ROW_H"] - e["UI_AXIS_BAR_H"]) // 2
            box(d, e["UI_AXIS_BAR_X"], byy, e["UI_AXIS_BAR_W"], e["UI_AXIS_BAR_H"],
                fill=C["TRACK"], radius=e["UI_AXIS_BAR_H"] // 2)
            frac = max(-1.0, min(1.0, val / 2.0))
            if abs(frac) > 0.02:
                half_w = e["UI_AXIS_BAR_W"] / 2 * abs(frac)
                x0 = e["UI_AXIS_BAR_X"] + (e["UI_AXIS_BAR_W"] / 2 if frac >= 0
                                           else e["UI_AXIS_BAR_W"] / 2 - half_w)
                box(d, x0, byy, half_w, e["UI_AXIS_BAR_H"], fill=ac,
                    radius=e["UI_AXIS_BAR_H"] // 2)
            text(d, e["UI_AXIS_VAL_X"], ry, e["UI_AXIS_VAL_W"], e["UI_AXIS_ROW_H"],
                 "%+.2f" % val, self.num[14], C["DIM"], "right")

        # ---------------- AI 回复卡片 ----------------
        cy0 = e["UI_CARD_Y"]
        box(d, e["UI_CARD_X"], cy0, e["UI_CARD_W"], e["UI_CARD_H"], fill=C["CARD"],
            outline=C["LINE"], radius=14)
        # 卡片顶部那道渐亮，用几行淡色叠出来
        for i in range(14):
            a = int(14 * (1 - i / 14.0))
            d.line([(e["UI_CARD_X"] + 1, cy0 + 1 + i),
                    (e["UI_CARD_X"] + e["UI_CARD_W"] - 2, cy0 + 1 + i)],
                   fill=tuple(min(255, C["CARD"][k] + a) for k in range(3)))

        tag_bg = tuple(int(C["CARD"][k] + (C["BLUE"][k] - C["CARD"][k]) * 48 / 255)
                       for k in range(3))
        box(d, e["UI_TAG_X"], cy0 + e["UI_TAG_Y"], e["UI_TAG_W"], e["UI_TAG_H"],
            fill=tag_bg, radius=7)
        text(d, e["UI_TAG_X"], cy0 + e["UI_TAG_Y"], e["UI_TAG_W"], e["UI_TAG_H"],
             "AI", self.num[14], C["BLUE"], "center")

        if st["pending"]:
            sr = e["UI_SPIN_D"] // 2 - 1
            scx = e["UI_SPIN_X"] + e["UI_SPIN_D"] // 2
            scy = cy0 + e["UI_SPIN_Y"] + e["UI_SPIN_D"] // 2
            arc(d, scx, scy, sr, 0, 360, C["TRACK"], 2)
            arc(d, scx, scy, sr, -60, 60, C["AMBER"], 2)
        else:
            page_txt, total = reply_page(st["reply"], 0, e["UI_REPLY_COLS"])
            if total > 1:
                text(d, e["UI_PAGE_X"], cy0 + e["UI_TAG_Y"], e["UI_PAGE_W"], e["UI_TAG_H"],
                     "1/%d" % total, self.num[14], C["FAINT"], "right")

        body = st["reply"] if st["reply"] else "按 BOOT 键向电脑服务器的 AI 提问"
        page_txt, _ = reply_page(body, 0, e["UI_REPLY_COLS"])
        shown = fit_text(d, page_txt, self.cjk[14], e["UI_REPLY_W"], max_lines=2)
        text(d, e["UI_CARD_X"] + e["UI_REPLY_X"], cy0 + e["UI_REPLY_Y"],
             e["UI_REPLY_W"], e["UI_REPLY_H"], shown, self.cjk[14],
             C["DIM"] if st["pending"] else C["AMBER"], "left", line_gap=5)

        return img


def render_old(env, r: Renderer, st) -> Image.Image:
    """改造前的界面：四个居中纯文本 label。用来做前后对比。"""
    e = env
    img = Image.new("RGB", (e["UI_SCR_W"], e["UI_SCR_H"]), (0x0D, 0x11, 0x17))
    d = ImageDraw.Draw(img)
    w = e["UI_SCR_W"]
    text(d, 6, 4, w - 12, 16, "AI交互课 · 第1周", r.cjk[14], (0x8B, 0x94, 0x9E), "center")
    text(d, 6, 26, w - 12, 20, "WiFi在线 · 服务器:OK · 100Hz", r.cjk[14],
         (0x7E, 0xE7, 0x87), "center")
    act = st["activity"] if st["activity"] else "等待服务器…"
    text(d, 6, 50, w - 12, 42,
         fit_text(d, act, r.cjk[14], w - 12, 2), r.cjk[14], (0x7E, 0xE7, 0x87), "center")
    text(d, 6, 96, w - 12, 42,
         "SC7A20  o0  %d/批\nX%+.2f Y%+.2f Z%+.2f" % (st["posts"] % 50, st["x"], st["y"], st["z"]),
         r.cjk[14], (0x79, 0xC0, 0xFF), "center", line_gap=5)
    text(d, 6, 142, w - 12, 90,
         fit_text(d, st["reply"] or "按 BOOT 键向电脑服务器的AI提问", r.cjk[14], w - 12, 4),
         r.cjk[14], (0xE3, 0xB3, 0x41), "center", line_gap=5)
    return img


# ==========================================================================
# 4. 状态样本
# ==========================================================================
def states() -> list[dict]:
    base = dict(hz=100, posts=1234, orient=0, source="SC7A20", cmd=0,
                wifi=True, online=True, pending=False)
    return [
        dict(base, slug="01-idle", title="静置 · 水平",
             activity="静置·水平", detail="水平",
             x=0.02, y=0.01, z=1.00, mag=1.00,
             reply="正在进行的动作是「静置·水平」。最近 8 秒内约计步 0 次、晃动 0 次。 "
                   "板子基本放平了，试试把它向某个方向倾斜吧。"),
        dict(base, slug="02-tilt", title="静置 · 向左倾斜",
             activity="静置·向左倾斜", detail="向左倾斜",
             x=-0.48, y=0.06, z=0.87, mag=0.99,
             reply="正在进行的动作是「静置·向左倾斜」。你现在把板子向左侧倾斜。"),
        dict(base, slug="03-walk", title="运动 / 步行",
             activity="运动/步行 (峰值 1.2g, 约8步)", detail="约 8 步",
             x=0.22, y=-0.35, z=1.02, mag=1.13,
             reply="正在进行的动作是「运动/步行 (峰值 1.2g, 约8步)」。最近 8 秒内约计步 8 次、"
                   "晃动 0 次。起伏频率 2.1Hz，是走路的节奏，不是晃动。"),
        dict(base, slug="04-shake", title="剧烈晃动",
             activity="剧烈晃动", detail="剧烈晃动",
             x=0.71, y=0.44, z=0.86, mag=1.36,
             reply="正在进行的动作是「剧烈晃动」。起伏频率 5.2Hz，属于晃动而不是步行。"),
        dict(base, slug="05-fall", title="疑似跌落（呼吸告警 + 远端闪灯）",
             activity="疑似跌落(失重)!", detail="失重告警",
             x=0.05, y=0.08, z=0.21, mag=0.23, cmd=2,
             reply="正在进行的动作是「疑似跌落(失重)!」。检测到连续失重 120ms，"
                   "已自动下发 led_blink 做物理告警。"),
        dict(base, slug="06-cmd", title="远程指令执行中 + AI 生成中",
             activity="静置·水平", detail="水平",
             x=0.03, y=0.02, z=1.00, mag=1.00, cmd=1, pending=True, posts=1240,
             reply="正在思考…"),
        dict(base, slug="07-offline", title="断链（服务器无响应）",
             activity="", detail="暂无数据",
             x=0.0, y=0.0, z=0.0, mag=0.00, online=False, wifi=True, posts=1234,
             reply=""),
    ]


# ==========================================================================
# 5. 输出
# ==========================================================================
def selfcheck(env, r: "Renderer") -> list[str]:
    """布局自检：控件是否都在屏内、环心文字是否落在环内、AI 回复是否真放得下。

    把「看着还行」变成可复现的结论。返回问题列表（空 = 全通过）。
    """
    import math

    W, H = env["UI_SCR_W"], env["UI_SCR_H"]
    problems: list[str] = []

    def inside(name, x, y, w, h):
        if x < 0 or y < 0 or x + w > W or y + h > H:
            problems.append("%s 越界：(%d,%d,%d,%d) 超出 %dx%d" % (name, x, y, w, h, W, H))

    inside("状态胶囊", env["UI_STATUS_X"], env["UI_STATUS_Y"],
           env["UI_STATUS_W"], env["UI_STATUS_H"])
    inside("活动环面板", env["UI_PANEL_LX"], env["UI_PANEL_Y"],
           env["UI_PANEL_W"], env["UI_PANEL_H"])
    inside("姿态球面板", env["UI_PANEL_RX"], env["UI_PANEL_Y"],
           env["UI_PANEL_W"], env["UI_PANEL_H"])
    inside("回复卡片", env["UI_CARD_X"], env["UI_CARD_Y"],
           env["UI_CARD_W"], env["UI_CARD_H"])
    for i, axis in enumerate("XYZ"):
        y = env["UI_AXIS_Y"] + i * (env["UI_AXIS_ROW_H"] + env["UI_AXIS_GAP"])
        inside("轴 %s 数值" % axis, env["UI_AXIS_VAL_X"], y,
               env["UI_AXIS_VAL_W"], env["UI_AXIS_ROW_H"])
        inside("轴 %s 进度条" % axis, env["UI_AXIS_BAR_X"],
               y + (env["UI_AXIS_ROW_H"] - env["UI_AXIS_BAR_H"]) // 2,
               env["UI_AXIS_BAR_W"], env["UI_AXIS_BAR_H"])

    # 面板底部小字不能压到环/球的下沿
    ring_bottom = env["UI_PANEL_Y"] + env["UI_RING_Y"] + env["UI_RING_D"]
    cap_top = env["UI_PANEL_Y"] + env["UI_PANEL_CAP_Y"]
    if cap_top < ring_bottom - 8:
        problems.append("面板底部小字 (y=%d) 离环太近，环下沿 y=%d" % (cap_top, ring_bottom))

    # 环心文字必须落在环内：按该行中心算圆内的可用弦长
    r_in = env["UI_RING_D"] / 2 - env["UI_RING_W"]
    cy = env["UI_RING_Y"] + env["UI_RING_D"] / 2
    for name, y, h, box_w, font, sample in (
            ("环心活动词", env["UI_WORD_Y"], env["UI_WORD_H"], env["UI_WORD_W"],
             r.cjk[18], "静置"),
            ("环心 |a| 读数", env["UI_ABS_Y"], env["UI_ABS_H"], env["UI_ABS_W"],
             r.num[14], "0.00g")):
        ly = y + h / 2 - cy
        chord = 2 * math.sqrt(max(0.0, r_in ** 2 - ly ** 2))
        need = max(box_w, r._measure(sample, font))
        if need > chord:
            problems.append("%s 需要 %dpx，但该高度处环内只有 %.0fpx" % (name, need, chord))

    # AI 回复：每一页都必须放得下（宽度 ≤ 回复区，行数 ≤ 2）
    probe = Image.new("RGB", (8, 8))
    pd = ImageDraw.Draw(probe)
    for st in states():
        body = st["reply"] if st["reply"] else "按 BOOT 键向电脑服务器的 AI 提问"
        page, total = reply_page(body, 0, env["UI_REPLY_COLS"])
        shown = fit_text(pd, page, r.cjk[14], env["UI_REPLY_W"], max_lines=2)
        lines = shown.split("\n")
        if len(lines) > 2:
            problems.append("%s：回复占了 %d 行（上限 2）" % (st["slug"], len(lines)))
        for ln in lines:
            wid = pd.textlength(ln, font=r.cjk[14])
            if wid > env["UI_REPLY_W"]:
                problems.append("%s：回复行宽 %.0fpx 超过回复区 %dpx（“%s”）"
                                % (st["slug"], wid, env["UI_REPLY_W"], ln))
        if total < 1:
            problems.append("%s：分页数异常" % st["slug"])
    return problems


def compose(images: list[tuple[str, Image.Image]], scale: int, old: Image.Image) -> Image.Image:
    pad, cap = 22, 34
    cols = 4
    rows = (len(images) + cols - 1) // cols
    w = images[0][1].width
    h = images[0][1].height
    sheet = Image.new("RGB", (pad + cols * (w * scale + pad), pad + rows * (h * scale + cap + pad)),
                      (0x0A, 0x0E, 0x14))
    d = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(FONT_CANDIDATES[1], 15) if os.path.isfile(FONT_CANDIDATES[1]) \
        else ImageFont.load_default()

    items = [("改造前（纯文本）", old)] + images
    for i, (title, im) in enumerate(items):
        r, c = divmod(i, cols)
        x = pad + c * (w * scale + pad)
        y = pad + r * (h * scale + cap + pad)
        d.text((x, y), title, font=f, fill=(0xE6, 0xED, 0xF3))
        sheet.paste(im.resize((w * scale, h * scale), Image.NEAREST), (x, y + cap - 8))
        d.rectangle([x - 1, y + cap - 9, x + w * scale, y + cap - 9 + h * scale],
                    outline=(0x25, 0x2D, 0x3A))
    return sheet


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="渲染开发板 240x240 界面预览图")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "ui-preview"))
    ap.add_argument("--scale", type=int, default=2, help="拼接图放大倍数")
    args = ap.parse_args()

    env = parse_constants(UI_C_PATH)
    colors, words = parse_activity_table(UI_C_PATH, env)
    r = Renderer(env, colors, words)

    print("解析 ui.c：%d 个 UI_* 宏，活动词 %s" % (len(env), "/".join(words)))
    problems = selfcheck(env, r)
    if problems:
        print("\n布局自检：%d 个问题" % len(problems))
        for p in problems:
            print("  ! " + p)
    else:
        print("布局自检：全部通过（控件都在屏内；环心文字落在环内；"
              "AI 回复每页 ≤ 2 行且不超宽）")

    os.makedirs(args.out, exist_ok=True)
    shots: list[tuple[str, Image.Image]] = []
    for st in states():
        im = r.render(st)
        path = os.path.join(args.out, "screen-%s.png" % st["slug"])
        im.save(path, optimize=True)
        shots.append((st["title"], im))
        print("已写出 %s" % path)

    old = render_old(env, r, states()[2])
    sheet_path = os.path.join(args.out, "contact-sheet.png")
    compose(shots, args.scale, old).save(sheet_path, optimize=True)
    print("已写出 %s（%d 个状态 + 改造前对比，%d 倍放大）"
          % (sheet_path, len(shots), args.scale))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

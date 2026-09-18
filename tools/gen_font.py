#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 LVGL tiny_ttf 用的中文字体子集 rw1_font.c。

背景：LVGL 自带的 source_han_sans_sc_16_cjk 是“演示字体”，只包含一小撮
随机字符（见其生成注释），绝大多数常用汉字缺字 → 屏幕上显示成方块/乱码。
本脚本从本机的一个中文字体裁一个只含“项目实际用到的字 + 常用字”的子集 TTF，
编译进固件，由 LVGL 的 tiny_ttf 运行时渲染，缺字问题即消失。

字集来源（自动扫描）：
  - ../server/server.py 里出现的非 ASCII 字符（AI 回复模板/活动标签）
  - ../device/main/*.c|*.h 里出现的非 ASCII 字符（界面文案）
  - 外加一批常用汉字 + 中文标点，给后续文案微调留余量。

用法：
    python gen_font.py                      # 自动找本机中文字体
    python gen_font.py --font D:\\f.ttf      # 指定字体（也认 RW1_FONT 环境变量）
    python gen_font.py --list-fonts          # 只列出探测到的候选字体
改完界面/服务器文案后重跑一次，再编译即可。

输出文件 device/main/rw1_font.{c,h} 已被 .gitignore 排除（系统字体版权原因，
详见 NOTICE）——每个使用者在自己机器上跑本脚本生成。

退出码：0 正常；1 找不到字体或板端文案有缺字（此时固件会显示方块，别忽略）。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_FILES = (
    [os.path.join(ROOT, "server", "server.py")]
    + glob.glob(os.path.join(ROOT, "device", "main", "*.c"))
    + glob.glob(os.path.join(ROOT, "device", "main", "*.h"))
)
OUT_C = os.path.join(ROOT, "device", "main", "rw1_font.c")
OUT_H = os.path.join(ROOT, "device", "main", "rw1_font.h")

# 常见中文字体候选。Windows 的 simhei/msyh 是系统自带；另外几个是开源字体，
# 手动装上就能得到一个可以随固件一起分发的子集（见 NOTICE）。
FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

# 常用字 + 标点，给未来文案调整留余量（约 350 字，≈130KB 以内）
EXTRA = (
    "，。！？：；、（）《》“”‘’·—…％℃"
    "你好我是开发板小助手当前传感器读数合加速度正在进行动作的是最近秒内约计步次"
    "摇晃试试把它某方向倾斜放平按即可向电脑服务器提问静置水平上下左右前后剧烈疑似"
    "跌落失重运动步行峰值检测到敲击状态怎么样谢谢同学老师任务完成交互课第一周"
    "显示屏幕连接失败无等待启动中数据发送接收响应请求正常异常断开通信网络地址端口"
    "超时空服务未请检查确认关闭浏览器刷新页面仪表实时推姿态曲线事件日志回答问题目"
    "需求实现流程闭环测试验证运行记录存储历史统计平均最大最断方差阈值触发判断识别"
    "分类算法模型人工能智大接口文档参数字段类型数值字符编码格式样式布局颜色字体"
    "大小写换滚按钮长按单击点击短按双击切换模式窗标签标内容文本图标箭头指示信号"
    "电源电量充电温度湿度光照距强弱快慢高低深浅宽窄厚薄远近少剩余足够完整缺少记录"
    "批次思考生成中已转交占用队列回退重试次数上限保留清理落盘波形采样率校准"
)


def collect_chars() -> set[str]:
    chars = set(EXTRA)
    for path in SRC_FILES:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for ch in text:
            if ord(ch) > 0x7F:
                chars.add(ch)
    return chars


def find_font(explicit: str | None) -> str | None:
    """按 显式指定 → 环境变量 → 候选列表 的顺序找一个能打开的字体。"""
    tried: list[str] = []
    for cand in [explicit, os.environ.get("RW1_FONT")] + FONT_CANDIDATES:
        if not cand or cand in tried:
            continue
        tried.append(cand)
        if os.path.isfile(cand):
            return cand
    return None


def load_font(path: str, opts):
    """支持 .ttc（字体集合）；集合里的第 0 个通常就是中文常规体。"""
    if path.lower().endswith((".ttc", ".otc")):
        return subset.load_font(path, opts, fontNumber=0)
    return subset.load_font(path, opts)


def main() -> int:
    try:  # 控制台可能是 GBK，缺字符直接改 UTF-8 容错输出
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="生成 LVGL 中文字体子集")
    ap.add_argument("--font", help="源字体路径（默认自动探测本机中文字体）")
    ap.add_argument("--list-fonts", action="store_true", help="只列出候选字体后退出")
    args = ap.parse_args()

    if args.list_fonts:
        for c in FONT_CANDIDATES:
            print("%s  %s" % ("[有]" if os.path.isfile(c) else "[无]", c))
        return 0

    font_in = find_font(args.font)
    if font_in is None:
        print("找不到可用的中文字体。请用 --font 指定，或设 RW1_FONT 环境变量。", file=sys.stderr)
        print("已尝试：", file=sys.stderr)
        for c in FONT_CANDIDATES:
            print("  " + c, file=sys.stderr)
        return 1
    print("源字体: %s" % font_in)

    chars = collect_chars()
    unicodes = sorted({ord(c) for c in chars} | set(range(0x20, 0x7F)))

    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    opts.recalc_bounds = True
    opts.drop_tables += ["DSIG"]
    font = load_font(font_in, opts)
    subsetter = subset.Subsetter(opts)
    subsetter.populate(unicodes=unicodes)
    subsetter.subset(font)
    tmp_ttf = os.path.join(HERE, "_rw1_font.tmp.ttf")
    font.save(tmp_ttf)

    # 覆盖率自检：板端源码里出现在字符串里的每个非 ASCII 字符都必须被子集包含；
    # 仅服务器/仪表盘文案里的装饰字符（如 ✔）缺失只作警告。
    board_chars = set()
    for path in glob.glob(os.path.join(ROOT, "device", "main", "*.c")) + \
                glob.glob(os.path.join(ROOT, "device", "main", "*.h")):
        with open(path, encoding="utf-8", errors="replace") as fh:
            # 注释里的字符可能出现在字符串外，宁可多报不漏报：只挑字符串行
            for line in fh:
                if '"' in line and not line.lstrip().startswith(("*", "//", "/*")):
                    board_chars.update(c for c in line if ord(c) > 0x7F)
    check = TTFont(tmp_ttf)
    have = set()
    for table in check["cmap"].tables:
        have.update(table.cmap.keys())
    missing = sorted(c for c in unicodes if c not in have)
    hard = [c for c in missing if chr(c) in board_chars]
    size = os.path.getsize(tmp_ttf)
    print(f"子集字符数={len(unicodes)} 体积={size/1024:.0f}KB "
          f"缺字={len(missing)}（其中板端文案缺字={len(hard)}）")
    if missing:
        print("提示 这些字不在源字体里（只影响浏览器/仪表盘）:",
              "".join(chr(c) for c in missing))
    if hard:
        print("板端会显示方块，请换一个字体或删掉这些文案:", "".join(chr(c) for c in hard),
              file=sys.stderr)

    with open(tmp_ttf, "rb") as fh:
        data = fh.read()
    os.remove(tmp_ttf)

    with open(OUT_H, "w", encoding="utf-8") as fh:
        fh.write(
            "/* Generated by tools/gen_font.py - do not edit by hand.\n"
            " * Subset covering the UI/server vocabulary, for LVGL tiny_ttf.\n"
            " * Source font: %s\n"
            " * See NOTICE before redistributing this firmware. */\n"
            % os.path.basename(font_in)
        )
        fh.write("#pragma once\n#include <stdint.h>\n\n"
                 "extern const uint8_t rw1_font_ttf[];\n"
                 "extern const unsigned int rw1_font_ttf_len;\n")
    with open(OUT_C, "w", encoding="utf-8") as fh:
        fh.write("/* Generated by tools/gen_font.py - do not edit by hand. */\n")
        fh.write('#include "rw1_font.h"\n\n')
        fh.write("const uint8_t rw1_font_ttf[] = {\n")
        for i in range(0, len(data), 20):
            fh.write(",".join(str(b) for b in data[i:i + 20]) + ",\n")
        fh.write("};\n\nconst unsigned int rw1_font_ttf_len = sizeof(rw1_font_ttf);\n")
    print(f"已写出 {OUT_C}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())

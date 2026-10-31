#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯函数单元测试（stdlib unittest，不需要装 pytest）。

为什么单独有这个文件：
    `tools/verify_server.py`（138 项）走的是**端到端**路线 —— 起真服务、发真 HTTP、
    等真线程。它验的是"整条链路对不对"，代价是 2 分半，而且**边界用例很难覆盖**：
    想验 `tilt_direction` 的 8 个方位、`parse_intent` 的 30 条话术，
    要为此发几十次真实请求。

    这些函数是纯的（无锁、无 IO、无全局状态），本来就该用毫秒级的断言覆盖。
    本文件只测它们，所以：**秒级、可进每次提交的门禁、失败信息直接指到边界条件**。

跑法：
    python -m unittest discover -s tools -p "test_*.py" -v
    # 或直接
    python tools/test_units.py

注意：导入 server.py 会执行模块级代码（定义 DASHBOARD_HTML 等），但**不会**启动服务
（`main()` 在 `if __name__ == "__main__"` 后面）。所以导入是安全的。
"""

import importlib.util
import math
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER_PY = os.path.join(os.path.dirname(_HERE), "server", "server.py")


def _load_server():
    spec = importlib.util.spec_from_file_location("rw1_server_under_test", _SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


srv = _load_server()
# 分类函数住在 `server/analysis.py`（2026-11-01 从 server.py 拆出）。
# 这里**直接拿那个模块对象**，而不是 `import analysis` 再加载一遍 ——
# server.py 在 import 时已经把它装进 sys.modules 了，所以
# `sys.modules["analysis"]` 就是服务端真正在用的那一份（同一个函数对象）。
# 顺带这也是一条断言：哪天 server.py 不再 import analysis，这里会立刻 KeyError。
ana = sys.modules["analysis"]


class TestClipUtf8(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(srv.clip_utf8("你好", 100), "你好")

    def test_never_splits_multibyte(self):
        """按字节截断，但绝不能切出半个汉字 —— 切了就是乱码（板上 LVGL 显示问号）。"""
        text = "中文" * 200
        for limit in range(1, 40):
            out = srv.clip_utf8(text, limit)
            out.encode("utf-8")                      # 解不出来会抛，等于断言
            self.assertLessEqual(len(out.encode("utf-8")), limit + len("…".encode("utf-8")))

    def test_truncated_marked(self):
        out = srv.clip_utf8("中" * 50, 30)
        self.assertTrue(out.endswith("…"))


class TestSafeName(unittest.TestCase):
    def test_keeps_alnum_and_zh(self):
        # isalnum() 对中文为真，所以中文设备名会被保留 —— 这是刻意的（目录名可读）
        self.assertEqual(srv.safe_name("第三组-07"), "第三组-07")

    def test_replaces_path_chars(self):
        self.assertEqual(srv.safe_name("a/b c\\d"), "a_b_c_d")
        self.assertNotIn("/", srv.safe_name("../../etc/passwd"))

    def test_empty_falls_back(self):
        self.assertEqual(srv.safe_name(""), "unknown")

    def test_truncation_does_not_collide(self):
        """前 41 字符相同的两个长名字必须拿到不同的目录名。

        2026-10-12 的修复：无脑截断到 48 会让「第三组-07-xxxx…」这类同前缀名字
        互相覆盖照片，而且看不出来。截断时追加 6 位短哈希。
        """
        prefix = "第三组-07-" + "x" * 40
        a = srv.safe_name(prefix + "-AAA")
        b = srv.safe_name(prefix + "-BBB")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 48)
        self.assertLessEqual(len(b), 48)


class TestTiltDirection(unittest.TestCase):
    """屏幕坐标系：+x 向右、+y 向下。8 个方位逐个钉死。"""

    def test_eight_directions(self):
        cases = {
            (1.0, 0.0): "右",
            (1.0, 1.0): "右下",
            (0.0, 1.0): "下",
            (-1.0, 1.0): "左下",
            (-1.0, 0.0): "左",
            (-1.0, -1.0): "左上",
            (0.0, -1.0): "上",
            (1.0, -1.0): "右上",
        }
        for (x, y), want in cases.items():
            self.assertEqual(ana.tilt_direction(x, y), want, "向量 (%.1f, %.1f)" % (x, y))

    def test_flat_returns_none(self):
        self.assertIsNone(ana.tilt_direction(0.0, 0.0))
        self.assertIsNone(ana.tilt_direction(0.1, 0.1))   # 阈值 0.25 以内算放平


class TestDecimate(unittest.TestCase):
    def test_caps_length(self):
        seq = list(range(1000))
        out = srv.decimate(seq, 240)
        self.assertEqual(len(out), 240)
        self.assertEqual(out[0], 0)

    def test_short_sequence_passthrough(self):
        seq = [1, 2, 3]
        self.assertEqual(srv.decimate(seq, 240), [1, 2, 3])


class TestQueryParam(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(srv.query_param("a=1&b=2", "b"), "2")

    def test_percent_encoded_utf8(self):
        """前端用 encodeURIComponent，中文会变成 %E7%AC%AC…；必须按 UTF-8 还原。"""
        self.assertEqual(srv.query_param("device=%E7%AC%AC%E4%B8%89%E7%BB%84-07", "device"),
                         "第三组-07")

    def test_plus_is_not_space(self):
        # 用 unquote 而不是 unquote_plus：裸 '+' 要原样保留（设备名里可能有）
        self.assertEqual(srv.query_param("device=a+b", "device"), "a+b")

    def test_missing_returns_none(self):
        self.assertIsNone(srv.query_param("a=1", "device"))
        self.assertIsNone(srv.query_param("", "device"))


class TestSanitizeParams(unittest.TestCase):
    """外部输入一律不信：服务端夹一道，板端还会再夹一道。"""

    def test_led_blink_clamped(self):
        p = srv.sanitize_params("led_blink", {"n": 999, "on_ms": 1, "off_ms": 99999})
        self.assertEqual(p["n"], srv.LED_MAX_BLINKS)
        self.assertEqual(p["on_ms"], 20)
        self.assertEqual(p["off_ms"], 5000)

    def test_led_blink_pattern_whitelist(self):
        self.assertEqual(srv.sanitize_params("led_blink", {"pattern": "alert"})["pattern"], "alert")
        self.assertNotIn("pattern", srv.sanitize_params("led_blink", {"pattern": "rm -rf"}))

    def test_set_orient_clamped(self):
        self.assertEqual(srv.sanitize_params("set_orient", {"o": 99})["o"], 15)
        self.assertEqual(srv.sanitize_params("set_orient", {"o": -3})["o"], 0)

    def test_sd_rm_rejects_paths(self):
        for bad in ("../x", "a/b", "a\\b", ""):
            self.assertEqual(srv.sanitize_params("sd_rm", {"name": bad}), {}, "输入 %r" % bad)
        self.assertEqual(srv.sanitize_params("sd_rm", {"name": "a.jpg"})["name"], "a.jpg")

    def test_set_config_drops_empty_strings(self):
        """空串必须丢掉：板端把"没传"当"不改"，传空串反而会被当成"清空"。"""
        p = srv.sanitize_params("set_config", {"ssid": "   ", "url": "http://x", "period_ms": 10})
        self.assertNotIn("ssid", p)
        self.assertEqual(p["url"], "http://x")
        self.assertEqual(p["period_ms"], 100)     # 下限 100（2026-10-30 从 50 提高）

    def test_period_bounds(self):
        """上报周期下限 100 ms 是硬约束，不是随手写的数字。

        2026-10-30 从 50 提到 100：50 ms = 20 Hz 时每批只剩 5 个样本，
        32 块板同时上报会把服务端的连接数打爆（TIME_WAIT 堆到几万）。
        这条测试同时钉住上下界，避免有人"顺手"把下限调回去。
        """
        lo = srv.sanitize_params("set_config", {"period_ms": 0})["period_ms"]
        hi = srv.sanitize_params("set_config", {"period_ms": 999999})["period_ms"]
        self.assertEqual(lo, 100)
        self.assertEqual(hi, 2000)

    def test_cam_stream_fps_optional(self):
        self.assertNotIn("fps", srv.sanitize_params("cam_stream", {"on": True}))
        self.assertEqual(srv.sanitize_params("cam_stream", {"on": True, "fps": 99})["fps"],
                         srv.CAM_STREAM_FPS_MAX)

    def test_non_dict_params(self):
        self.assertEqual(srv.sanitize_params("led_blink", None)["n"], 3)
        self.assertEqual(srv.sanitize_params("led_blink", "junk")["n"], 3)


class TestParseIntent(unittest.TestCase):
    """自然语言 → 已有指令。规则是"宁可漏判、不可错判"。"""

    def test_photo(self):
        name, params, _ = srv.parse_intent("拍张照")
        self.assertEqual(name, "cam_capture")

    def test_dangerous_refused(self):
        # 注意：匹配的是 NL_DANGEROUS 里写死的词组。口语化的「把卡格了」不在词表里
        # （会落到大模型路径，只回话、不下发指令，所以没有执行风险，只是没被"指路"）。
        for text in ("格式化一下卡", "格卡", "清空卡", "重启板子", "改一下 wifi 密码"):
            got = srv.parse_intent(text)
            self.assertIsNotNone(got, "应当识别为危险动作而不是放过去：%s" % text)
            self.assertEqual(got[0], "__refuse__", text)

    def test_stream_on_off(self):
        self.assertEqual(srv.parse_intent("打开实时画面")[1], {"on": True})
        self.assertEqual(srv.parse_intent("关掉实时画面")[1], {"on": False})

    def test_led_blink_count(self):
        name, params, _ = srv.parse_intent("闪三下")
        self.assertEqual(name, "led_blink")
        self.assertEqual(params["n"], 3)

    def test_led_set(self):
        self.assertEqual(srv.parse_intent("把灯关掉")[1], {"on": False})
        self.assertEqual(srv.parse_intent("把灯点亮")[1], {"on": True})

    def test_look_back_is_not_a_new_capture(self):
        """「上次采集的数据」要走查询路径（保留旧时间），不能被当成新采集请求。"""
        self.assertIsNone(srv.parse_intent("上次采集的数据是多少"))

    def test_long_sentence_is_chat(self):
        self.assertIsNone(srv.parse_intent("我" * (srv.NL_MAX_LEN + 1)))

    def test_plain_question_is_none(self):
        self.assertIsNone(srv.parse_intent("今天天气怎么样"))

    def test_safe_commands_are_known(self):
        """自然语言白名单里的名字必须都在 CMD_NAMES 里（否则永远下发不出去）。"""
        for name in srv.NL_SAFE_COMMANDS:
            self.assertIn(name, srv.CMD_NAMES, name)


class TestSignalHelpers(unittest.TestCase):
    def test_count_steps_finds_peaks(self):
        """1 Hz 的正弦起伏，8 秒里应当数出约 8 步（局部极大值 + 0.3s 不应期）。

        注意不能用方波：`count_steps` 找的是**严格局部极大值**（`m < 前` 或 `m < 后`
        就跳过），平台期一个峰都数不出来 —— 这是刻意的，否则持续高幅运动会
        被每个采样点都数成一步。
        """
        win, mags = [], []
        for i in range(800):
            t = i * 0.01
            mag = 1.0 + 0.5 * math.sin(2 * math.pi * 1.0 * t)
            win.append((t, 0.0, 0.0, mag))
            mags.append(mag)
        steps = ana.count_steps(win, mags, 1.0)      # 均值按 1.0 给，阈值 = 1.25
        self.assertGreaterEqual(steps, 7)
        self.assertLessEqual(steps, 9)

    def test_count_steps_flat_is_zero(self):
        win = [(i * 0.01, 0.0, 0.0, 1.0) for i in range(100)]
        self.assertEqual(ana.count_steps(win, [1.0] * 100, 1.0), 0)

    def test_freefall_run(self):
        # FREEFALL_MIN_S(0.05) / dt(0.01) = 5 个样本才算数 —— 短于这个就是噪声
        mags = [1.0, 0.2, 0.1, 0.15, 0.2, 0.1, 0.12, 1.0]     # 中间连续 6 个 < 0.35
        self.assertEqual(ana.max_freefall_run(mags, 0.01), 6)
        self.assertEqual(ana.max_freefall_run([1.0] * 10, 0.01), 0)
        # 只有 3 个失重样本 → 不够长，判 0
        self.assertEqual(ana.max_freefall_run([1.0, 0.2, 0.1, 0.15, 1.0], 0.01), 0)

    def test_zero_cross_rate_frequency(self):
        """5 Hz 的正弦，零穿越率应当算回 ~5 Hz。"""
        dt = 0.01
        mags = [1.0 + 0.5 * math.sin(2 * math.pi * 5.0 * i * dt) for i in range(200)]
        zcr = ana.zero_cross_rate(mags, dt)
        self.assertAlmostEqual(zcr, 5.0, delta=1.0)


class TestAnalyze(unittest.TestCase):
    def _fresh(self):
        return srv.Device("unit-test")

    def test_still_board_reports_level(self):
        dev = self._fresh()
        now = 1000.0
        pts = [(0.0, 0.0, 1.0)] * 50
        activity, _events = ana.analyze(dev, now, pts, 0.01)
        self.assertIn("静置", activity)

    def test_freefall_detected(self):
        dev = self._fresh()
        now = 2000.0
        ana.analyze(dev, now - 0.5, [(0.0, 0.0, 1.0)] * 50, 0.01)
        activity, events = ana.analyze(dev, now, [(0.0, 0.0, 0.05)] * 20, 0.01)
        self.assertIn("跌落", activity)
        self.assertTrue(any(k == "fall" for k, _ in events))

    def test_samples_window_bounded(self):
        dev = self._fresh()
        for i in range(300):
            ana.analyze(dev, 3000.0 + i * 0.01, [(0.0, 0.0, 1.0)] * 10, 0.01)
        self.assertLessEqual(len(dev.samples), srv.MAX_WINDOW)


class TestDeviceNameEncodingContract(unittest.TestCase):
    """**跨端契约**：固件把设备名 percent-encode 后拼进 URL，服务端必须解回同一个 key。

    这条钉的是 `device/main/transport.c` 的 `url_encode()` 与服务端 `query_param()`
    必须互逆。任一侧改了规则（把空格当 '+'、改成不解码、或忘了转义就直接拼），
    这里先红 —— 而不是等真机上"实时画面恒 404、同一块板裂成两台"再查半天。

    背景（2026-10-30）：设备名是**用户在配网页手填**的，README 推荐的写法就是
    「第三组-07」。裸 UTF-8 拼进请求行后，Python 的 http.server 按 latin-1 解，
    于是同一块板在服务端变成 `ç¬¬ä¸ç»„-07` 这第二台设备。
    """

    RAW = "第三组-07"

    @staticmethod
    def _firmware_encode(text):
        """与 transport.c 的 url_encode() 同一规则（RFC 3986 unreserved 之外 %XX）。"""
        import urllib.parse
        return urllib.parse.quote(text, safe="-_.~")

    def test_encoded_round_trips_to_same_key(self):
        enc = self._firmware_encode(self.RAW)
        self.assertNotIn(self.RAW, enc)                      # 确实被编码了（全 ASCII）
        enc.encode("ascii")                                  # 编码后必须是纯 ASCII
        got = srv.clean_device_id(srv.query_param("device=" + enc, "device"))
        self.assertEqual(got, srv.clean_device_id(self.RAW))

    def test_raw_utf8_would_split_the_device(self):
        """反证：不做编码时服务端拿到的是乱码 key —— 这就是 bug 本身。"""
        mojibake = self.RAW.encode("utf-8").decode("latin-1")   # http.server 的解码方式
        self.assertNotEqual(srv.clean_device_id(mojibake), srv.clean_device_id(self.RAW))

    def test_ascii_name_is_stable(self):
        """ASCII 设备名编码前后必须一致（单板调试用的 rw1-XXXX 不能被改坏）。"""
        self.assertEqual(self._firmware_encode("rw1-07"), "rw1-07")


@unittest.skipUnless(
    hasattr(srv, "VALID_G_MAX"),
    "非有限输入过滤尚未合入（见 feature/p0-hardening）；合入后本组自动生效")
class TestNonFiniteInputs(unittest.TestCase):
    """`NaN` / `Infinity` 必须被挡在入口，绝不能进到分析里。

    背景（2026-10-30 复现）：`json.loads` 默认接受 `NaN` / `Infinity` 这两个
    **非法 JSON 字面量**，`float()` 也照收。于是 `{"batch":[[NaN,NaN,NaN]]}`
    （30 字节、无需鉴权）会：

      1. 在 `analyze()` → `tilt_direction()` 的 `int(nan)` 抛 ValueError，
         而且抛在响应头之前 —— 客户端只看到**连接被掐断**，不是 500；
      2. NaN 已经进了 `dev.samples`，**接下来 8 秒每一帧正常数据**都在同一个
         `int(nan)` 上再抛一次（窗口里还留着它）；
      3. `/api/latest` 再把 NaN 序列化出去，浏览器 `JSON.parse` 抛错 → 整页停更。

    固件侧早就有等价的一道（`transport.c` 的 `sanitize3`：任一轴越界整帧丢弃、
    绝不填 0），服务端一直漏着。本组用例钉住"入口过滤"这个不变量。
    """

    def test_nan_batch_is_rejected(self):
        pts = srv.Handler._parse_points({"batch": [[float("nan"), 0.0, 0.0]]})
        self.assertEqual(pts, [])

    def test_infinity_batch_is_rejected(self):
        for bad in (float("inf"), float("-inf")):
            pts = srv.Handler._parse_points({"batch": [[bad, 0.0, 1.0]]})
            self.assertEqual(pts, [], "值 %r 应当被丢弃" % bad)

    def test_out_of_range_is_rejected(self):
        """1e300 会让网页曲线纵轴直接压成一条线，按坏帧丢弃。"""
        self.assertEqual(srv.Handler._parse_points({"batch": [[1e300, 0.0, 0.0]]}), [])

    def test_good_points_survive(self):
        pts = srv.Handler._parse_points({"batch": [[0.1, -0.2, 0.98], [0.0, 0.0, 1.0]]})
        self.assertEqual(len(pts), 2)
        for p in pts:
            self.assertTrue(all(math.isfinite(v) for v in p))

    def test_clamp_int_rejects_non_finite(self):
        """`int(float("inf"))` 抛 OverflowError，不在原来的 except 里 → 500 + traceback。"""
        self.assertEqual(srv.clamp_int("inf", 1, 12, 3), 3)
        self.assertEqual(srv.clamp_int("nan", 1, 12, 3), 3)
        self.assertEqual(srv.clamp_int(float("inf"), 0, 15, 0), 0)
        self.assertEqual(srv.clamp_int("7", 1, 12, 3), 7)      # 正常值不受影响


class TestDashboardPage(unittest.TestCase):
    """仪表盘页面从 `server/web/index.html` 读，不再内嵌在 Python 字符串里。

    2026-10-30 拆出来的：2952 行 HTML/CSS/JS 住在三引号字符串里没有语法高亮、
    没有 ESLint，还逼出"页面里禁止出现反斜杠"这条古怪规矩（`\\n` 会被 Python
    先吃成真换行），改完样式还得重启服务端。

    这两条钉住"文件是唯一来源"：谁要是把 HTML 又塞回 Python 字符串，
    `test_file_is_the_source_of_truth` 立刻红。
    """

    def test_file_is_the_source_of_truth(self):
        path = os.path.join(os.path.dirname(_SERVER_PY), "web", "index.html")
        self.assertTrue(os.path.isfile(path), "缺 %s" % path)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), srv.dashboard_html())

    def test_page_looks_like_the_dashboard(self):
        html = srv.dashboard_html()
        self.assertIn("<!doctype html>", html)
        self.assertIn('id="board3d"', html)          # 3D 姿态那块画布
        self.assertIn("EventSource", html)           # SSE 客户端
        self.assertGreater(len(html), 100000)

    def test_missing_file_reports_readably(self):
        """文件丢了要给人一句能读的话，而不是抛异常让页面白屏。"""
        import tempfile
        old = srv._WEB_DIR
        srv._WEB_DIR = os.path.join(tempfile.gettempdir(), "rw1-no-such-web-dir")
        try:
            html = srv.dashboard_html()
        finally:
            srv._WEB_DIR = old
        self.assertIn("仪表盘文件缺失", html)


class TestModuleSplit(unittest.TestCase):
    """钉住 2026-11-01 的拆分边界（analysis / jsonl_log 搬出 server.py）。

    为什么值得有这几条：拆分的**全部价值**在于"这两个模块不依赖服务端"。
    一旦有人图省事在里面写 `from server import LOCK` 或把逻辑抄回 server.py，
    文件又会慢慢长回原样，而所有功能测试**照样全绿** —— 只有这类结构性断言能拦住。
    """

    def test_analysis_is_the_same_object_server_uses(self):
        """server.analyze 必须**就是** analysis.analyze，不是复制的一份。"""
        self.assertIs(srv.analyze, ana.analyze)
        self.assertIs(srv.tilt_direction, ana.tilt_direction)
        self.assertEqual(os.path.abspath(ana.__file__),
                         os.path.join(os.path.dirname(_HERE), "server", "analysis.py"))

    def test_analysis_holds_no_server_state(self):
        """analysis 里不该出现任何服务端全局状态 —— 它必须能单独 import。"""
        for name in ("LOCK", "DEVICES", "LOGGER", "SHOTS_DIR", "AUDIO_DIR",
                     "FRAMES", "API_TOKEN", "Handler", "Device"):
            self.assertFalse(hasattr(ana, name),
                             "analysis 里出现了服务端状态 %s —— 它就不再是叶子模块了" % name)

    def test_analysis_does_not_import_server(self):
        """源码层面也不许反向 import（那会变成循环依赖，按路径加载时直接炸）。"""
        src = open(ana.__file__, encoding="utf-8").read()
        for bad in ("import server", "from server import"):
            self.assertNotIn(bad, src, "analysis.py 反向 import 了服务端")

    def test_jsonl_logger_is_self_contained(self):
        """JsonlLogger 同样不许碰服务端状态（它的依赖数本来就是 0）。"""
        import jsonl_log
        self.assertIs(srv.JsonlLogger, jsonl_log.JsonlLogger)
        for name in ("LOCK", "DEVICES", "LOGGER", "Handler"):
            self.assertFalse(hasattr(jsonl_log, name))

    def test_thresholds_live_in_analysis_only(self):
        """阈值只能有一份。server.py 里再定义一次 = 两份慢慢走偏。"""
        for name in ("STEP_MIN_G", "STEP_REFRACTORY_S", "MOTION_STD", "SHAKE_ZCR",
                     "WALK_ZCR_MIN", "SHAKE_MIN_GAP_S", "FREEFALL_G", "FREEFALL_MIN_S"):
            self.assertFalse(hasattr(srv, name), "server.py 里又定义了 %s" % name)
            self.assertTrue(hasattr(ana, name), "analysis.py 里缺 %s" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)

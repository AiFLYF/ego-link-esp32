#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运动分析：把一批 IMU 样本变成「板子现在在干什么」。

**为什么单独一个文件**（2026-11-01 从 server.py 拆出）：

1. 这里是**唯一需要经常调参**的地方 —— 计步阈值、晃动频率、失重判据全在这一屏里。
   原来它们散在 server.py 的 56 / 67 / 121~137 / 171 行四段不连续的注释块中间，
   改一个阈值要滚过 2000 行别的代码。
2. 这个模块**零全局状态**：不碰 LOCK、不碰 DEVICES、不碰任何文件。
   所以它可以被单独 import（`python -c "import analysis"` 就能跑），
   单测也不必为了测一个纯函数而把整个 HTTP 服务端加载一遍。
3. 拆出来的边界是**用依赖图量出来的**，不是拍脑袋：这 5 个函数 + 11 个常量
   在 server.py 里只被彼此引用，外部一个都不碰（唯一的例外是
   `ACTIVITY_IDLE`，`Device` 拿它当初始值 —— 所以 server.py 反过来 import 它）。

坐标契约：板端上报的 x/y 已经是**屏幕坐标系**（+x 右、+y 下），板端在发送前
应用了自己的 NVS 方向校准（accel_input 的 orientation）。服务端不再做任何
轴向假设，两端对"上/下/左/右"的定义因此天然一致。
"""
import math

# --------------------------------------------------------------------------
# 阈值（改这里就够了，不用翻 server.py）
# --------------------------------------------------------------------------
# 统计/计步/曲线用的时间窗（秒）。server.py 的 MAX_WINDOW 由它算出容量。
WINDOW_S = 8.0
MOTION_WINDOW_S = 0.6     # 瞬时运动分类（晃动/跌落）用的短窗（秒）

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

TILT_DIRS = ["上", "右上", "右", "右下", "下", "左下", "左", "左上"]


# --------------------------------------------------------------------------
# 纯函数：给定输入必得同一输出，不读也不写任何外部状态
# --------------------------------------------------------------------------
def tilt_direction(x, y):
    """把屏幕坐标系的重力方向 (x 向右, y 向下) 映射成 8 方位。

    非有限值一律返回 None（当"判不出方向"，与放平同档）。这是一道**纵深防御**：
    正常路径上 `_parse_points()` 已经把 NaN/Inf 滤掉了，但本函数会被
    `ai_summary()` 直接调用，而 `int(nan)` 抛的 ValueError 会顺着
    `/api/latest` 的调用栈一路冒出去 —— 那种故障看起来像"整页卡死"，
    排查成本远高于这一行的代价。（2026-10-30 采纳协作者的复核意见。）
    """
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
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


# --------------------------------------------------------------------------
# 有状态：唯一一个会读写 dev 的函数（但只碰 dev，不碰模块全局）
# --------------------------------------------------------------------------
def analyze(dev, now, pts, dt):
    """把一批样本并入该设备的滑动窗口，返回 (活动标签, 事件列表)。

    `dev` 是 duck-typed 的：只要求有 `samples`（deque，元素为 (ts, x, y, z)）、
    `st`（dict）、`shake_times`（deque）。**不 import Device** —— 那是反向依赖，
    会让这个模块又变成"必须连着服务端一起加载"。
    """
    events = []
    prev_activity = dev.st["activity"]
    prev_fall = dev.st.get("fall_active", False)

    n = len(pts)
    for i, (x, y, z) in enumerate(pts):
        dev.samples.append((now - (n - 1 - i) * dt, x, y, z))

    # 建窗口与算 |a| **一趟做完**：原来分两个列表推导各扫一遍窗口，
    # 750 个样本就要多扫一次（实测省 ~0.06 ms/次，20 板 40 次/秒 ≈ 2.4 ms/s）。
    # 顺带删掉一个从来没人读的 `std_all`（长窗 |a| 标准差）：它每次上报都要
    # 对 750 个样本做一遍 (m-mean)**2 求和，实测 0.215 ms —— 占 analyze 的 24%，
    # 而分类判据用的是**短窗** r_std，这个值算完就丢。删掉不影响任何输出。
    win, mags = [], []
    for s in dev.samples:
        if now - s[0] <= WINDOW_S:
            win.append(s)
            ax, ay, az = s[1], s[2], s[3]
            mags.append(math.sqrt(ax * ax + ay * ay + az * az))
    if not win:
        return ACTIVITY_IDLE, events

    mean = sum(mags) / len(mags)

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

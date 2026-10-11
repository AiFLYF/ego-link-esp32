#!/usr/bin/env python
"""看一眼「20 台设备」的仪表盘 —— 这是从没被验证过的画面。

现有的 `e2e_dashboard.js` 永远只起 **2 块**假板子，所以「设备」卡片里
排 20 行是什么样子、窄屏会不会被撑爆，**没人看过**。课堂场景恰恰是 20 台。

做法：起服务端 → 用合成的遥测把 20 台设备喂进册 → 用真 Chrome 打开仪表盘，
按宽屏 / 窄屏各截一张图，并断言：
  - 设备卡片里确实有 20 行
  - 没有横向溢出
  - 没有 JS 报错 / 请求失败
截图落在 .workbuddy-ai/tmp/ 下，自己看一眼再下结论。

用法：
    NODE_PATH=<node workspace>/node_modules python tools/probe_20dev.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import verify_server as vs          # noqa: E402  复用 firmware_body / CHILD_ENV / OPENER

OUT = os.path.join(ROOT, ".workbuddy-ai", "tmp")
N = int(os.environ.get("PROBE_N", "20"))

# 要量哪些视口。默认扫一遍常见分辨率 + 两个断点贴边的值：
#   1920/1440 = 三列布局；1280 恰好是断点（≤1280 塌成两列）；
#   1024 是一列；320 是手机。
# 只量 1280 会把"断点"和"设备列表太高"两件事混在一起（见 PROBE_JS 里的注释）。
DEFAULT_VIEWPORTS = [[1920, 1080, "fhd"], [1440, 900, "wide"],
                     [1280, 900, "bp1280"], [1024, 768, "bp1024"],
                     [320, 720, "narrow"]]

PROBE_JS = r"""
const { chromium } = require('playwright-core');
(async () => {
  const b = await chromium.launch({ channel: 'chrome' });
  const out = [];
  /* 视口列表由 Python 侧传进来（PROBE_VIEWPORTS）。**必须扫多个宽度**：
     1280 恰好是 .stage-grid 从三列塌成两列的断点，只量它一个会把
     "断点把 hero-ai 挤到第二行"误读成"设备列表太长"（T64 的同一个教训：
     夹具规模/覆盖面本身是覆盖面的一部分）。 */
  const VPS = JSON.parse(process.env.PROBE_VIEWPORTS || '[]');
  for (const [w, h, tag] of VPS) {
    const page = await b.newPage({ viewport: { width: w, height: h } });
    const errs = [];
    page.on('pageerror', e => errs.push(String(e)));
    page.on('requestfailed', r => errs.push('reqfail ' + r.url()));
    await page.goto(process.env.PROBE_URL, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const info = await page.evaluate(() => {
      const devs = document.querySelectorAll('.dev');
      const de = document.documentElement;
      const box = sel => { const e = document.querySelector(sel); if (!e) return null;
        const r = e.getBoundingClientRect();
        return { top: Math.round(r.top + window.scrollY), h: Math.round(r.height) }; };
      // 找出所有比视口宽的元素（横向溢出的元凶）
      const wide = [];
      for (const el of document.querySelectorAll('*')) {
        if (el.scrollWidth > de.clientWidth + 2 && el.clientWidth > 0) {
          wide.push(el.tagName + '.' + (el.className || '').toString().split(' ')[0]
                    + ' sw=' + el.scrollWidth);
        }
      }
      return {
        devRows: devs.length,
        docScroll: de.scrollWidth, docClient: de.clientWidth,
        overflow: de.scrollWidth > de.clientWidth + 2,
        wide: wide.slice(0, 6),
        pageHeight: de.scrollHeight,
        viewport: window.innerHeight,
        devCard: box('#devcard'),
        rowH: devs[0] ? Math.round(devs[0].getBoundingClientRect().height) : 0,
        aiCard: box('.hero-ai'),
        c3d: box('#card3d'),
        heroWord: box('.hero-word'),
        /* .stage-grid 是几列、hero-ai 落在哪个 grid-column ——
           这是"AI 卡片被挤下去"到底是断点造成的、还是设备列表造成的分水岭 */
        gridCols: getComputedStyle(document.querySelector('.stage-grid'))
                    .gridTemplateColumns.split(' ').length,
        aiCol: getComputedStyle(document.querySelector('.hero-ai')).gridColumn,
      };
    });
    const png = process.env.PROBE_OUT + '/dash-20dev-' + tag + '.png';
    await page.screenshot({ path: png, fullPage: false });
    out.push({ tag, ...info, errs: errs.slice(0, 4), png });
    await page.close();
  }
  await b.close();
  console.log('PROBE_JSON=' + JSON.stringify(out));
})().catch(e => { console.log('PROBE_FATAL=' + e.message); process.exit(1); });
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    port = vs.free_port()
    base = "http://127.0.0.1:%d" % port
    tmp = tempfile.mkdtemp(prefix="rw1-20dev-")
    srv = subprocess.Popen([vs.PY, "-u", vs.SERVER_PY, "--port", str(port),
                            "--data-dir", os.path.join(tmp, "data"), "--retain-days", "0"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=vs.CHILD_ENV, cwd=ROOT)
    try:
        for _ in range(120):
            try:
                vs.OPENER.open(base + "/api/latest", timeout=2).read()
                break
            except Exception:                      # noqa: BLE001
                time.sleep(0.25)
        else:
            print("服务端起不来")
            return 2

        # 喂 20 台设备，名字用课堂里真实会出现的（组号）
        devs = ["第%02d组-%02d" % (i // 10 + 1, i % 10 + 1) for i in range(1, N + 1)]
        for round_ in range(6):                     # 多灌几轮，让曲线/事件有内容
            for d in devs:
                pts = [(0.01 * round_, -0.2 + 0.02 * round_, 1.0)] * 50
                vs.post_raw(base + "/api/telemetry",
                            vs.firmware_body(pts, device=d, orient=(round_ % 8)), timeout=10)
            time.sleep(0.15)
        live = vs.get_json(base + "/api/devices").get("devices", [])
        print("喂进去 %d 台，服务端在册 %d 台" % (len(devs), len(live)))

        vps = json.loads(os.environ.get("PROBE_VIEWPORTS") or "null") or DEFAULT_VIEWPORTS
        env = dict(os.environ)
        env["PROBE_URL"] = base + "/"
        env["PROBE_OUT"] = OUT.replace(os.sep, "/")
        env["PROBE_VIEWPORTS"] = json.dumps(vps)
        js = os.path.join(tempfile.gettempdir(), "rw1_probe20.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(PROBE_JS)
        # 超时**按视口数算**，别写死：一个视口 ≈ 一次冷启动 Chrome + 加载 20 台设备
        # 的整页 + 等 2.5s + 截图，实测 40~70 秒（沙箱里进程创建本身就慢，见手册 T59）。
        # 原来写死 180s 是照 2 个视口给的余量；加到 5 个视口会**稳定超时**
        # （2026-10-11 踩到：TimeoutExpired 而不是断言失败，看着像探针坏了）。
        timeout = 150 * len(vps) + 60
        p = subprocess.run([os.environ.get("NODE", "node"), js],
                           capture_output=True, text=True, env=env, timeout=timeout)
        for line in (p.stdout or "").splitlines():
            if line.startswith("PROBE_JSON="):
                for item in json.loads(line[len("PROBE_JSON="):]):
                    print("\n[%s] 视口 %s" % (item["tag"], item["png"]))
                    print("   设备卡片行数: %d（每行 %d px）" % (item["devRows"], item["rowH"]))
                    print("   页面总高 %d px / 视口 %d px（%.1f 屏）"
                          % (item["pageHeight"], item["viewport"],
                             item["pageHeight"] / float(item["viewport"])))
                    print("   stage-grid 列数: %s（hero-ai 落在 grid-column:%s）"
                          % (item.get("gridCols"), item.get("aiCol")))
                    if item.get("devCard"):
                        print("   设备卡片: top=%d 高=%d" % (item["devCard"]["top"], item["devCard"]["h"]))
                    if item.get("heroWord"):
                        print("   hero 活动词: top=%d" % item["heroWord"]["top"])
                    if item.get("aiCard"):
                        print("   AI 卡片: top=%d（首屏 %d px，%s）"
                              % (item["aiCard"]["top"], item["viewport"],
                                 "在首屏内" if item["aiCard"]["top"] < item["viewport"] else "**被顶出首屏**"))
                    if item.get("c3d"):
                        print("   3D 卡片: top=%d（%s）"
                              % (item["c3d"]["top"],
                                 "在首屏内" if item["c3d"]["top"] < item["viewport"] else "**被顶出首屏**"))
                    print("   横向溢出: %s（doc %d / client %d）"
                          % (item["overflow"], item["docScroll"], item["docClient"]))
                    if item["wide"]:
                        print("   比视口宽的元素: %s" % item["wide"])
                    print("   JS 报错: %s" % (item["errs"] or "无"))
            elif line.startswith("PROBE_FATAL="):
                print("!! %s" % line)
        if p.returncode != 0 and "PROBE_FATAL" not in (p.stdout or ""):
            print("node 退出码 %d\n%s" % (p.returncode, (p.stderr or "")[-600:]))
        return 0
    finally:
        srv.kill()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

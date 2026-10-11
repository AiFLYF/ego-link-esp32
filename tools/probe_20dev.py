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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import verify_server as vs          # noqa: E402  复用 firmware_body / CHILD_ENV / OPENER

OUT = os.path.join(ROOT, ".workbuddy-ai", "tmp")
N = int(os.environ.get("PROBE_N", "20"))

PROBE_JS = r"""
const { chromium } = require('playwright-core');
(async () => {
  const b = await chromium.launch({ channel: 'chrome' });
  const out = [];
  for (const [w, h, tag] of [[1280, 900, 'wide'], [320, 720, 'narrow']]) {
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

        env = dict(os.environ)
        env["PROBE_URL"] = base + "/"
        env["PROBE_OUT"] = OUT.replace(os.sep, "/")
        js = os.path.join(tempfile.gettempdir(), "rw1_probe20.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(PROBE_JS)
        p = subprocess.run([os.environ.get("NODE", "node"), js],
                           capture_output=True, text=True, env=env, timeout=180)
        for line in (p.stdout or "").splitlines():
            if line.startswith("PROBE_JSON="):
                for item in json.loads(line[len("PROBE_JSON="):]):
                    print("\n[%s] 视口 %s" % (item["tag"], item["png"]))
                    print("   设备卡片行数: %d（每行 %d px）" % (item["devRows"], item["rowH"]))
                    print("   页面总高 %d px / 视口 %d px（%.1f 屏）"
                          % (item["pageHeight"], item["viewport"],
                             item["pageHeight"] / float(item["viewport"])))
                    if item.get("devCard"):
                        print("   设备卡片: top=%d 高=%d" % (item["devCard"]["top"], item["devCard"]["h"]))
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

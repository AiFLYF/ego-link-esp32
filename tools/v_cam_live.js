/*
 * 验证摄像头实时画面这条链路：网页点「开启实时画面」→ 服务端取到帧 → <img> 渲染出来。
 *
 * 板子是 HTTP 客户端、没有自己的服务端，所以这里用"模拟板子"持续 POST JPEG，
 * 走的就是真机那条路径（板子 POST /api/frame → 网页 GET /api/frame）。
 *
 * 同时验证一件关键的事：**卡片在没有任何帧时也必须可见** ——
 * 早先版本让它 hidden、等收到第一帧才显示，而"开启"按钮就在卡片里 → 死锁。
 */
'use strict';
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const http = require('http');

const ROOT = path.resolve(__dirname, '..');
const PORT = 8083;
const BASE = 'http://127.0.0.1:' + PORT;
const PY = process.env.E2E_PYTHON || 'python';
const OUT = process.env.CAM_OUT || os.tmpdir();

/* 造一张最小但合法的 JPEG（纯色块）。服务端只校验魔数 FF D8。 */
function makeJpeg(w, h, rgb) {
  // 用最简的 JPEG：这里直接调 python 生成一张真图，避免手写编码器
  const { execFileSync } = require('child_process');
  const tmp = path.join(os.tmpdir(), 'rw1_cam_src.png');
  execFileSync(PY, ['-c', `
import io
from PIL import Image
Image.new('RGB', (${w}, ${h}), (${rgb})).save(r'${tmp.replace(/\\/g, '\\\\')}', 'PNG')
`]);
  return null;
}

function postFrame(buf) {
  return new Promise((resolve) => {
    const req = http.request(BASE + '/api/frame?device=fake-01', {
      method: 'POST',
      headers: { 'Content-Type': 'image/jpeg', 'Content-Length': buf.length },
    }, (res) => { res.resume(); res.on('end', resolve); });
    req.on('error', resolve);
    req.write(buf);
    req.end();
  });
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  // 用 Python 生成一张真 JPEG（带一块亮色，截图里一眼能认出来）
  const { execFileSync } = require('child_process');
  const jpgPath = path.join(os.tmpdir(), 'rw1_cam_frame.jpg');
  execFileSync(PY, ['-c', `
from PIL import Image, ImageDraw
im = Image.new('RGB', (320, 240), (18, 24, 34))
d = ImageDraw.Draw(im)
d.rectangle([20, 20, 300, 220], outline=(29, 158, 117), width=6)
d.ellipse([120, 80, 200, 160], fill=(239, 159, 39))
im.save(r'${jpgPath.replace(/\\/g, '\\\\')}', 'JPEG', quality=85)
`]);
  const jpeg = require('fs').readFileSync(jpgPath);
  console.log('  造好一帧 JPEG: ' + jpeg.length + ' 字节');

  const env = Object.assign({}, process.env,
    { no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost' });
  const srv = spawn(PY, ['-u', 'server/server.py', '--port', String(PORT),
                         '--data-dir', path.join(os.tmpdir(), 'rw1-camlive'), '--retain-days', '0'],
                   { cwd: ROOT, env });
  await sleep(2500);

  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--enable-unsafe-swiftshader'],
  });
  const page = await (await browser.newContext({ viewport: { width: 900, height: 800 } })).newPage();
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await sleep(3000);

  // ① **没有任何帧时**，卡片必须已经可见（这是修复的核心）
  const before = await page.evaluate(() => {
    const c = document.getElementById('cardcam');
    return { hidden: c ? c.hidden : null, btn: !!document.getElementById('camlive') };
  });
  console.log('  ① 没帧时卡片: ' + JSON.stringify(before) +
              (before.hidden === false ? '  ✓ 可见' : '  ✗ 还是隐藏的'));

  // ② 点「开启实时画面」
  await page.click('#camlive');
  const label = await page.evaluate(() => document.getElementById('camlive').textContent);
  console.log('  ② 点完按钮文案: ' + label);

  // ③ 模拟板子持续推帧
  for (let i = 0; i < 25; i++) {
    await postFrame(jpeg);
    await sleep(150);
  }
  await sleep(1500);

  // ④ 画面应该真的渲染出来了
  const after = await page.evaluate(() => {
    const im = document.getElementById('camimg');
    return { src: (im.src || '').slice(-28), w: im.naturalWidth, h: im.naturalHeight,
             nosig: document.getElementById('camnosig').style.display };
  });
  console.log('  ③ <img> 实际尺寸: ' + after.w + '×' + after.h +
              '  无信号提示 display=' + (after.nosig || '(空)'));
  console.log('     ' + (after.w > 0 ? '✓ 画面渲染出来了' : '✗ 画面没出来'));

  const el = await page.$('#cardcam');
  const png = path.join(OUT, 'cam-live.png');
  if (el) await el.screenshot({ path: png });
  console.log('  截图: ' + png);

  await browser.close();
  try { srv.kill(); } catch (e) { /* ignore */ }
})().catch(e => { console.log('FATAL ' + e.message); process.exit(1); });

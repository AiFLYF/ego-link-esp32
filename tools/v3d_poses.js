/*
 * 3D 姿态的**三姿态受控验证**（直接灌已知数值，不依赖假板子场景）。
 *
 *   flat     (0, 0, +1)      平放、屏幕朝上        → 期望：绿面朝上、黄条在远边
 *   upright  (0, +1, 0)      立起来、屏幕朝观察者  → 期望：站直、绿面朝我、黄条在**上面**
 *   tilt     (+0.7, 0, +0.72) 右边压低            → 期望：向右滚、右边低
 *
 * 每个姿态连灌 40 帧（让 slerp 收敛）后截图。
 */
'use strict';
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const http = require('http');

const ROOT = path.resolve(__dirname, '..');
const PORT = 8092;
const BASE = 'http://127.0.0.1:' + PORT;
const PY = process.env.E2E_PYTHON || 'python';
const OUT = process.env.V3_OUT || os.tmpdir();

const POSES = [
  ['flat', 0.0, 0.0, 1.0],
  /* **接近**平放的真实值（用户板子实测）—— 精确的 (0,0,1) 是退化点，
   * 只测它会掩盖"由噪声决定旋转角"这类问题（2026-09-24 踩过）。 */
  ['flat-real', -0.03, 0.0, 1.02],
  ['upright', 0.0, 1.0, 0.0],
  ['tilt', 0.7, 0.0, 0.72],
];

function post(payload) {
  return new Promise((resolve) => {
    const body = JSON.stringify(payload);
    const req = http.request(BASE + '/api/telemetry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => { res.resume(); res.on('end', resolve); });
    req.on('error', resolve);
    req.write(body);
    req.end();
  });
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const env = Object.assign({}, process.env, {
    no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost' });
  const srv = spawn(PY, ['-u', 'server/server.py', '--port', String(PORT),
                         '--data-dir', path.join(os.tmpdir(), 'rw1-pose3'), '--retain-days', '0'],
                   { cwd: ROOT, env });
  await sleep(2500);

  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--enable-unsafe-swiftshader'],
  });
  const page = await (await browser.newContext({ viewport: { width: 900, height: 700 } })).newPage();
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await sleep(3000);

  for (const [name, x, y, z] of POSES) {
    const dev = 'pose-' + name;
    for (let i = 0; i < 40; i++) {
      await post({ x: x, y: y, z: z, source: 'SC7A20', device: dev,
                   batch: [[x, y, z], [x, y, z], [x, y, z]] });
      await sleep(60);
    }
    await sleep(2500);                       // 等 slerp 收敛
    const el = await page.$('#card3d');
    const png = path.join(OUT, 'pose-' + name + '.png');
    if (el) await el.screenshot({ path: png });
    console.log('  ' + name.padEnd(8) + ' (' + x + ',' + y + ',' + z + ') → ' + png);
  }

  await browser.close();
  try { srv.kill(); } catch (e) { /* ignore */ }
  console.log('done');
})().catch(e => { console.log('FATAL ' + e.message); process.exit(1); });

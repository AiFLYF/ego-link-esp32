/*
 * 3D 姿态卡片的受控验证：用已知姿态的假板子各截一张图。
 *
 *   flat  —— 平放（静止水平）  → 期望：板面朝上，绿色屏幕面可见
 *   tilt  —— 持续向右倾斜      → 期望：板子绕"前后轴"滚转，右侧压低
 *
 * 输出两张 PNG，肉眼比对"物理姿态"和"3D 画出来的姿态"是否一致。
 * 不 push、不改任何东西 —— 只做验证。
 */
'use strict';
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');

const ROOT = path.resolve(__dirname, '..');
const PORT = 8097;
const BASE = 'http://127.0.0.1:' + PORT;
const PY = process.env.E2E_PYTHON || 'python';
const OUT = process.env.V3_OUT || os.tmpdir();

const procs = [];
function start(args) {
  const env = Object.assign({}, process.env, {
    no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost' });
  const p = spawn(PY, args, { cwd: ROOT, env });
  procs.push(p);
  return p;
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  start(['-u', 'server/server.py', '--port', String(PORT),
         '--data-dir', path.join(os.tmpdir(), 'rw1-v3data'), '--retain-days', '0']);
  await sleep(2500);

  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--enable-unsafe-swiftshader'],
  });
  const ctx = await browser.newContext({ viewport: { width: 900, height: 700 } });
  const page = await ctx.newPage();
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await sleep(3000);

  for (const scen of ['idle', 'tilt']) {
    const b = start(['-u', 'tools/fake_board.py', '--url', BASE, '--device', 'v3-' + scen,
                     '--scenario', scen, '--seconds', '30', '--quiet']);
    await sleep(5000);        // 等它上报几帧 + 3D slerp 收敛
    // 选中这台设备，保证页面看的是它
    await page.evaluate((dev) => { if (window.selDevice !== undefined) window.selDevice = dev; }, 'v3-' + scen);
    await sleep(4000);
    const el = await page.$('#card3d');
    const png = path.join(OUT, 'v3-' + scen + '.png');
    if (el) await el.screenshot({ path: png });
    // 同时把页面算出来的姿态读数抓下来，和 3D 对账
    const info = await page.evaluate(() => ({
      axis: document.getElementById('axisval') ? document.getElementById('axisval').textContent : null,
      act: document.getElementById('act') ? document.getElementById('act').textContent : null,
    }));
    console.log('  ' + scen + ' → ' + png + '  活动=' + (info.act || '?'));
    try { b.kill(); } catch (e) { /* ignore */ }
    await sleep(1500);
  }
  await browser.close();
  for (const p of procs) { try { p.kill(); } catch (e) { /* ignore */ } }
  console.log('done');
})().catch(e => { console.log('FATAL ' + e.message); process.exit(1); });

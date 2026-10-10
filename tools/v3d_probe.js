/* 直接在浏览器里量"黄条在世界坐标系里指向哪"——不再靠推理。 */
'use strict';
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const http = require('http');

const ROOT = path.resolve(__dirname, '..');
const PORT = 8090;
const BASE = 'http://127.0.0.1:' + PORT;
const PY = process.env.E2E_PYTHON || 'python';

function post(payload) {
  return new Promise((resolve) => {
    const body = JSON.stringify(payload);
    const req = http.request(BASE + '/api/telemetry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => { res.resume(); res.on('end', resolve); });
    req.on('error', resolve);
    req.write(body); req.end();
  });
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const env = Object.assign({}, process.env,
    { no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost' });
  const srv = spawn(PY, ['-u', 'server/server.py', '--port', String(PORT),
                         '--data-dir', path.join(os.tmpdir(), 'rw1-mk2'), '--retain-days', '0'],
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

  for (const [name, x, y, z] of [['flat', 0, 0, 1], ['upright', 0, 1, 0]]) {
    for (let i = 0; i < 25; i++) {
      await post({ x, y, z, source: 'SC7A20', device: 'mk-' + name, batch: [[x, y, z]] });
      await sleep(60);
    }
    await sleep(2500);
    const info = await page.evaluate(() => {
      const out = { has3d: !!window.d3 };
      if (!window.d3) return out;
      const THREE = window.THREE;
      const q = window.d3.cur;
      const dir = (v) => v.clone().applyQuaternion(q).toArray().map(n => +n.toFixed(2));
      out.top = dir(new THREE.Vector3(0, -1, 0));      // 板子顶边（黄条）
      out.screen = dir(new THREE.Vector3(0, 0, 1));    // 屏幕法线
      out.target_top = new THREE.Vector3(0, -1, 0).applyQuaternion(window.d3.target)
                        .toArray().map(n => +n.toFixed(2));
      return out;
    });
    console.log('  ' + name.padEnd(8) +
                ' 顶边→' + JSON.stringify(info.top) +
                '  屏幕法线→' + JSON.stringify(info.screen) +
                '  (target 顶边→' + JSON.stringify(info.target_top) + ')');
    console.log('            期望：顶边 (0,0,-1)=远边 / (0,-1,0)=上；屏幕法线 (0,1,0)=朝上 / (0,0,1)=朝我');
  }
  await browser.close();
  try { srv.kill(); } catch (e) { /* ignore */ }
})().catch(e => { console.log('FATAL ' + e.message); process.exit(1); });

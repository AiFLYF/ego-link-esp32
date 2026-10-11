#!/usr/bin/env node
/*
 * 仪表盘取数「背压」验收（真浏览器）。
 *
 * 为什么需要它：`e2e_dashboard.js` 验的是「能取到数、点了有反应」，
 * 而「服务端一慢，请求会不会无限排队」它一条都测不出来 —— 内网响应只要
 * 几毫秒，这个故障在开发环境**永远不会自己露面**。
 *
 * 背景：仪表盘的轮询是 `setInterval(pull, ms)`（默认 100ms）+ 异步 fetch。
 * setInterval 只管按节拍往下排，不看上一个请求回来没有。平时没问题；
 * 一旦单次响应耗时超过轮询间隔（服务端只有一把全局 LOCK，20 台同时上报
 * 都要进去；课堂 WiFi 再打个折 —— 恰恰是这套东西真正上场的场景），
 * 在飞的请求就会一个接一个积压，而且**永不收敛**。
 *
 * 实测：改之前，给浏览器网络加 400ms 延迟，10 秒里发起 99 个请求、
 * 只回来 21 个，结束时仍有 78 个在飞且还在涨；浏览器同域并发上限是 6，
 * 排队的那批会连 SSE、连取帧一起挤掉。加了在飞保护之后，同样条件下
 * 峰值在飞恒为 1。
 *
 * 判据：**任何时候同时在飞的请求不得超过 1 个**。
 * 有保护就是这样：这一 tick 发现上一轮没回来，直接跳过。
 *
 * 跑法：
 *   NODE_PATH="<node workspace>/node_modules" node tools/check_poll_backpressure.js
 * 自己起独立端口 + 假板子，不碰 8000。退出码非 0 即失败。
 */
'use strict';

const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.POLL_CHECK_PORT || 8098);
const BASE = 'http://127.0.0.1:' + PORT;
const PY = process.env.E2E_PYTHON || 'python';
const DATA_DIR = path.join(os.tmpdir(), 'rw1-pollcheck-data');
const SHOT_DIR = path.join(ROOT, '.workbuddy-ai', 'tmp');

/* 慢网这一档是「课堂 WiFi 打满」的等价物：每个请求固定加 400ms，
   远大于默认轮询间隔 100ms，堆积会立刻现形。 */
const SLOW = {
  offline: false,
  latency: 400,
  downloadThroughput: 300 * 1024 / 8,
  uploadThroughput: 300 * 1024 / 8,
};
const FAST = { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 };

const procs = [];
const logs = [];
function start(args, tag) {
  const env = Object.assign({}, process.env, {
    no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost',
  });
  const fs2 = require('fs');
  const logPath = path.join(SHOT_DIR, 'pollcheck-' + tag + '.log');
  const fd = fs2.openSync(logPath, 'w');
  logs.push(logPath);
  const p = spawn(PY, args, { cwd: ROOT, env, stdio: ['ignore', fd, fd] });
  /* 父进程这一份 fd 必须关掉：spawn 已经把副本给子进程了，自己留着等于泄漏。 */
  try { fs2.closeSync(fd); } catch (e) { /* 已关 */ }
  procs.push(p);
  return p;
}
const cleanup = () => { for (const p of procs) { try { p.kill(); } catch (e) { /* 没了 */ } } };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function closeBrowser(b) {
  try { await Promise.race([b.close(), sleep(8000)]); } catch (e) { /* 已经关了 */ }
}
async function exitNow(code) {
  await Promise.race([
    new Promise(r => { try { process.stdout.write('\n', () => r()); } catch (e) { r(); } }),
    sleep(1500),
  ]);
  process.exit(code);
}

/* 等 HTTP 真的能应答再往下走。固定 sleep 不可靠：服务端起得慢一点、
   或端口被占直接退出，后面就会拿到「连接被拒」这种像产品 bug 的假象。 */
async function waitHttp(url, maxSec) {
  const t0 = Date.now();
  while (Date.now() - t0 < (maxSec || 20) * 1000) {
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(1500) });
      if (r.ok) return true;
    } catch (e) { /* 还没起来 */ }
    await sleep(400);
  }
  return false;
}

async function measure(page, label, cond, secs) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', cond);

  const st = { started: 0, finished: 0, failed: 0, peak: 0, inflight: 0 };
  const pending = new Set();
  const hit = (r) => r.url().includes('/api/latest');

  const onReq = (r) => {
    if (!hit(r)) return;
    st.started++;
    pending.add(r);
    st.inflight = pending.size;
    if (st.inflight > st.peak) st.peak = st.inflight;
  };
  const settle = (r, which) => {
    if (!hit(r) || !pending.delete(r)) return;
    st[which]++;
    st.inflight = pending.size;
  };
  const onDone = (r) => settle(r, 'finished');
  const onFail = (r) => settle(r, 'failed');

  page.on('request', onReq);
  page.on('requestfinished', onDone);
  page.on('requestfailed', onFail);
  await sleep(secs * 1000);
  page.off('request', onReq);
  page.off('requestfinished', onDone);
  page.off('requestfailed', onFail);

  const mark = st.peak <= 1 ? 'OK' : 'FAIL';
  console.log('  [' + mark + '] ' + label + ' —— ' + secs + 's 内发起 ' + st.started +
              ' / 完成 ' + st.finished + ' / 失败 ' + st.failed +
              ' / 峰值在飞 ' + st.peak + '（上限 1）');
  return st;
}

(async () => {
  console.log('【取数背压】Server SSE + 轮询在慢网下不允许排队积压');
  start(['-u', 'server/server.py', '--port', String(PORT),
         '--data-dir', DATA_DIR, '--retain-days', '0'], 'server');
  if (!await waitHttp(BASE + '/api/latest')) {
    console.error('  服务端在 ' + PORT + ' 端口起不来（看 ' + logs.join(' ') + '）');
    cleanup();
    await exitNow(1);
  }
  start(['-u', 'tools/fake_board.py', '--url', BASE, '--device', '第三组-07'], 'board');
  await sleep(1500);

  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage();
  const jsErrors = [];
  page.on('pageerror', (e) => jsErrors.push(String(e)));

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await sleep(2500);   /* 等 SSE 和轮询都拉起来 */

  const ms = await page.evaluate(
    () => Number((document.querySelector('#pollsel') || {}).value) || 100);
  console.log('  当前轮询间隔 ' + ms + 'ms；慢网档给每个请求加 400ms 延迟');

  let bad = 0;
  const a = await measure(page, '正常网络', FAST, 3);
  const b = await measure(page, '慢网络（课堂 WiFi 打满）', SLOW, 5);
  if (a.peak > 1) { console.log('    ↑ 正常网络就有 ' + a.peak + ' 个请求同时在飞'); bad++; }
  if (b.peak > 1) { console.log('    ↑ 慢网络下堆积到 ' + b.peak + ' 个，且不收敛'); bad++; }
  if (jsErrors.length) {
    console.log('    ↑ 页面 JS 报错：' + jsErrors.slice(0, 3).join(' | '));
    bad++;
  }

  await closeBrowser(browser);
  cleanup();
  await sleep(300);
  console.log(bad ? '' : '结果: 无堆积，随时最多 1 个请求在飞');
  await exitNow(bad ? 1 : 0);
})().catch(async (e) => {
  console.error('脚本挂了：' + (e && e.stack || e));
  cleanup();
  await exitNow(1);
});

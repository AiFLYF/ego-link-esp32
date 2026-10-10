#!/usr/bin/env node
/*
 * 仪表盘真浏览器端到端测试（playwright-core + 系统 Chrome）。
 *
 * 为什么需要它：`tools/verify_server.py` 只测 HTTP 接口，测不到"页面能不能点"。
 * 2026-09-23 上线这套之后当场抓到 3 个真 bug：
 *   ① `syncOrient()` 只在初始化跑一次，而设备列表是**后到的** → 档位显示永远停在
 *      "还没收到板端上报的档位"（时序 bug，只跑接口测不出来）
 *   ② 页面没设 favicon → 每次打开都在控制台留一条 404
 *   ③ `sanitize_params` 对 set_orient 返回 `{}` → **网页选的档位被服务端丢掉**，
 *      板子只会收到"改成 o0"
 *
 * ⚠️ ③ 留下的教训（本文件里已经体现）：**要验"服务端存下的"，不能只验"页面发出的"**。
 * 当时的断言只检查了请求体里有 `o:3`，而服务端其实把它丢了 —— 所以下面
 * `下发后服务端队列里也要有` 这一步是必须的。
 *
 * 跑法（需要先装 playwright-core，见 README「跑真浏览器回归」一节）：
 *   NODE_PATH="<node workspace>/node_modules" node tools/e2e_dashboard.js
 *
 * 它会自己起一个**独立端口**的服务端 + 两块假板子，绝不碰你正在用的 8000 端口。
 */
'use strict';

const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.E2E_PORT || 8099);
const BASE = 'http://127.0.0.1:' + PORT;
const PY = process.env.E2E_PYTHON || 'python';
const DATA_DIR = path.join(os.tmpdir(), 'rw1-e2e-data');

const procs = [];
const logs = [];
function start(args, tag) {
  const env = Object.assign({}, process.env, {
    no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost',
  });
  /* 把子进程输出落盘：失败时能一眼看出是"服务端没起来"还是"产品 bug"，
     而不是对着一个空日志猜。 */
  const fs = require('fs');
  const logPath = path.join(os.tmpdir(), 'rw1-e2e-' + (tag || 'proc') + '.log');
  const fd = fs.openSync(logPath, 'w');
  logs.push(logPath);
  const p = spawn(PY, args, { cwd: ROOT, env, stdio: ['ignore', fd, fd] });
  /* 父进程这一份 fd 必须关掉：副本已经给子进程了，自己留着是泄漏（2026-09-29 修）。 */
  try { fs.closeSync(fd); } catch (e) { /* 已关 */ }
  procs.push(p);
  return p;
}
function cleanup() {
  for (const p of procs) { try { p.kill(); } catch (e) { /* 已经没了 */ } }
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* 关浏览器可能挂住：仪表盘有 SSE 长连接，Chrome 关不掉时 browser.close() 会一直等。
   跑完断言却卡在收尾，在 CI 里等于永不结束，所以给它一个上限。 */
async function closeBrowser(b) {
  try { await Promise.race([b.close(), sleep(8000)]); } catch (e) { /* 已经关了 */ }
}

/* 显式退出：playwright + spawn 的子进程会留下活跃 handle，事件循环不空，
   node 自己就永远不退（实测跑完断言后仍挂着，被 timeout 杀掉、退出码 124）。
   flush 用 race 兜底：Windows 下 write('') 空写不触发回调，等它就会卡死。 */
async function exitNow(code) {
  await Promise.race([
    new Promise(r => { try { process.stdout.write('\n', () => r()); } catch (e) { r(); } }),
    sleep(1500),
  ]);
  process.exit(code);
}

/* 等 HTTP 真的能应答再往下走。
   固定 `sleep(2500)` 是**不可靠的**：机器忙一点、服务端晚 1 秒绑定端口，
   后面的 `page.goto` 就会超时，看起来像产品打不开，其实是测试自己抢跑。
   （踩过：连着跑三套测试时这套报 "navigating ... waiting until domcontentloaded" 超时，
   单独跑又全绿 —— 就是这个原因。本仓库的记忆里早写着"E2E 必须先就绪探测"。） */
async function waitHttp(url, maxSec) {
  const t0 = Date.now();
  while (Date.now() - t0 < (maxSec || 25) * 1000) {
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(1500) });
      if (r.ok) return true;
    } catch (e) { /* 还没起来 */ }
    await sleep(400);
  }
  return false;
}

const problems = [];
let passed = 0;
async function step(name, fn) {
  try { await fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) {
    const msg = String(e.message || e).split('\n').slice(0, 3).join(' / ');
    console.log('  \u2717 ' + name + '  \u2192  ' + msg);
    problems.push('[' + name + '] ' + msg);
  }
}

(async () => {
  start(['-u', 'server/server.py', '--port', String(PORT),
         '--data-dir', DATA_DIR, '--retain-days', '0'], 'server');
  if (!await waitHttp(BASE + '/api/latest')) {
    throw new Error('服务端在 ' + PORT + ' 端口起不来（看 ' + logs[0] + '）');
  }
  // 两块假板子：一块带 oN，一块不带（模拟不支持 oN 的老固件）
  start(['-u', 'tools/fake_board.py', '--url', BASE, '--device', '第三组-07',
         '--orient', '7', '--scenario', 'tilt', '--seconds', '180', '--quiet'], 'boardA');
  start(['-u', 'tools/fake_board.py', '--url', BASE, '--device', '老固件-01',
         '--scenario', 'idle', '--seconds', '180', '--quiet'], 'boardB');
  /* 等两块板子都真的上报过 —— 用"设备列表里有 2 台"当就绪信号，
     不要 sleep 固定秒数（板子晚到 1 秒，后面的断言就会假失败）。 */
  const t0 = Date.now();
  while (Date.now() - t0 < 20000) {
    try {
      const j = await (await fetch(BASE + '/api/devices')).json();
      if ((j.devices || []).length >= 2) break;
    } catch (e) { /* 再等 */ }
    await sleep(500);
  }

  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--enable-unsafe-swiftshader'],   // headless 里要有 WebGL，3D 卡才建得起来
  });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();

  page.on('console', m => {
    if (m.type() === 'error') problems.push('CONSOLE: ' + m.text());
  });
  page.on('pageerror', e => problems.push('PAGEERROR: ' + e.message));
  page.on('requestfailed', r => problems.push('REQFAIL: ' + r.url()));
  page.on('response', r => {
    if (r.status() >= 400 && r.url().indexOf('favicon') < 0) {
      problems.push('HTTP ' + r.status() + ' ' + r.url());
    }
  });

  // 记录页面发出的下发请求（用于验"页面发出的"）
  const sent = [];
  page.on('request', r => {
    if (r.method() === 'POST' && r.url().indexOf('/api/command') >= 0) {
      try { sent.push(JSON.parse(r.postData() || '{}')); }
      catch (e) { sent.push({ bad: r.postData() }); }
    }
  });

  const api = (p) => ctx.request.get(BASE + p).then(r => r.json());
  const lastSent = () => sent[sent.length - 1];
  /* 按钮会因为"上一条指令还没走完"而 disabled —— 等它可用再点，
     否则测试会随命令往返的时机随机失败（不是产品 bug）。 */
  const clickWhenReady = async (sel, timeoutMs) => {
    const loc = page.locator(sel);
    const t0 = Date.now();
    while (Date.now() - t0 < (timeoutMs || 15000)) {
      if ((await loc.count()) && !(await loc.isDisabled())) {
        await loc.click({ timeout: 8000 });
        return;
      }
      await sleep(300);
    }
    const diag = await page.evaluate(() => ({
      pill: (document.getElementById('dev') || {}).textContent,
      devcount: (document.getElementById('devcount') || {}).textContent,
      hint: (document.getElementById('cmdhint') || {}).textContent,
    }));
    let qs = '?';
    try {
      const c = await api('/api/commands');
      qs = JSON.stringify((c.commands || []).map(x => x.name + ':' + x.state));
    } catch (e) { qs = '取不到: ' + e.message; }
    throw new Error('等不到可点击的 ' + sel + '；页面=' + JSON.stringify(diag)
                    + ' 队列=' + qs);
  };

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await sleep(4000);

  await step('设备卡片出现且有两台', async () => {
    await page.waitForSelector('#devcard:not([hidden])', { timeout: 15000 });
    const n = await page.locator('#devs .dev').count();
    if (n !== 2) throw new Error('设备行数 = ' + n);
  });

  await step('设备卡片不会闪（连续取数 3 秒内始终可见）', async () => {
    /* 2026-09-26 抓到的真 bug：`/api/latest`（100ms 轮询）不带 devices 字段，
       而 SSE（500ms）带 —— 于是这一块以取数频率反复藏/显，大部分时间不可见。
       采样 60 次 × 50ms = 3 秒，覆盖 30 次轮询 + 6 次 SSE。 */
    const r = await page.evaluate(() => new Promise(resolve => {
      const el = document.getElementById('devcard');
      let hidden = 0, n = 0;
      const t = setInterval(() => {
        n++;
        if (el.hidden || getComputedStyle(el).display === 'none') hidden++;
        if (n >= 60) { clearInterval(t); resolve({ hidden, n }); }
      }, 50);
    }));
    if (r.hidden) throw new Error('3 秒内被藏起来 ' + r.hidden + '/' + r.n + ' 次（应为 0）');
  });

  await step('每台前面有复选框', async () => {
    const n = await page.locator('#devs .devchk').count();
    if (n !== 2) throw new Error('复选框数 = ' + n);
  });

  await step('档位选择器有 0..15 共 16 项', async () => {
    const n = await page.locator('#orientsel option').count();
    if (n !== 16) throw new Error('option 数 = ' + n);
  });

  await step('档位显示里能看到板端上报的 oN（假板子报的 7）', async () => {
    const t = (await page.textContent('#orientnow')) || '';
    if (t.indexOf('o7') < 0) throw new Error('实际: ' + t);
  });

  await step('点「全选」后按钮文案变化、两个框都勾上', async () => {
    await page.click('#devall');
    await sleep(300);
    const t = (await page.textContent('#devall')) || '';
    if (t.indexOf('已选') < 0) throw new Error('按钮文案: ' + t);
    const c = await page.locator('#devs .devchk:checked').count();
    if (c !== 2) throw new Error('勾选数 = ' + c);
  });

  let batchIds = [];
  await step('全选后下发 → 请求体走 devices，且**服务端真的给两台各建了一条**', async () => {
    sent.length = 0;
    await clickWhenReady('#cmdbts button[data-cmd="led_blink"]');
    await sleep(1500);
    const b = lastSent();
    if (!b) throw new Error('没抓到 /api/command 请求');
    if (!Array.isArray(b.devices) || b.devices.length !== 2) {
      throw new Error('请求体 devices = ' + JSON.stringify(b.devices));
    }
    // ★ 关键：不只看页面发了什么，还要看服务端**存下了什么**
    const devs = (await api('/api/devices')).devices.map(d => d.id);
    batchIds = [];
    for (const d of devs) {
      const cs = (await api('/api/commands?device=' + encodeURIComponent(d))).commands || [];
      const mine = cs.filter(c => c.name === 'led_blink');
      if (mine.length) batchIds.push(mine[0].id);
    }
    if (batchIds.length !== 2) {
      throw new Error('服务端只给 ' + batchIds.length + ' 台建了命令（应为 2）');
    }
  });

  await step('再点「取消全选」→ 不再带 devices', async () => {
    await page.click('#devall');
    await sleep(300);
    const t = (await page.textContent('#devall')) || '';
    if (t.indexOf('全选') < 0 || t.indexOf('已选') >= 0) throw new Error('按钮文案: ' + t);
    sent.length = 0;
    await clickWhenReady('#cmdbts button[data-cmd="led_blink"]');
    await sleep(1500);
    const b = lastSent();
    if (!b) throw new Error('没抓到请求');
    if (b.devices) throw new Error('取消全选后不该带 devices: ' + JSON.stringify(b));
    /* 注意：不勾任何一台时**本来就不带 device 字段** —— 交给服务端用
       "最近上报过的那台"（单板场景的向后兼容）。 */
  });

  await step('点「应用档位」→ 页面发 set_orient，且**服务端队列里 o=3 没被丢**', async () => {
    await page.selectOption('#orientsel', '3');
    sent.length = 0;
    await clickWhenReady('#orientapply');
    await sleep(1500);
    const b = lastSent();
    if (!b || b.name !== 'set_orient') throw new Error('body = ' + JSON.stringify(b));
    if (!b.params || b.params.o !== 3) throw new Error('页面发的 params = ' + JSON.stringify(b.params));
    // ★ 这条就是 2026-09-23 漏掉的那一步：sanitize_params 曾把 o 丢掉
    let found = null;
    for (const d of (await api('/api/devices')).devices.map(x => x.id)) {
      const cs = (await api('/api/commands?device=' + encodeURIComponent(d))).commands || [];
      const m = cs.find(c => c.name === 'set_orient');
      if (m) found = m;
    }
    if (!found) throw new Error('服务端队列里找不到 set_orient');
    if (found.params.o !== 3) {
      throw new Error('服务端把 o 丢了：' + JSON.stringify(found.params));
    }
  });

  await step('设置面板存在，且空着不发', async () => {
    const has = await page.locator('#cfgssid, #cfgpass, #cfgurl, #cfgper, #cfgapply').count();
    if (has < 5) throw new Error('设置表单元素不全（找到 ' + has + ' 个）');
    sent.length = 0;
    await page.click('#cfgapply');
    await sleep(800);
    if (sent.length) throw new Error('什么都没填却下发了：' + JSON.stringify(lastSent()));
  });

  await step('/vendor/three.min.js 能取到（白名单路由）', async () => {
    const r = await ctx.request.get(BASE + '/vendor/three.min.js');
    if (r.status() !== 200) throw new Error('HTTP ' + r.status());
    const t = await r.text();
    if (t.indexOf('THREE') < 0) throw new Error('内容不像 three.js');
  });

  await step('3D 卡片显示出来、THREE 已加载', async () => {
    const ok = await page.evaluate(() => {
      const c = document.getElementById('card3d');
      return { hidden: c ? c.hidden : null, has: typeof THREE !== 'undefined',
               rev: (typeof THREE !== 'undefined') ? THREE.REVISION : null };
    });
    if (ok.hidden !== false) throw new Error('卡片还 hidden：' + JSON.stringify(ok));
    if (!ok.has) throw new Error('THREE 没加载');
    console.log('     three.js r' + ok.rev);
  });

  await step('3D canvas 有实际尺寸（WebGL 生效）', async () => {
    const m = await page.evaluate(() => {
      const cv = document.getElementById('board3d');
      return { w: cv.width, h: cv.height };
    });
    if (!(m.w > 100 && m.h > 100)) throw new Error('canvas 尺寸异常: ' + JSON.stringify(m));
    console.log('     canvas ' + m.w + 'x' + m.h);
  });

  await step('没有横向溢出', async () => {
    const over = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (over > 2) throw new Error('溢出 ' + over + 'px');
  });

  await step('没有 JS 报错 / 请求失败 / 4xx', async () => {
    if (problems.length) throw new Error(problems.slice(0, 3).join(' | '));
  });

  const shot = process.env.E2E_SHOT;
  if (shot) await page.screenshot({ path: shot, fullPage: true });
  await closeBrowser(browser);
  cleanup();

  console.log('\n结果: ' + passed + ' 通过' + (problems.length ? ', ' + problems.length + ' 处问题' : ', 无问题'));
  if (problems.length) {
    console.log(problems.join('\n'));
    const fs = require('fs');
    for (const p of logs) {
      try {
        const t = fs.readFileSync(p, 'utf8').trim();
        if (t) console.log('\n--- ' + path.basename(p) + ' ---\n' + t.split('\n').slice(-20).join('\n'));
      } catch (e) { /* 没日志 */ }
    }
  }
  await exitNow(problems.length ? 1 : 0);
})().catch(async e => {
  console.log('FATAL: ' + (e && e.message));
  const fs = require('fs');
  for (const p of logs) {
    try {
      const t = fs.readFileSync(p, 'utf8').trim();
      if (t) console.log('\n--- ' + path.basename(p) + ' ---\n' + t.split('\n').slice(-20).join('\n'));
    } catch (err) { /* 没日志 */ }
  }
  cleanup();
  await exitNow(1);
});

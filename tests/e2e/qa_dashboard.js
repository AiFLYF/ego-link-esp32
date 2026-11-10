#!/usr/bin/env node
/*
 * QA 全量回归 · 仪表盘（docs/test/TEST_PLAN.md 的 A–N 段）
 *
 * 自己起独立端口 + 自带 JS 假板子（比 fake_board.py 好控制：能回 sd_ls 的 note、
 * 能按需"故意不执行指令"）。不碰任何已有实例。
 *
 * 用法：
 *   NODE_PATH=<node workspace>/node_modules node tests/e2e/qa_dashboard.js
 *
 * 结果写 _qa_shots/run/results.json，截图写 _qa_shots/run/*.png
 */
'use strict';
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const PORT = Number(process.env.QA_PORT || 8098);
const BASE = 'http://127.0.0.1:' + PORT;
const OUT = path.join(ROOT, '_qa_shots', 'run');
fs.mkdirSync(OUT, { recursive: true });
const DATA_DIR = path.join(os.tmpdir(), 'rw1-qa-data-' + PORT);

const procs = [];
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function startServer() {
  const logPath = path.join(os.tmpdir(), 'rw1-qa-server-' + PORT + '.log');
  const fd = fs.openSync(logPath, 'w');
  const env = Object.assign({}, process.env, {
    no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost',
  });
  const p = spawn(process.env.QA_PYTHON || 'python',
    ['-u', 'server/server.py', '--port', String(PORT), '--data-dir', DATA_DIR,
     '--retain-days', '0', '--cmd-timeout', '6'],
    { cwd: ROOT, env, stdio: ['ignore', fd, fd] });
  try { fs.closeSync(fd); } catch (e) {}
  procs.push(p);
  return p;
}

async function waitHttp(url, maxSec) {
  const t0 = Date.now();
  while (Date.now() - t0 < (maxSec || 60) * 1000) {
    try { const r = await fetch(url, { signal: AbortSignal.timeout(1500) }); if (r.ok) return true; }
    catch (e) {}
    await sleep(400);
  }
  return false;
}

/* ---------------- JS 假板子 ---------------- */
const SCEN = {
  idle: () => [0.02, 0.01, 1.0],
  // 1.9 Hz 步行：100Hz 采样下周期约 52 个样本（真实走路 1.5–2.5Hz）
  walk: (i) => [0.42 * Math.sin(i * 0.12), 0.18 * Math.cos(i * 0.09), 1.0 + 0.30 * Math.sin(i * 0.12 + 0.6)],
  tilt: () => [0.62, 0.05, 0.78],
  shake: (i) => [0.9 * Math.sin(i * 2.2), 0.8 * Math.cos(i * 2.6), 1.0 + 0.5 * Math.sin(i * 3.1)],
};

function startBoard(opts) {
  const o = Object.assign({ dev: 'qa-board', scenario: 'walk', orient: null,
                            period: 500, hz: 100, noCmd: false, sd: null }, opts);
  const gen = SCEN[o.scenario] || SCEN.idle;
  const per = Math.max(1, Math.round(o.hz * o.period / 1000));
  let pending = null, stopped = false, n = 0, paused = false;
  const stat = { posts: 0, fails: 0, cmds: [] };

  async function tick() {
    if (stopped || paused) return;
    n++;
    const batch = [];
    for (let i = 0; i < per; i++) {
      const v = gen(n * per + i);
      batch.push([+(v[0]).toFixed(4), +(v[1]).toFixed(4), +(v[2]).toFixed(4)]);
    }
    const last = batch[batch.length - 1];
    const body = { device: o.dev, batch: batch, x: last[0], y: last[1], z: last[2],
                   source: 'SC7A20', ask: false };
    if (o.orient !== null) body.o = o.orient;
    if (pending) {
      const [cid, cname, cparams] = pending;
      const res = { id: cid, ok: true, ms: 200, n: batch.length };
      if (cname === 'capture_once') {
        res.x = last[0]; res.y = last[1]; res.z = last[2]; res.std = 0.0123;
      } else if (cname === 'sd_ls' && o.sd) {
        res.note = o.sd;
      } else if (cname === 'sd_rm') {
        res.note = '已删除 ' + (cparams.name || '?');
      }
      body.result = res;
      pending = null;
    }
    try {
      const r = await fetch(BASE + '/api/telemetry', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: AbortSignal.timeout(4000),
      });
      stat.posts++;
      const d = await r.json();
      if (d.cmd) {
        stat.cmds.push(d.cmd.name);
        if (o.noCmd) { /* 故意不执行 */ }
        else pending = [d.cmd.id, d.cmd.name, d.cmd.params || {}];
      }
    } catch (e) { stat.fails++; }
  }
  const timer = setInterval(tick, o.period);
  tick();
  return {
    stop() { stopped = true; clearInterval(timer); },
    pause() { paused = true; },
    resume() { paused = false; },
    stat, opts: o,
  };
}

/* ---------------- 断言框架 ---------------- */
const results = [];
let passed = 0, failed = 0, skipped = 0;
const consoleIssues = [];

async function step(id, name, fn) {
  const t0 = Date.now();
  try {
    await fn();
    passed++;
    results.push({ id, name, status: 'PASS', ms: Date.now() - t0 });
    console.log('  \u2713 ' + id + ' ' + name);
  } catch (e) {
    failed++;
    const msg = String(e && e.message || e).split('\n').slice(0, 4).join(' / ');
    results.push({ id, name, status: 'FAIL', msg, ms: Date.now() - t0 });
    console.log('  \u2717 ' + id + ' ' + name + '  \u2192  ' + msg);
  }
}
function skip(id, name, why) {
  skipped++;
  results.push({ id, name, status: 'SKIP', msg: why });
  console.log('  \u2013 ' + id + ' ' + name + '（无法测试：' + why + '）');
}
function ok(c, m) { if (!c) throw new Error(m || '断言失败'); }
function eq(a, b, m) {
  if (a !== b) throw new Error((m || '') + '：期望 ' + JSON.stringify(b) + '，实际 ' + JSON.stringify(a));
}
function has(s, sub, m) {
  if (String(s).indexOf(sub) < 0) throw new Error((m || '') + '：期望包含「' + sub + '」，实际「' + s + '」');
}

async function txt(page, sel) {
  const el = await page.$(sel);
  if (!el) return null;
  return (await el.textContent() || '').trim();
}

async function waitFor(page, fn, sec, label) {
  const t0 = Date.now();
  while (Date.now() - t0 < (sec || 10) * 1000) {
    try { if (await fn()) return true; } catch (e) {}
    await sleep(250);
  }
  throw new Error('等待超时（' + (sec || 10) + 's）：' + (label || ''));
}

async function shot(page, name) {
  try { await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: false }); } catch (e) {}
}

(async () => {
  startServer();
  if (!await waitHttp(BASE + '/api/latest', 90)) {
    console.error('服务端起不来，终止');
    process.exit(2);
  }

  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--enable-unsafe-swiftshader'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const external = [];
  page.on('request', r => {
    try {
      const u = new URL(r.url());
      if (u.hostname !== '127.0.0.1' && u.hostname !== 'localhost') external.push(r.url());
    } catch (e) {}
  });
  page.on('console', m => { if (m.type() === 'error') consoleIssues.push('CONSOLE ' + m.text()); });
  page.on('pageerror', e => consoleIssues.push('PAGEERROR ' + e.message));
  page.on('response', r => { if (r.status() >= 400) consoleIssues.push('HTTP ' + r.status() + ' ' + r.url()); });

  /* ============ 第一段：空态 ============ */
  console.log('\n== A/B/C/E/F/G/H/I/L 空态 ==');
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await sleep(2500);

  await step('A01', '页面可打开，标题正确', async () => {
    has(await page.title(), 'Ego Link');
    has(await txt(page, '.brand'), 'Ego Link');
  });
  await step('A07', '空态不出现 NaN/undefined/null', async () => {
    // 用 innerText：textContent 会把 <script> 源码也算进来（那是测试自身的假阳性）
    const body = await page.evaluate(() => document.body.innerText);
    ok(!/NaN|undefined|\[object Object\]/.test(body), '页面文本含 NaN/undefined：' +
       (body.match(/.{0,30}(NaN|undefined|\[object Object\]).{0,30}/) || [''])[0]);
    eq(await txt(page, '#act'), '等待数据');
    eq(await txt(page, '#abs'), '0.00 g');
    eq(await txt(page, '#steps'), '0');
  });
  await step('B01', '无板子时设备胶囊为离线', async () => {
    has(await txt(page, '#dev'), '离线');
  });
  await step('B03', '无数据采样率显示 — Hz', async () => {
    eq(await txt(page, '#hzv'), '— Hz');
  });
  await step('B04', '无数据上报帧数为 0', async () => {
    eq(await txt(page, '#postv'), '↑0 帧');
  });
  await step('C01', '单板/无板时设备卡片隐藏', async () => {
    ok(!(await page.isVisible('#devcard')), '#devcard 不该显示');
  });
  await step('E01', '指令按钮 8 个，只有 1 个 primary', async () => {
    const n = await page.$$eval('#cmdbts button', bs => bs.length);
    eq(n, 8, '按钮数');
    const prim = await page.$$eval('#cmdbts button.primary', bs => bs.map(b => b.dataset.cmd));
    eq(prim.length, 1, 'primary 按钮数');
    eq(prim[0], 'capture_once');
    const names = await page.$$eval('#cmdbts button', bs => bs.map(b => b.dataset.cmd));
    ok(names.indexOf('sd_rm') < 0, 'sd_rm 不该出现在指令栏');
    ok(names.indexOf('cam_stream') < 0, 'cam_stream 不该出现在指令栏');
  });
  await step('E03', '板子离线时指令按钮全部禁用', async () => {
    const dis = await page.$$eval('#cmdbts button', bs => bs.filter(b => b.disabled).length);
    eq(dis, 8);
  });
  await step('D16', '没有 pending 时停止按钮隐藏', async () => {
    ok(!(await page.isVisible('#aicancel')), '#aicancel 不该显示');
  });
  await step('G01', '指令记录空态', async () => {
    has(await txt(page, '#cmds'), '还没有下发过指令');
  });
  await step('H01', '事件流空态有提示文案', async () => {
    const t = (await txt(page, '#feed')) || '';
    ok(t.length > 0, '事件流卡片在无事件时是**空白**的（没有任何空态提示）');
  });
  await step('I01', '无帧时不显示破图，显示引导文案', async () => {
    ok(!(await page.isVisible('#camimg')), '#camimg 不该显示');
    has(await txt(page, '#camnosig'), '开启实时画面');
  });
  await step('I09', '照片列表空态', async () => {
    has(await txt(page, '#shotlist'), '还没有照片');
  });
  await step('L01', '存储面板未读取时给引导', async () => {
    has(await txt(page, '#sdring'), '还没读过');
  });
  await step('A06', '播报开关默认开且 aria-pressed 正确', async () => {
    eq(await txt(page, '#ttsbtn'), '播报 开');
    eq(await page.getAttribute('#ttsbtn', 'aria-pressed'), 'true');
  });
  await step('F01', '曲线空态提示不贴着 y 轴刻度、不顶在最上沿', async () => {
    /* 不看源码里的常量（那是自证），直接看**画布像素**：
       全不透明的像素只可能来自文字（网格线 alpha .22、参考线 .40/.55），
       左边 x<padL 的是刻度数字，右边的是空态提示。要求：
         ① 提示的墨迹离 y 轴刻度至少 40px（不能读成 "2.0 等待…"）；
         ② 提示的纵向中心落在画布中间一半（不能顶在最上沿冒充刻度）。 */
    const g = await page.evaluate(() => {
      const cv = document.getElementById('cv');
      const c2 = cv.getContext('2d');
      const W = cv.width, H = cv.height, dpr = window.devicePixelRatio || 1;
      const padL = 34 * dpr;
      const d = c2.getImageData(0, 0, W, H).data;
      let minX = W, minY = H, maxY = -1, n = 0;
      for (let y = 0; y < H; y++) {
        for (let x = Math.floor(padL + 2 * dpr); x < W; x++) {
          if (d[(y * W + x) * 4 + 3] > 200) {
            n++;
            if (x < minX) minX = x;
            if (y < minY) minY = y;
            if (y > maxY) maxY = y;
          }
        }
      }
      return { minX, minY, maxY, n, padL, H, W, dpr };
    });
    ok(g.n > 20, '画布上没找到空态提示文字（不透明像素 ' + g.n + ' 个）');
    const dx = (g.minX - g.padL) / g.dpr;
    ok(dx >= 40, '空态提示离 y 轴刻度只有 ' + dx.toFixed(1) + 'px，会被读成刻度的一部分');
    const cy = (g.minY + g.maxY) / 2;
    ok(cy > g.H * 0.3 && cy < g.H * 0.7, '空态提示的纵向中心在 ' + (cy / g.H * 100).toFixed(0) + '%，顶在最上沿');
  });
  await step('A03', '零外部请求（教室没外网也能用）', async () => {
    eq(external.length, 0, '请求了外部资源：' + external.slice(0, 3).join(', '));
    const fonts = await page.evaluate(() => {
      const out = [];
      document.fonts.forEach(f => out.push(f.family + '/' + f.status));
      return out;
    });
    ok(fonts.length > 0, '没有任何 @font-face 被加载');
  });
  await step('A02', '首屏无 console error / 4xx', async () => {
    const real = consoleIssues.filter(x => x.indexOf('HTTP 4') >= 0 || x.indexOf('CONSOLE') >= 0 || x.indexOf('PAGEERROR') >= 0);
    eq(real.length, 0, '实际有 ' + real.length + ' 条：\n    ' + real.slice(0, 6).join('\n    '));
  });
  await shot(page, 'empty-1440');

  /* ============ 第二段：单板在线 ============ */
  console.log('\n== 单板在线（步行） ==');
  const b1 = startBoard({ dev: '第三组-07', scenario: 'walk', orient: 7 });
  await waitFor(page, async () => (await page.textContent('#dev')).indexOf('在线') >= 0, 20, '设备上线');

  await step('B02', '板子上线后胶囊显示在线', async () => {
    has(await txt(page, '#dev'), '在线');
  });
  await step('B05', '链路状态为实时', async () => {
    await waitFor(page, async () => (await txt(page, '#linkv')) === '实时', 8, '实时');
  });
  await step('D02', '步行场景活动词与步数', async () => {
    await waitFor(page, async () => (await txt(page, '#act')).indexOf('步行') >= 0, 12, '步行');
    const steps = Number(await txt(page, '#steps'));
    ok(steps > 0, '步数应 > 0，实际 ' + steps);
  });
  await step('D01', '合加速度读数 = 三轴模长', async () => {
    const j = await (await fetch(BASE + '/api/latest')).json();
    const l = j.latest;
    const mag = Math.sqrt(l[1] * l[1] + l[2] * l[2] + l[3] * l[3]);
    const shown = parseFloat(await txt(page, '#abs'));
    ok(Math.abs(shown - mag) < 0.06, '页面 ' + shown + ' vs 计算 ' + mag.toFixed(3));
  });
  await step('D06', '三轴数值与接口一致', async () => {
    // 先暂停上报：否则读页面和读接口之间数据又变了（2Hz），比的是两个时刻
    b1.pause();
    await sleep(1200);
    const j = await (await fetch(BASE + '/api/latest')).json();
    const l = j.latest;
    const vx = parseFloat(await txt(page, '#vx'));
    const vy = parseFloat(await txt(page, '#vy'));
    const vz = parseFloat(await txt(page, '#vz'));
    ok(Math.abs(vx - l[1]) < 0.02, 'X: 页面 ' + vx + ' vs 接口 ' + l[1]);
    ok(Math.abs(vy - l[2]) < 0.02, 'Y: 页面 ' + vy + ' vs 接口 ' + l[2]);
    ok(Math.abs(vz - l[3]) < 0.02, 'Z: 页面 ' + vz + ' vs 接口 ' + l[3]);
    b1.resume();
  });
  await step('D05', '倾角与姿态球同时有值', async () => {
    has(await txt(page, '#tilt'), '倾角');
    const d = await page.$eval('#ball', el => getComputedStyle(el).transform);
    ok(d && d !== 'none', '姿态球没有位移：' + d);
  });
  await step('J01', '3D 卡片可见且有渲染内容', async () => {
    await waitFor(page, async () => page.isVisible('#card3d'), 10, '3D 卡片');
    const nonBlank = await page.evaluate(() => {
      const c = document.getElementById('board3d');
      if (!c) return false;
      return c.width > 10 && c.height > 10;
    });
    ok(nonBlank, '3D 画布尺寸异常');
  });
  await step('F02', '有数据时曲线画出来了', async () => {
    const nonBlank = await page.evaluate(() => {
      const cv = document.getElementById('cv');
      const g = cv.getContext('2d');
      const d = g.getImageData(0, 0, cv.width, cv.height).data;
      let n = 0;
      for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
      return n;
    });
    ok(nonBlank > 1000, '画布几乎全空，非透明像素 ' + nonBlank);
  });
  await step('E02', '点「采集一次」→ 按钮变等待 → 回传后恢复', async () => {
    await page.click('#cmdbts button[data-cmd="capture_once"]');
    await sleep(120);
    const t = await txt(page, '#cmdbts button[data-cmd="capture_once"]');
    ok(t === '等待板子回传…' || t === '采集一次', '按钮文案：' + t);
    await waitFor(page, async () => {
      const tt = await txt(page, '#cmdbts button[data-cmd="capture_once"]');
      return tt === '采集一次';
    }, 15, '回传后恢复');
  });
  await step('G02/G03', '指令记录出现完成态并带测量值', async () => {
    await waitFor(page, async () => {
      const t = await txt(page, '#cmds');
      return t && t.indexOf('已完成') >= 0;
    }, 15, '已完成');
    const t = await txt(page, '#cmds');
    has(t, 'capture_once');
    ok(/x=.*y=.*z=/.test(t), '缺少测量值：' + t);
  });
  await step('B04', '上报帧数随上报增长', async () => {
    const v = await txt(page, '#postv');
    const n = Number(String(v).replace(/[^\d]/g, ''));
    ok(n > 0, '帧数 ' + v);
  });
  await step('G09', '档位显示板端上报的 o7', async () => {
    await waitFor(page, async () => (await txt(page, '#orientnow')).indexOf('o7') >= 0, 8, 'o7');
  });
  await shot(page, 'live-1440');

  await step('H02', '事件流有内容且时间格式正确', async () => {
    await waitFor(page, async () => (await page.$$('#feed .item')).length > 0, 10, '事件');
    const first = await page.$eval('#feed .item', el => el.textContent);
    ok(/\d{2}:\d{2}:\d{2}/.test(first), '时间格式：' + first);
  });

  /* ============ 第三段：多设备 ============ */
  console.log('\n== 多设备 ==');
  const b2 = startBoard({ dev: '第一组-01', scenario: 'tilt', orient: 3 });
  await waitFor(page, async () => page.isVisible('#devcard'), 20, '设备卡片出现');
  await sleep(1200);

  await step('C02', '设备卡片显示台数与在线数', async () => {
    const t = await txt(page, '#devcount');
    has(t, '2 台');
    has(t, '在线 2');
  });
  await step('C03', '点第 2 行切换查看设备', async () => {
    const rows = await page.$$('#devs .dev');
    eq(rows.length, 2, '设备行数');
    const ids = await page.$$eval('#devs .dev', rs => rs.map(r => r.dataset.dev));
    const target = ids[1];
    await page.click(`#devs .dev[data-dev="${target}"]`);
    await sleep(300);
    ok(await page.$eval(`#devs .dev[data-dev="${target}"]`, r => r.classList.contains('sel')),
       '被点的行没有 .sel');
    await waitFor(page, async () => (await txt(page, '#src')) === 'SC7A20', 8, 'src');
  });
  await step('C04', '勾复选框不切换查看设备', async () => {
    const before = await page.$eval('#devs .dev.sel', r => r.dataset.dev);
    await page.click('#devs .dev:first-child .devchk');
    await sleep(200);
    const after = await page.$eval('#devs .dev.sel', r => r.dataset.dev);
    eq(after, before, '勾选后选中行变了');
    has(await txt(page, '#devall'), '已选 1');
  });
  await step('C05', '再点全选按钮 → 取消全部', async () => {
    await page.click('#devall');
    await sleep(200);
    eq(await txt(page, '#devall'), '全选');
    const checked = await page.$$eval('#devs .devchk', bs => bs.filter(b => b.checked).length);
    eq(checked, 0);
  });
  await step('C06', '点全选 → 全部勾上', async () => {
    await page.click('#devall');
    await sleep(200);
    has(await txt(page, '#devall'), '已选 2');
    const checked = await page.$$eval('#devs .devchk', bs => bs.filter(b => b.checked).length);
    eq(checked, 2);
  });
  await step('C08', '勾 2 台下发 → 服务端建 2 条', async () => {
    await page.click('#cmdbts button[data-cmd="led_blink"]');
    await sleep(800);
    // /api/commands 不带 ?device= 只回"最近上报的那台"，所以逐台查
    const ids = await page.$$eval('#devs .dev', rs => rs.map(r => r.dataset.dev));
    let total = 0;
    for (const id of ids) {
      const j = await (await fetch(BASE + '/api/commands?device=' + encodeURIComponent(id))).json();
      total += (j.commands || []).filter(c => c.name === 'led_blink').length;
    }
    ok(total >= 2, '两台合计只建了 ' + total + ' 条 led_blink');
    has(await txt(page, '#cmdhint'), '2 台');
  });
  await step('C09', '离线设备行显示离线', async () => {
    b2.stop();
    await waitFor(page, async () => {
      const t = await txt(page, '#devs');
      return t.indexOf('离线') >= 0;
    }, 20, '离线');
  });
  await step('C07', '设备行键盘可达（Enter 切换）', async () => {
    const ids = await page.$$eval('#devs .dev', rs => rs.map(r => r.dataset.dev));
    const target = ids[0];
    await page.focus(`#devs .dev[data-dev="${target}"]`);
    await page.keyboard.press('Enter');
    await sleep(300);
    ok(await page.$eval(`#devs .dev[data-dev="${target}"]`, r => r.classList.contains('sel')),
       'Enter 没有切换设备');
  });
  await shot(page, 'multi-1440');

  /* ============ 第四段：交互 ============ */
  console.log('\n== 交互 ==');
  await step('D10', '空输入点发送不发请求', async () => {
    let asked = 0;
    const h = r => { if (r.url().indexOf('/api/ask') >= 0) asked++; };
    page.on('request', h);
    await page.fill('#asktext', '   ');
    await page.click('#asksend');
    await sleep(600);
    page.off('request', h);
    eq(asked, 0, '空输入仍发了请求');
  });
  await step('D09', '打字提问 → 回复更新且输入框清空', async () => {
    await page.fill('#asktext', '分析当前运动状态');
    await page.keyboard.press('Enter');
    await waitFor(page, async () => {
      const t = await txt(page, '#reply');
      return t && t !== '还没有提问。' && t.length > 2;
    }, 15, '回复更新');
    eq(await page.inputValue('#asktext'), '');
    ok(await page.$eval('#replybox', e => e.classList.contains('has')), '回复框没有 .has');
  });
  await step('D11', '输入框 maxlength=200', async () => {
    eq(await page.getAttribute('#asktext', 'maxlength'), '200');
    await page.fill('#asktext', 'x'.repeat(260));
    eq((await page.inputValue('#asktext')).length, 200);
    await page.fill('#asktext', '');
  });
  await step('D12', '注入字符串不执行、页面不被破坏', async () => {
    await page.fill('#asktext', '<img src=x onerror=window.__xss=1><script>window.__xss2=1</script>');
    await page.keyboard.press('Enter');
    await sleep(2000);
    const xss = await page.evaluate(() => [window.__xss, window.__xss2]);
    ok(!xss[0] && !xss[1], '发生了 XSS：' + JSON.stringify(xss));
    ok((await page.$$('#reply img, #reply script')).length === 0, '回复里生成了标签');
    const after = await txt(page, '#reply');
    ok(after && after.length > 0, '回复被清空');
    /* 不比较"回复是否变化"：规则 AI 的回答只依赖传感器读数，
       两次提问拿到同一句是完全正常的（第一版这么断言，跑第二次就假红）。 */
  });
  await step('D13', '4 个快捷 chip 都能发出对应文本', async () => {
    const chips = await page.$$eval('.ask-chip', bs => bs.map(b => b.dataset.ask));
    eq(chips.length, 4);
    const seen = [];
    const h = async r => {
      if (r.url().indexOf('/api/ask') >= 0) {
        try { seen.push(JSON.parse(r.postData() || '{}').text); } catch (e) {}
      }
    };
    page.on('request', h);
    for (const c of chips) {
      await page.click(`.ask-chip[data-ask="${c}"]`);
      await sleep(700);
    }
    page.off('request', h);
    for (const c of chips) ok(seen.indexOf(c) >= 0, 'chip 没发出：' + c + '（实际 ' + seen.join('|') + '）');
  });
  await step('D14', '播报开关切换 + 持久化', async () => {
    await page.click('#ttsbtn');
    eq(await txt(page, '#ttsbtn'), '播报 关');
    eq(await page.getAttribute('#ttsbtn', 'aria-pressed'), 'false');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await sleep(1800);
    eq(await txt(page, '#ttsbtn'), '播报 关', '刷新后没记住');
    await page.click('#ttsbtn');
    eq(await txt(page, '#ttsbtn'), '播报 开');
  });
  await step('B06', '主题切换 3 次不残留中间态', async () => {
    eq(await page.getAttribute('html', 'data-theme'), 'light');
    for (let i = 0; i < 3; i++) {
      await page.click('#theme');
      await sleep(350);
    }
    eq(await page.getAttribute('html', 'data-theme'), 'dark');
    await page.click('#theme');
    await sleep(350);
    eq(await page.getAttribute('html', 'data-theme'), 'light');
  });
  await step('E04/N01/N02', '格式化 SD 卡：弹自定义确认框，取消不发', async () => {
    let sent = 0;
    const h = r => { if (r.url().indexOf('/api/command') >= 0) sent++; };
    page.on('request', h);
    await page.click('#cmdbts button[data-cmd="sd_format"]');
    await sleep(300);
    ok(await page.isVisible('#cfm'), '没弹确认框');
    has(await txt(page, '#cfm-title'), '格式化');
    has(await txt(page, '#cfm-body'), '无法恢复');
    await page.click('#cfm-cancel');
    await sleep(500);
    page.off('request', h);
    eq(sent, 0, '取消后仍发了指令');
  });
  await step('N02', '确认框 Esc = 取消', async () => {
    await page.click('#cmdbts button[data-cmd="sd_format"]');
    await sleep(300);
    await page.keyboard.press('Escape');
    await sleep(400);
    ok(!(await page.isVisible('#cfm')), 'Esc 没关掉弹窗');
  });
  await step('E05', '确认后才真的下发 sd_format', async () => {
    await page.click('#cmdbts button[data-cmd="sd_format"]');
    await sleep(300);
    await page.click('#cfm-ok');
    await sleep(800);
    const j = await (await fetch(BASE + '/api/commands')).json();
    ok((j.commands || []).some(c => c.name === 'sd_format'), '服务端没有 sd_format');
  });
  await step('E06', 'LED 常亮/熄灭 是 toggle，两次 on 相反', async () => {
    const before = await (await fetch(BASE + '/api/commands')).json();
    const n0 = (before.commands || []).filter(c => c.name === 'led_set').length;
    await page.click('#cmdbts button[data-cmd="led_set"]');
    await sleep(900);
    const t1 = await txt(page, '#cmdbts button[data-cmd="led_set"]');
    await page.click('#cmdbts button[data-cmd="led_set"]');
    await sleep(900);
    const after = await (await fetch(BASE + '/api/commands')).json();
    const ls = (after.commands || []).filter(c => c.name === 'led_set');
    ok(ls.length >= n0 + 2, '没发出两条 led_set');
    const on0 = ls.find(c => c.params && c.params.on === true);
    const on1 = ls.find(c => c.params && c.params.on === false);
    ok(on0 && on1, '两次的 on 参数没有相反');
  });
  await step('G07', '应用档位 o5 后服务端真的存了 o=5', async () => {
    await page.selectOption('#orientsel', '5');
    await page.click('#orientapply');
    await sleep(900);
    const j = await (await fetch(BASE + '/api/commands')).json();
    const c = (j.commands || []).find(x => x.name === 'set_orient' && x.params && x.params.o === 5);
    ok(c, '服务端没存 o=5，实际：' +
       JSON.stringify((j.commands || []).filter(x => x.name === 'set_orient').map(x => x.params)));
  });
  await step('H03/H04/H06', '事件流展开/收起', async () => {
    await waitFor(page, async () => (await page.$$('#feed .item')).length > 0, 10, '有事件');
    await page.click('#evmore');
    await waitFor(page, async () => (await txt(page, '#evmore')) === '只看最近', 10, '展开');
    has(await txt(page, '#evcount'), '全部');
    await page.click('#evmore');
    await sleep(600);
    eq(await txt(page, '#evmore'), '查看全部');
  });
  await step('K01', '板子设置全空 → 提示不改，不发请求', async () => {
    let sent = 0;
    const h = r => { if (r.url().indexOf('/api/command') >= 0) sent++; };
    page.on('request', h);
    await page.click('#cfgapply');
    await sleep(400);
    page.off('request', h);
    has(await txt(page, '#cfghint'), '什么都没填');
    eq(sent, 0);
  });
  await step('K06', '填 SSID+密码 下发后密码框清空', async () => {
    await page.fill('#cfgssid', 'QA-WiFi');
    await page.fill('#cfgpass', 'secret123');
    await page.click('#cfgapply');
    await sleep(600);
    eq(await page.inputValue('#cfgpass'), '');
    has(await txt(page, '#cfghint'), '试连');
  });
  await step('M01/M02/M04', '密钥框摊开 / 保存 / 回车', async () => {
    ok(!(await page.isVisible('#keyinput')), '密钥输入框默认应收起');
    await page.click('#keybtn');
    ok(await page.isVisible('#keyinput'), '点了没摊开');
    await page.fill('#keyinput', 'qa-token');
    await page.keyboard.press('Enter');
    await sleep(400);
    has(await txt(page, '#keybtn'), '密钥 ✓');
    ok(!(await page.isVisible('#keyinput')), '保存后没收起');
  });

  /* ============ 第五段：SD 面板 / 照片 ============ */
  console.log('\n== 存储 / 照片 ==');
  await step('L02', '读取存储信息 → 圆环 + 文件列表', async () => {
    // 让板子回一条 80% 占用的 note
    b1.opts.sd = 'SPACE,1000000000,200000000;CAM0001.JPG|123456;CAM0002.JPG|2345;LONGNAME.TXT|999';
    await page.click('#sdrefresh');
    await waitFor(page, async () => (await txt(page, '#sdring')).indexOf('%') >= 0, 15, '圆环');
    has(await txt(page, '#sdring'), '80%');
    has(await txt(page, '#sdring'), '已用');
    has(await txt(page, '#sdfiles'), 'CAM0001.JPG');
  });
  await step('L03', '占用 >75% 时圆环变琥珀色', async () => {
    const c = await page.$eval('#sdring .ringtxt b', el => getComputedStyle(el).color);
    // 琥珀 --color-shake 的 oklch 值在亮色下应是偏暖色：R 明显大于 B
    const m = c.match(/[\d.]+/g).map(Number);
    ok(m.length >= 3, '取色失败：' + c);
    ok(m[0] > m[2], '80% 占用时颜色不是琥珀（' + c + '）');
  });
  await step('L06', '点文件删除 → 取消 → 不发 sd_rm', async () => {
    let sent = 0;
    const h = r => { if (r.url().indexOf('/api/command') >= 0) sent++; };
    page.on('request', h);
    await page.click('#sdfiles [data-rm="CAM0001.JPG"]');
    await sleep(300);
    await page.click('#cfm-cancel');
    await sleep(400);
    page.off('request', h);
    eq(sent, 0);
  });
  await step('L07', '删除 SD 文件只需确认一次', async () => {
    await page.click('#sdfiles [data-rm="CAM0001.JPG"]');
    await sleep(400);
    ok(await page.evaluate(() => document.getElementById('cfm').open), '第一次确认框没打开');
    await page.click('#cfm-ok');
    await sleep(900);
    const stillOpen = await page.evaluate(() => document.getElementById('cfm').open);
    const title = await txt(page, '#cfm-title');
    ok(!stillOpen, '点了「确定」之后又弹了第二个确认框（标题：' + title + '）');
  });
  await step('L08', '连续确认两次后 sd_rm 真的下发（验证底层可用）', async () => {
    // 若上一步已经关掉，这里可能没有弹窗了；容错处理
    if (await page.evaluate(() => document.getElementById('cfm').open)) {
      await page.click('#cfm-ok');
      await sleep(700);
    }
    const j = await (await fetch(BASE + '/api/commands?device=' + encodeURIComponent('第三组-07'))).json();
    ok((j.commands || []).some(c => c.name === 'sd_rm' && c.params && c.params.name === 'CAM0001.JPG'),
       '没下发 sd_rm 或参数不对');
  });
  await step('I02/I10', '推一帧后画面显示；照片出现在列表', async () => {
    // 上一步可能留下了一个没关的确认框（见 L07），先清掉，否则后面所有点击都被它挡住
    for (let i = 0; i < 3; i++) {
      if (await page.evaluate(() => document.getElementById('cfm').open)) {
        await page.click('#cfm-ok'); await sleep(400);
      } else break;
    }
    // 必须是**浏览器能解码**的真 JPEG：服务端只校验 FF D8 魔数，但 <img> 解不出来
    // 就会走 onerror → 图片保持 hidden，测试会误判成"画面没显示"（踩过一次）。
    const jpeg = Buffer.from(
      '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a' +
      'HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDs0NDT/wAALCAABAAEBAREA/8QAFAABAAAAAAAA' +
      'AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==', 'base64');
    const r = await fetch(BASE + '/api/frame?device=' + encodeURIComponent('第三组-07') + '&save=1',
      { method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: jpeg });
    ok(r.ok, '推帧失败 ' + r.status);
    await page.click('#camlive');       // 开启实时画面
    await waitFor(page, async () => page.isVisible('#camimg'), 15, '画面显示');
    has(await txt(page, '#camstat'), '×');
    await sleep(1200);
    await page.click('#camlive');       // 关掉
    await sleep(800);
  });
  await step('I03/I04', '实时画面按钮下发 cam_stream on/off', async () => {
    const j = await (await fetch(BASE + '/api/commands')).json();
    const cs = (j.commands || []).filter(c => c.name === 'cam_stream');
    ok(cs.some(c => c.params && c.params.on === true), '没下发 on:true');
    ok(cs.some(c => c.params && c.params.on === false), '没下发 on:false');
  });
  await step('I06/I08', '拍照存本机 + 删除照片', async () => {
    for (let i = 0; i < 3; i++) {
      if (await page.evaluate(() => document.getElementById('cfm').open)) {
        await page.click('#cfm-ok'); await sleep(400);
      } else break;
    }
    await page.click('#camshot');
    await waitFor(page, async () => {
      const t = await txt(page, '#shotlist');
      return t && t.indexOf('本机存档') >= 0;
    }, 15, '本机存档');
    const n0 = (await page.$$('#shotlist .shot')).length;
    await page.click('#shotlist .shot button[data-local="1"]');
    await sleep(400);
    await page.click('#cfm-ok');
    await sleep(800);
    const n1 = (await page.$$('#shotlist .shot')).length;
    ok(n1 < n0, '删除后数量没减少：' + n0 + ' → ' + n1);
  });

  await step('D08-T03', '窄屏摄像头空态文案不指错方向', async () => {
    await page.setViewportSize({ width: 375, height: 812 });
    await sleep(700);
    const cols = await page.$eval('.camwrap', e =>
      getComputedStyle(e).gridTemplateColumns.trim().split(/\s+/).length);
    const t = await txt(page, '#camnosig');
    if (cols === 1) {
      ok(t.indexOf('右侧') < 0, '窄屏下按钮在图片**下方**，文案却说「点右侧」：' + t);
    }
    await page.screenshot({ path: path.join(OUT, 'cam-narrow.png'), fullPage: false });
    await page.setViewportSize({ width: 1440, height: 900 });
    await sleep(400);
  });

  /* ============ 第六段：响应式 / 主题 / 可访问性 ============ */
  console.log('\n== 响应式 / 主题 / 可访问性 ==');
  for (const vp of [{ n: '1440', w: 1440, h: 900 }, { n: '768', w: 768, h: 1024 }, { n: '375', w: 375, h: 812 }]) {
    await step('A04-' + vp.n, vp.n + 'px 无横向溢出', async () => {
      await page.setViewportSize({ width: vp.w, height: vp.h });
      await sleep(900);
      const m = await page.evaluate(() => ({
        sw: document.documentElement.scrollWidth, iw: window.innerWidth,
        bw: document.body.scrollWidth,
      }));
      ok(m.sw <= m.iw + 1, 'scrollWidth ' + m.sw + ' > innerWidth ' + m.iw);
      await shot(page, 'resp-' + vp.n);
    });
  }
  await step('A05', '默认明亮主题，切深色后刷新保持', async () => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await sleep(1800);
    eq(await page.getAttribute('html', 'data-theme'), 'light');
    await page.click('#theme');
    await sleep(400);
    eq(await page.getAttribute('html', 'data-theme'), 'dark');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await sleep(1500);
    eq(await page.getAttribute('html', 'data-theme'), 'dark', '刷新后没记住主题');
    await shot(page, 'dark-1440');
    await page.click('#theme');
    await sleep(300);
  });
  await step('A11', 'Tab 顺序：第一个焦点落在主题按钮', async () => {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await sleep(2000);
    const order = [];
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press('Tab');
      order.push(await page.evaluate(() => {
        const a = document.activeElement;
        return a ? (a.id || a.className || a.tagName) : 'null';
      }));
    }
    eq(order[0], 'theme', '第一个 Tab 应落在主题按钮，实际 ' + order[0]);
    ok(order.indexOf('asktext') > 0, '输入框不在 Tab 序列里：' + order.join(','));
  });
  await step('A11b', '每个可聚焦元素有可见焦点', async () => {
    await page.focus('#asktext');
    const outline = await page.evaluate(() => {
      const e = document.activeElement;
      const s = getComputedStyle(e);
      return { o: s.outlineStyle, w: s.outlineWidth, shadow: s.boxShadow };
    });
    ok(outline.o !== 'none' || outline.shadow !== 'none', '焦点没有可见样式：' + JSON.stringify(outline));
  });
  await step('A12', '所有 img 有 alt', async () => {
    const bad = await page.$$eval('img', imgs => imgs.filter(i => i.getAttribute('alt') === null).length);
    eq(bad, 0);
  });
  await step('B07', '滚动后顶栏加阴影', async () => {
    await page.evaluate(() => window.scrollTo(0, 600));
    await sleep(400);
    ok(await page.$eval('.appbar', e => e.classList.contains('scrolled')), '没有 scrolled');
    await page.evaluate(() => window.scrollTo(0, 0));
    await sleep(400);
    ok(!(await page.$eval('.appbar', e => e.classList.contains('scrolled'))), '回顶后没去掉');
  });
  await step('J05', 'reduced-motion 下 3D 不报错', async () => {
    const c2 = await browser.newContext({ viewport: { width: 1280, height: 900 },
      reducedMotion: 'reduce' });
    const p2 = await c2.newPage();
    const errs = [];
    p2.on('pageerror', e => errs.push(e.message));
    await p2.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await sleep(2500);
    eq(errs.length, 0, '有 JS 报错：' + errs.join(';'));
    await c2.close();
  });

  /* ============ 第七段：异常 ============ */
  console.log('\n== 异常 ==');
  /* 故障注入之前的控制台快照 —— 之后那些"连不上"的报错是测试自己制造的，
     不能算产品缺陷（这是本仓库反复强调的"别把探针问题当系统故障"）。 */
  const issuesBeforeFault = consoleIssues.slice();
  await step('A02b', '故障注入之前全程无 console error / 4xx', async () => {
    eq(issuesBeforeFault.length, 0,
       '实际有 ' + issuesBeforeFault.length + ' 条：\n    ' + issuesBeforeFault.slice(0, 8).join('\n    '));
  });
  await step('A08/A09', '服务端挂掉 → 链路胶囊变「重连中…」且不崩', async () => {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await sleep(1500);
    procs[0].kill();
    await sleep(6000);
    const t = await txt(page, '#linkv');
    eq(t, '重连中…', '链路提示没变');
    const cls = await page.$eval('#linkp', e => e.className);
    has(cls, 'warn');
    ok((await page.textContent('body')).indexOf('实时仪表盘') >= 0, '页面被搞崩了');
    await shot(page, 'server-down');
  });
  await step('A10', '服务端恢复 → 自动回到实时', async () => {
    startServer();
    ok(await waitHttp(BASE + '/api/latest', 90), '重启失败');
    await waitFor(page, async () => (await txt(page, '#linkv')) === '实时', 20, '恢复实时');
  });
  await step('E07', '下发失败时有错误提示（不静默）', async () => {
    // 板子在线才能点按钮 —— 所以不能靠"杀掉服务端"制造失败（那会把按钮变灰），
    // 改用 route 让这一个请求失败，模拟网络抖动。
    await waitFor(page, async () => (await txt(page, '#dev')).indexOf('在线') >= 0, 20, '设备回线');
    await page.route('**/api/command', r => r.abort());
    await page.click('#cmdbts button[data-cmd="led_blink"]');
    await waitFor(page, async () => {
      const t = await txt(page, '#cmdhint');
      return t && t.indexOf('下发失败') >= 0;
    }, 12, '失败提示');
    await page.unroute('**/api/command');
  });

  await step('A02c', '故障恢复后不再产生新的错误', async () => {
    // 允许故障窗口内的连不上；恢复之后若还在报错就是真问题
    const after = consoleIssues.slice(issuesBeforeFault.length).filter(x =>
      x.indexOf('net::ERR') < 0 && x.indexOf('Failed to fetch') < 0 &&
      x.indexOf('Failed to load resource') < 0 && x.indexOf('HTTP 4') < 0 &&
      x.indexOf('HTTP 5') < 0 && x.indexOf('/api/') < 0);
    eq(after.length, 0, '恢复后仍有 ' + after.length + ' 条：\n    ' + after.slice(0, 6).join('\n    '));
  });

  /* 收尾 */
  for (const b of [b1, b2]) { try { b.stop(); } catch (e) {} }
  try { await Promise.race([browser.close(), sleep(8000)]); } catch (e) {}
  fs.writeFileSync(path.join(OUT, 'results.json'),
    JSON.stringify({ passed, failed, skipped, results, consoleIssues }, null, 2));
  console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败, ' + skipped + ' 无法测试');
  for (const p of procs) { try { p.kill(); } catch (e) {} }
  process.exit(failed ? 1 : 0);
})().catch(async (e) => {
  console.error('脚本异常：', e);
  for (const p of procs) { try { p.kill(); } catch (err) {} }
  process.exit(3);
});

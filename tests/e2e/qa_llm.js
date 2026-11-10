#!/usr/bin/env node
/*
 * QA · 大模型相关 UI 状态（TEST_PLAN D08 / D15 / D17）
 *
 * 为什么要单独一套：pending / 停止 / 计时这三个状态**只在真的调大模型时出现**，
 * 规则 AI 是同步返回的，页面上永远看不到。所以这里起一个故意慢 5 秒的假上游
 * （OpenAI 兼容 /v1/chat/completions），把服务端指过去。
 *
 * 用法：NODE_PATH=... node tests/e2e/qa_llm.js
 */
'use strict';
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const os = require('os');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const PORT = Number(process.env.QA_LLM_PORT || 8097);
const LLM_PORT = PORT + 1;
const BASE = 'http://127.0.0.1:' + PORT;
const OUT = path.join(ROOT, '_qa_shots', 'run');
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

let passed = 0, failed = 0, skipped = 0;
const results = [];
async function step(id, name, fn) {
  try { await fn(); passed++; results.push({ id, name, status: 'PASS' }); console.log('  \u2713 ' + id + ' ' + name); }
  catch (e) {
    failed++;
    const m = String(e.message || e).split('\n')[0];
    results.push({ id, name, status: 'FAIL', msg: m });
    console.log('  \u2717 ' + id + ' ' + name + '  \u2192  ' + m);
  }
}
const ok = (c, m) => { if (!c) throw new Error(m || '断言失败'); };
const eq = (a, b, m) => { if (a !== b) throw new Error((m || '') + '：期望 ' + JSON.stringify(b) + '，实际 ' + JSON.stringify(a)); };
const has = (s, sub, m) => { if (String(s).indexOf(sub) < 0) throw new Error((m || '') + '：期望含「' + sub + '」，实际「' + s + '」'); };
async function txt(page, sel) { const e = await page.$(sel); return e ? ((await e.textContent()) || '').trim() : null; }
async function waitFor(page, fn, sec, label) {
  const t0 = Date.now();
  while (Date.now() - t0 < (sec || 10) * 1000) { try { if (await fn()) return true; } catch (e) {} await sleep(200); }
  throw new Error('等待超时：' + (label || ''));
}

const procs = [];
function startServer() {
  const fd = fs.openSync(path.join(os.tmpdir(), 'rw1-qa-llm.log'), 'w');
  const env = Object.assign({}, process.env, {
    no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost',
    RW1_LLM_API_KEY: 'qa-key',
    RW1_LLM_BASE_URL: 'http://127.0.0.1:' + LLM_PORT + '/v1',
    RW1_LLM_MODEL: 'qa-model',
  });
  const p = spawn('python', ['-u', 'server/server.py', '--port', String(PORT),
    '--data-dir', path.join(os.tmpdir(), 'rw1-qa-llm-data'), '--retain-days', '0'],
    { cwd: ROOT, env, stdio: ['ignore', fd, fd] });
  try { fs.closeSync(fd); } catch (e) {}
  procs.push(p);
}

/* 假大模型：延迟 5 秒，返回固定回答 */
let llmCalls = 0;
function startLLM() {
  const srv = http.createServer((req, res) => {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      llmCalls++;
      setTimeout(() => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ choices: [{ message: { content: '这是慢速假大模型的回答。' } }] }));
      }, 5000);
    });
  });
  srv.listen(LLM_PORT, '127.0.0.1');
  return srv;
}

async function waitHttp(u, s) {
  const t0 = Date.now();
  while (Date.now() - t0 < s * 1000) { try { if ((await fetch(u)).ok) return true; } catch (e) {} await sleep(300); }
  return false;
}

function startBoard(dev) {
  let pending = null, stopped = false;
  async function tick() {
    if (stopped) return;
    const body = { device: dev, batch: [[0.02, 0.01, 1], [0.02, 0.01, 1]], x: 0.02, y: 0.01, z: 1, source: 'SC7A20', ask: false, o: 5 };
    if (pending) { body.result = { id: pending, ok: true, ms: 5, n: 2 }; pending = null; }
    try {
      const d = await (await fetch(BASE + '/api/telemetry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
      if (d.cmd) pending = d.cmd.id;
    } catch (e) {}
  }
  const t = setInterval(tick, 500); tick();
  return { stop() { stopped = true; clearInterval(t); } };
}

(async () => {
  const llm = startLLM();
  startServer();
  if (!await waitHttp(BASE + '/api/latest', 90)) { console.error('服务端起不来'); process.exit(2); }
  const board = startBoard('QA-LLM板');

  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--enable-unsafe-swiftshader'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await waitFor(page, async () => (await txt(page, '#dev')).indexOf('在线') >= 0, 25, '上线');
  await sleep(1500);

  await step('D08', '提问后进入 pending：卡片高亮 + 「生成中…」+ 停止按钮出现', async () => {
    await page.fill('#asktext', '今天天气怎么样');
    await page.keyboard.press('Enter');
    await waitFor(page, async () => (await txt(page, '#reply')) === '正在思考…', 8, '占位文案');
    ok(await page.$eval('#replybox', e => e.classList.contains('pending')), '缺少 .pending');
    has(await txt(page, '#mode'), '生成中');
    ok(await page.isVisible('#aicancel'), '停止按钮没出现');
    await page.screenshot({ path: path.join(OUT, 'llm-pending.png') });
  });

  await step('D15', '点「停止」→ 立刻停在「已停止」，迟到的回答被丢弃', async () => {
    await page.click('#aicancel');
    await waitFor(page, async () => (await txt(page, '#reply')) === '已停止', 6, '已停止');
    ok(!(await page.isVisible('#aicancel')), '停止后按钮还在');
    // 假上游 5 秒后才回话 —— 等它回来，确认没有把「已停止」盖掉
    await sleep(6500);
    eq(await txt(page, '#reply'), '已停止', '迟到的回答覆盖了「已停止」');
    ok(!(await page.$eval('#replybox', e => e.classList.contains('pending'))), '仍然是 pending');
  });

  await step('D17', '走大模型的回答完成后显示生成耗时', async () => {
    await page.fill('#asktext', '再说一次');
    await page.keyboard.press('Enter');
    await waitFor(page, async () => (await txt(page, '#reply')) === '这是慢速假大模型的回答。', 20, '真回答');
    ok(await page.isVisible('#timing'), '计时块没显示');
    const t = await txt(page, '#timing');
    has(t, '生成');
    ok(/生成 \d/.test(t), '计时文案：' + t);
    await page.screenshot({ path: path.join(OUT, 'llm-answer.png') });
  });

  await step('D08b', 'pending 期间「停止」与文字入口同时可用（不互相锁死）', async () => {
    await page.fill('#asktext', '再问一句');
    await page.keyboard.press('Enter');
    await waitFor(page, async () => await page.isVisible('#aicancel'), 8, 'pending');
    const enabled = await page.$eval('#asktext', e => !e.disabled);
    ok(enabled, '输入框在 pending 期间被禁用（用户无法改口）');
    await page.click('#aicancel');
    await sleep(800);
  });

  await step('D14b', '播报开关关闭时不念（speechSynthesis 无调用）', async () => {
    // headless 里没有语音引擎，这里只能验证"调用路径不被触发"：关掉播报后再提问，
    // speechSynthesis.speak 不应被调用（用桩替换）。
    await page.evaluate(() => {
      window.__spoke = 0;
      if (window.speechSynthesis) {
        window.speechSynthesis.speak = function () { window.__spoke++; };
      }
    });
    if ((await txt(page, '#ttsbtn')) !== '播报 关') await page.click('#ttsbtn');
    await page.fill('#asktext', '关播报测试');
    await page.keyboard.press('Enter');
    await sleep(7000);
    const n = await page.evaluate(() => window.__spoke || 0);
    eq(n, 0, '关掉播报后仍然调用了 speak()');
  });

  await step('A02', '大模型链路全程无 JS 报错', async () => {
    eq(errs.length, 0, errs.join('; '));
  });

  board.stop();
  try { await Promise.race([browser.close(), sleep(8000)]); } catch (e) {}
  llm.close();
  for (const p of procs) { try { p.kill(); } catch (e) {} }
  fs.writeFileSync(path.join(OUT, 'results-llm.json'),
    JSON.stringify({ passed, failed, skipped, results }, null, 2));
  console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); for (const p of procs) p.kill(); process.exit(3); });

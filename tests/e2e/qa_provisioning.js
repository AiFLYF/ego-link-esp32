#!/usr/bin/env node
/*
 * QA · 板端配网页（TEST_PLAN 的 O 段）
 *
 * 配网页是**编进固件**的一段 HTML 字符串（device/main/provisioning_page.h），
 * 真机要连板子热点才能打开。这里把那段字符串解析出来、用 mock 接口喂它 ——
 * 验的是页面本身（表单/校验/提示/响应式），接口的**固件实现**不在覆盖范围内。
 *
 * 用法：NODE_PATH=... node tests/e2e/qa_provisioning.js
 */
'use strict';
const { chromium } = require('playwright-core');
const http = require('http');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const PORT = Number(process.env.QA_PROV_PORT || 8088);
const OUT = path.join(ROOT, '_qa_shots', 'run');
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------- 从 C 头文件里把 PROV_PAGE_HTML 抽出来 ---------- */
function cUnescape(s) {
  return s.replace(/\\(.)/g, (m, c) =>
    ({ n: '\n', t: '\t', r: '\r', '"': '"', '\\': '\\', '0': '\0' }[c] !== undefined
      ? { n: '\n', t: '\t', r: '\r', '"': '"', '\\': '\\', '0': '\0' }[c] : c));
}
function extractPage() {
  let src = fs.readFileSync(path.join(ROOT, 'device/main/provisioning_page.h'), 'utf8');
  /* 必须先去掉 C 注释：注释里出现的中文引号（如 只写"没扫到"）会被下面的
     字符串正则当成字面量吃进去，拼出来的 HTML 里会多出裸文本 → JS 语法错误。
     （第一版就是这么挂的，表现为 "Unexpected identifier 'say'"。） */
  src = src.replace(/\/\*[\s\S]*?\*\//g, '');
  const start = src.indexOf('PROV_PAGE_HTML[] =');
  if (start < 0) throw new Error('找不到 PROV_PAGE_HTML');
  const body = src.slice(start);
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m, out = '';
  while ((m = re.exec(body))) out += cUnescape(m[1]);
  return out;
}

/* ---------- mock 服务端 ---------- */
let scanMode = 'ok';
let saveMode = 'ok';
const hits = [];
function startMock(html) {
  const srv = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    hits.push(u.pathname);
    const json = (o, code) => {
      res.writeHead(code || 200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(o));
    };
    if (u.pathname === '/') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(html);
    }
    if (u.pathname === '/scan') {
      if (scanMode === 'ok') {
        return json({ nets: [
          { ssid: '教室-2.4G', rssi: -42, open: false },
          { ssid: 'Office-5G', rssi: -71, open: false },
          { ssid: 'FreeWiFi', rssi: -80, open: true },
        ] });
      }
      if (scanMode === 'err') return json({ nets: [], err: '扫描失败（STA 接口未就绪）' });
      return json({ nets: [] });
    }
    if (u.pathname === '/testurl') {
      const url = u.searchParams.get('url') || '';
      if (url.indexOf('bad') >= 0) return json({ ok: false, reason: '连接超时' });
      return json({ ok: true, code: 200 });
    }
    if (u.pathname === '/save') {
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        if (saveMode === 'fail' || body.indexOf('wrongpass') >= 0) {
          return json({ ok: false, reason: 'WiFi 连接失败：密码错误' });
        }
        return json({ ok: true, ip: '192.168.1.42' });
      });
      return;
    }
    if (u.pathname === '/clear') return json({ ok: true });
    json({ ok: false, error: 'not found' }, 404);
  });
  srv.listen(PORT, '127.0.0.1');
  return srv;
}

/* ---------- 断言 ---------- */
let passed = 0, failed = 0;
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

(async () => {
  const html = extractPage();
  console.log('抽出配网页 HTML：' + html.length + ' 字节');
  const srv = startMock(html);
  const BASE = 'http://127.0.0.1:' + PORT;

  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server'] });
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  const errs = [], external = [];
  page.on('pageerror', e => errs.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errs.push('CONSOLE ' + m.text()); });
  page.on('request', r => {
    try { const h = new URL(r.url()).hostname; if (h !== '127.0.0.1' && h !== 'localhost') external.push(r.url()); } catch (e) {}
  });

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await sleep(1200);

  await step('O01', '打开页面自动扫描，下拉框填好 3 个网络', async () => {
    const opts = await page.$$eval('#ssid option', os => os.map(o => o.textContent));
    ok(opts.length >= 4, '选项数 ' + opts.length + '：' + opts.join('|'));
    has(opts.join('|'), '教室-2.4G');
    has(opts.join('|'), '开放');
    eq(opts[0], '（请选择）');
  });
  await step('O14', '零外部请求（配网时没有外网）', async () => {
    eq(external.length, 0, '请求了外部资源：' + external.join(', '));
  });
  await step('O04', '选下拉项会清空手动输入框', async () => {
    await page.fill('#ssid2', '手动网络');
    await page.selectOption('#ssid', { label: '教室-2.4G  (-42dBm)' });
    eq(await page.inputValue('#ssid2'), '');
  });
  await step('O06', '显示/隐藏密码', async () => {
    eq(await page.getAttribute('#pass', 'type'), 'password');
    await page.click('#pwb');
    eq(await page.getAttribute('#pass', 'type'), 'text');
    eq(await page.textContent('#pwb'), '隐藏');
    await page.click('#pwb');
    eq(await page.getAttribute('#pass', 'type'), 'password');
  });
  await step('O07', '空 URL 点测试连接 → 红字提示且不发请求', async () => {
    const before = hits.filter(h => h === '/testurl').length;
    await page.fill('#url', '');
    await page.click('text=测试连接');
    await sleep(300);
    has(await page.textContent('#msg'), '请先填服务器地址');
    eq(hits.filter(h => h === '/testurl').length, before, '空 URL 仍发了请求');
    ok(await page.$eval('#msg', e => e.classList.contains('err')), '不是红字样式');
  });
  await step('O08', '服务器可达 → 绿字', async () => {
    await page.fill('#url', 'http://192.168.1.100:8000');
    await page.click('text=测试连接');
    await sleep(600);
    has(await page.textContent('#msg'), '服务器可达');
    ok(await page.$eval('#msg', e => e.classList.contains('ok')), '不是绿字样式');
  });
  await step('O09', '服务器不可达 → 红字原因', async () => {
    await page.fill('#url', 'http://bad-host:8000');
    await page.click('text=测试连接');
    await sleep(600);
    has(await page.textContent('#msg'), '连不上');
    ok(await page.$eval('#msg', e => e.classList.contains('err')));
  });
  await step('O10', '保存成功 → 按钮禁用 → 显示本机 IP', async () => {
    await page.fill('#url', 'http://192.168.1.100:8000');
    await page.fill('#pass', 'goodpass');
    await page.click('#save');
    await sleep(900);
    has(await page.textContent('#msg'), '已连接');
    has(await page.textContent('#msg'), '192.168.1.42');
    eq(await page.textContent('#save'), '保存并连接', '按钮文案没恢复');
    ok(!(await page.$eval('#save', e => e.disabled)), '按钮仍是禁用');
  });
  await step('O11', '保存失败 → 红字原因，按钮可再点', async () => {
    await page.fill('#pass', 'wrongpass');
    await page.click('#save');
    await sleep(900);
    has(await page.textContent('#msg'), '密码错误');
    ok(await page.$eval('#msg', e => e.classList.contains('err')));
    ok(!(await page.$eval('#save', e => e.disabled)), '失败后按钮仍禁用（用户无法重试）');
  });
  await step('O12', '清除配置：原生 confirm 取消则不发 /clear', async () => {
    let dialogs = 0;
    page.on('dialog', async d => { dialogs++; await d.dismiss(); });
    const before = hits.filter(h => h === '/clear').length;
    await page.click('text=清除配置并重启');
    await sleep(400);
    eq(dialogs, 1, '没有弹 confirm');
    eq(hits.filter(h => h === '/clear').length, before, '取消后仍发了 /clear');
  });
  await step('O02', '扫描失败 → 红字提示可手动填写', async () => {
    scanMode = 'err';
    await page.click('text=重新扫描');
    await sleep(700);
    has(await page.textContent('#msg'), '扫描失败');
    const opts = await page.$$eval('#ssid option', os => os.map(o => o.textContent));
    has(opts.join('|'), '没扫到');
  });
  await step('O03', '扫描结果为空 → 提示手动填写', async () => {
    scanMode = 'empty';
    await page.click('text=重新扫描');
    await sleep(700);
    const opts = await page.$$eval('#ssid option', os => os.map(o => o.textContent));
    has(opts.join('|'), '没扫到');
  });
  await step('O05', '手动填的 SSID 优先于下拉框', async () => {
    scanMode = 'ok';
    await page.click('text=重新扫描');
    await sleep(700);
    await page.selectOption('#ssid', { index: 1 });
    await page.fill('#ssid2', '隐藏网络-5G');
    let sent = '';
    const h = r => { if (r.url().endsWith('/save')) sent = r.postData() || ''; };
    page.on('request', h);
    await page.click('#save');
    await sleep(800);
    page.off('request', h);
    has(decodeURIComponent(sent.replace(/\+/g, ' ')), 'ssid=隐藏网络-5G');
  });
  await step('O13', '375 宽不横向溢出', async () => {
    await page.setViewportSize({ width: 375, height: 812 });
    await sleep(400);
    const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth }));
    ok(m.sw <= m.iw + 1, 'scrollWidth ' + m.sw + ' > ' + m.iw);
    await page.screenshot({ path: path.join(OUT, 'prov-375.png'), fullPage: true });
  });
  await step('O15', '控件都有可读标签（label 或 aria-label）', async () => {
    const bad = await page.evaluate(() => {
      const ids = ['ssid', 'ssid2', 'pass', 'url', 'device', 'period'];
      return ids.filter(id => {
        const el = document.getElementById(id);
        if (!el) return true;
        const hasLabel = el.labels && el.labels.length > 0;
        return !hasLabel && !el.getAttribute('aria-label') && !el.getAttribute('placeholder');
      });
    });
    eq(bad.length, 0, '没有标签的控件：' + bad.join(','));
  });
  await step('A02', '配网页无 JS 报错', async () => {
    eq(errs.length, 0, errs.join('; '));
  });

  try { await Promise.race([browser.close(), sleep(6000)]); } catch (e) {}
  srv.close();
  fs.writeFileSync(path.join(OUT, 'results-prov.json'),
    JSON.stringify({ passed, failed, results, htmlBytes: html.length }, null, 2));
  console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(3); });

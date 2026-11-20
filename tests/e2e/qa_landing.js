#!/usr/bin/env node
/*
 * QA · 项目主页 docs/index.html（TEST_PLAN 的 Q 段）
 * 起一个静态服务器指向 docs/，用真浏览器验。
 * 用法：NODE_PATH=... node tests/e2e/qa_landing.js
 */
'use strict';
const { chromium } = require('playwright-core');
const http = require('http');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const DOCS = path.join(ROOT, 'docs');
const PORT = Number(process.env.QA_DOCS_PORT || 8087);
const BASE = 'http://127.0.0.1:' + PORT + '/index.html';
const OUT = path.join(ROOT, '_qa_shots', 'run');
fs.mkdirSync(OUT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.png': 'image/png',
               '.woff2': 'font/woff2', '.css': 'text/css', '.svg': 'image/svg+xml' };
function startStatic() {
  const srv = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    let p = path.join(DOCS, decodeURIComponent(u.pathname));
    if (!p.startsWith(DOCS)) { res.writeHead(403); return res.end(); }
    if (u.pathname === '/') p = path.join(DOCS, 'index.html');
    fs.readFile(p, (e, buf) => {
      if (e) { res.writeHead(404); return res.end('not found'); }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(p)] || 'application/octet-stream' });
      res.end(buf);
    });
  });
  srv.listen(PORT, '127.0.0.1');
  /* 端口被占时立刻炸，别去测别人的页面 */
  srv.on('error', e => { console.error('静态服务端口 ' + PORT + ' 不可用：' + e.message); process.exit(4); });
  return srv;
}

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
  const srv = startStatic();
  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--enable-unsafe-swiftshader'] });

  /* ---- 正常路径 ---- */
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 },
    permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await ctx.newPage();
  const errs = [], bad = [];
  page.on('pageerror', e => errs.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errs.push('CONSOLE ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) bad.push(r.status() + ' ' + r.url()); });

  await page.goto(BASE, { waitUntil: 'load' });
  await sleep(3000);

  await step('Q01', '加载遮罩淡出且不再拦截点击', async () => {
    const st = await page.evaluate(() => {
      const l = document.getElementById('loader');
      if (!l) return { gone: true };
      const s = getComputedStyle(l);
      const r = l.getBoundingClientRect();
      const hit = document.elementFromPoint(r.width / 2, r.height / 2);
      return { gone: false, opacity: Number(s.opacity), pe: s.pointerEvents,
               vis: s.visibility, hit: hit ? (hit.id || hit.tagName) : 'null' };
    });
    if (st.gone) return;
    ok(st.opacity < 0.05, '遮罩还看得见，opacity=' + st.opacity);
    ok(st.pe === 'none' || st.vis === 'hidden', '遮罩仍在拦截点击：pointer-events=' + st.pe + ' visibility=' + st.vis);
  });
  await step('Q02', '无 JS 报错 / 无 4xx', async () => {
    eq(errs.length, 0, errs.join('; '));
    eq(bad.length, 0, bad.join(', '));
  });
  await step('Q07', '4 张板端截图真的加载出来', async () => {
    // 图片是 loading="lazy"，必须先滚到可视区，否则 naturalWidth 恒为 0（踩过一次）
    await page.evaluate(() => document.getElementById('ui').scrollIntoView());
    await sleep(1200);
    const imgs = await page.$$eval('#ui img', is => is.map(i => ({ src: i.getAttribute('src'), w: i.naturalWidth })));
    eq(imgs.length, 4, '图片数');
    for (const i of imgs) ok(i.w > 0, i.src + ' 没加载（naturalWidth=0）');
  });
  await step('Q06', 'hero 数字滚到目标值', async () => {
    const nums = await page.$$eval('.stats .num', ns => ns.map(n => n.textContent.trim()));
    eq(nums.join(','), '500,16,8,0,180', '实际：' + nums.join(','));
  });
  await step('Q03', '6 个导航锚点都能滚到对应区块且标题不被顶栏遮住', async () => {
    const links = await page.$$eval('.links a', as => as.map(a => a.getAttribute('href')));
    eq(links.length, 6, '导航项数');
    for (const h of links) {
      await page.click(`.links a[href="${h}"]`);
      await sleep(900);
      const r = await page.evaluate(sel => {
        const el = document.querySelector(sel);
        const t = el.querySelector('h2');
        const rb = (t || el).getBoundingClientRect();
        return { top: Math.round(rb.top), h: Math.round(rb.height), id: el.id };
      }, h);
      ok(r.top >= 0, h + ' 的标题被滚到视口上方（top=' + r.top + '）');
      ok(r.top < 400, h + ' 的标题没滚进视口（top=' + r.top + '）');
    }
  });
  await step('Q05', '代码块「复制」真的写入剪贴板', async () => {
    const btn = await page.$('#quickstart .copy');
    ok(btn, '找不到复制按钮');
    const code = await page.evaluate(() => {
      const b = document.querySelector('#quickstart .copy');
      const block = b.closest('.code') || b.parentElement.parentElement;
      return (block.querySelector('pre') || block).innerText;
    });
    await btn.scrollIntoViewIfNeeded();
    await btn.click();
    await sleep(400);
    const clip = await page.evaluate(() => navigator.clipboard.readText().catch(() => ''));
    ok(clip && clip.length > 0, '剪贴板是空的');
    ok(code.indexOf(clip.trim().slice(0, 12)) >= 0 || clip.indexOf('python') >= 0,
       '剪贴板内容与代码块不符：' + clip.slice(0, 40));
    const label = await page.evaluate(() => {
      const b = document.querySelector('#quickstart .copy');
      return b.textContent;
    });
    has(label, '已复制');
  });
  await step('Q04', '回到顶部按钮：滚动后出现，点击回顶', async () => {
    await page.evaluate(() => window.scrollTo(0, 2000));
    await sleep(600);
    ok(await page.$eval('#top', e => e.classList.contains('on')), '滚动后按钮没出现');
    await page.click('#top');
    await sleep(1200);
    const y = await page.evaluate(() => window.scrollY);
    ok(y < 50, '点了没回到顶部，scrollY=' + y);
    ok(!(await page.$eval('#top', e => e.classList.contains('on'))), '回顶后按钮没隐藏');
  });
  await step('Q08', '375 宽无横向溢出', async () => {
    await page.setViewportSize({ width: 375, height: 812 });
    await sleep(900);
    const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth }));
    ok(m.sw <= m.iw + 1, 'scrollWidth ' + m.sw + ' > ' + m.iw);
    await page.screenshot({ path: path.join(OUT, 'landing-375.png'), fullPage: false });
  });
  await step('Q11', '直接访问 #api：该区块内容可见（滚动揭示被触发）', async () => {
    const p2 = await ctx.newPage();
    await p2.goto(BASE + '#api', { waitUntil: 'load' });
    await sleep(3000);
    const st = await p2.evaluate(() => {
      const sec = document.getElementById('api');
      const rv = sec.querySelector('.rv');
      const cs = rv ? getComputedStyle(rv) : null;
      const r = sec.getBoundingClientRect();
      return { op: cs ? Number(cs.opacity) : -1, top: Math.round(r.top) };
    });
    ok(st.op > 0.9, '#api 里的 .rv 还是透明的（opacity=' + st.op + '）');
    await p2.close();
  });
  await ctx.close();

  /* ---- 降级路径 ---- */
  const ctx2 = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' });
  const p3 = await ctx2.newPage();
  await p3.addInitScript(() => {
    // 模拟 WebGL 不可用
    try { Object.defineProperty(window, 'WebGLRenderingContext', { value: undefined, configurable: true }); } catch (e) {}
  });
  await p3.goto(BASE, { waitUntil: 'load' });
  await sleep(2000);
  await step('Q10', 'WebGL 不可用 → 显示降级文案，不空白', async () => {
    const st = await p3.evaluate(() => {
      const f = document.querySelector('.hero-fallback');
      const cs = getComputedStyle(f);
      return { display: cs.display, text: f.textContent.trim().slice(0, 20),
               cls: document.documentElement.className };
    });
    eq(st.display, 'flex', '降级文案没显示（display=' + st.display + '）');
    has(st.text, 'WebGL');
  });
  await step('Q09', 'prefers-reduced-motion：不滚动也能看到全部内容', async () => {
    const hidden = await p3.evaluate(() =>
      Array.from(document.querySelectorAll('.rv')).filter(e => Number(getComputedStyle(e).opacity) < 0.9).length);
    eq(hidden, 0, '还有 ' + hidden + ' 个 .rv 是透明的');
  });
  await ctx2.close();

  try { await Promise.race([browser.close(), sleep(8000)]); } catch (e) {}
  srv.close();
  fs.writeFileSync(path.join(OUT, 'results-landing.json'),
    JSON.stringify({ passed, failed, results }, null, 2));
  console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(3); });

#!/usr/bin/env node
/*
 * QA 阶段 3 用的「看页面」脚本：不写断言，只截图 + 收集控制台/网络问题，
 * 供人工（模型）核对视觉与交互。所有产物落到 _qa_shots/。
 *
 * 用法：
 *   NODE_PATH=... node tests/e2e/qa_shots.js [baseUrl] [prefix]
 */
'use strict';
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8090';
const PREFIX = process.argv[3] || 'dash';
const OUT = path.resolve(__dirname, '../../_qa_shots');
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function closeBrowser(b) {
  try { await Promise.race([b.close(), sleep(8000)]); } catch (e) {}
}
async function exitNow(code) {
  await Promise.race([
    new Promise(r => { try { process.stdout.write('\n', () => r()); } catch (e) { r(); } }),
    sleep(1500),
  ]);
  process.exit(code);
}

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--enable-unsafe-swiftshader'],
  });
  const issues = [];
  const viewports = [
    { name: '1440', width: 1440, height: 900 },
    { name: '768', width: 768, height: 1024 },
    { name: '375', width: 375, height: 812 },
  ];
  for (const theme of ['light', 'dark']) {
    for (const vp of viewports) {
      const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
      const page = await ctx.newPage();
      page.on('console', m => { if (m.type() === 'error') issues.push(`[${theme}/${vp.name}] CONSOLE ${m.text()}`); });
      page.on('response', r => { if (r.status() >= 400) issues.push(`[${theme}/${vp.name}] HTTP ${r.status()} ${r.url()}`); });
      page.on('pageerror', e => issues.push(`[${theme}/${vp.name}] PAGEERROR ${e.message}`));
      page.on('requestfailed', r => {
        const why = (r.failure() && r.failure().errorText) || '';
        if (why.indexOf('ERR_ABORTED') >= 0) return;
        issues.push(`[${theme}/${vp.name}] REQFAIL ${r.url()} (${why})`);
      });
      await page.addInitScript(t => {
        try { localStorage.setItem('rw1-theme', t); localStorage.setItem('rw1-tts', '0'); } catch (e) {}
      }, theme);
      await page.goto(BASE + (BASE.indexOf('.html') >= 0 ? '' : '/'), { waitUntil: 'domcontentloaded' });
      await sleep(3500);
      await page.screenshot({ path: path.join(OUT, `${PREFIX}-${theme}-${vp.name}.png`), fullPage: true });
      if (vp.name === '1440') {
        const m = await page.evaluate(() => ({
          docH: document.documentElement.scrollHeight,
          docW: document.documentElement.scrollWidth,
          winW: window.innerWidth,
          theme: document.documentElement.getAttribute('data-theme'),
          bodyBg: getComputedStyle(document.body).backgroundColor,
        }));
        console.log(`${theme}/${vp.name}: ${JSON.stringify(m)}`);
      }
      await ctx.close();
    }
  }
  console.log('\n=== 问题 ===');
  if (!issues.length) console.log('（无）');
  issues.forEach(x => console.log(x));
  await closeBrowser(browser);
  await exitNow(0);
})();

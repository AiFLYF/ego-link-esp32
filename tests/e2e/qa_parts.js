#!/usr/bin/env node
/* 逐区块高清截图：给每个选择器单独截一张，方便看清细节。
   用法：node tests/e2e/qa_parts.js [baseUrl] [prefix] [theme] [width] */
'use strict';
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8090';
const PREFIX = process.argv[3] || 'part';
const THEME = process.argv[4] || 'light';
const WIDTH = Number(process.argv[5] || 1440);
const OUT = path.resolve(__dirname, '../../_qa_shots');
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const PARTS = [
  ['appbar', 'header.appbar'],
  ['devcard', '#devcard'],
  ['stage', 'section.stage'],
  ['cmdbts', '.stage-actions'],
  ['cmdcard', '#cardcmd'],
  ['feedcard', '#cardfeed'],
  ['camcard', '#cardcam'],
  ['card3d', '#card3d'],
  ['cfgcard', '#cardcfg'],
  ['sdcard', '#cardsd'],
  ['footer', 'footer'],
];

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--enable-unsafe-swiftshader'],
  });
  const ctx = await browser.newContext({ viewport: { width: WIDTH, height: 1000 } });
  const page = await ctx.newPage();
  await page.addInitScript(t => { try { localStorage.setItem('rw1-theme', t); localStorage.setItem('rw1-tts', '0'); } catch (e) {} }, THEME);
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await sleep(2500);
  for (const [name, sel] of PARTS) {
    const el = await page.$(sel);
    if (!el) { console.log('MISSING ' + sel); continue; }
    const vis = await el.isVisible();
    if (!vis) { console.log('HIDDEN ' + sel); continue; }
    try {
      await el.screenshot({ path: path.join(OUT, `${PREFIX}-${THEME}-${WIDTH}-${name}.png`) });
      const box = await el.boundingBox();
      console.log(`${name}: ${Math.round(box.width)}x${Math.round(box.height)}`);
    } catch (e) { console.log('FAIL ' + name + ' ' + e.message); }
  }
  try { await Promise.race([browser.close(), sleep(6000)]); } catch (e) {}
  process.exit(0);
})();

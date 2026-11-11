#!/usr/bin/env node
/* 单个元素高清截图（deviceScaleFactor 可调），用来放大看细节。
   用法：node tests/e2e/qa_zoom.js <url> <selector> <outName> [theme] [width] [scale] */
'use strict';
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');
const BASE = process.argv[2];
const SEL = process.argv[3];
const NAME = process.argv[4] || 'zoom';
const THEME = process.argv[5] || 'light';
const WIDTH = Number(process.argv[6] || 1440);
const SCALE = Number(process.argv[7] || 2);
const OUT = path.resolve(__dirname, '../../_qa_shots');
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--enable-unsafe-swiftshader'],
  });
  const ctx = await browser.newContext({ viewport: { width: WIDTH, height: 1000 }, deviceScaleFactor: SCALE });
  const page = await ctx.newPage();
  await page.addInitScript(t => { try { localStorage.setItem('rw1-theme', t); localStorage.setItem('rw1-tts', '0'); } catch (e) {} }, THEME);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await sleep(2500);
  const el = await page.$(SEL);
  if (!el) { console.log('MISSING ' + SEL); process.exit(2); }
  await el.scrollIntoViewIfNeeded();
  await sleep(300);
  await el.screenshot({ path: path.join(OUT, NAME + '.png') });
  console.log('saved ' + NAME + '.png');
  try { await Promise.race([browser.close(), sleep(6000)]); } catch (e) {}
  process.exit(0);
})();

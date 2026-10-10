#!/usr/bin/env node
/*
 * 仪表盘「设计系统规则」验收（真浏览器）。
 *
 * 为什么需要它：`tools/e2e_dashboard.js` 只验功能（能点、能下发、无 4xx）。
 * 功能全绿而视觉悄悄退回系统字体 / 到处 16px 圆角 / hero 字号掉回 30px —— 这些
 * 它一个都测不出来。设计规则是硬约束，硬约束就得有断言，不能靠肉眼看截图。
 *
 * 它检查的每一条都对应一条明确规则：
 *   字体   —— 不是 Inter/Roboto/泛型 sans-serif，且 Sora/PlexMono **真的加载了**
 *             （font-family 写对了但字体没送到浏览器 = 静默退回系统字体，最阴的一种）
 *   颜色   —— 令牌用 OKLCH 定义；没有纯黑/纯白大面积；主色只有一个
 *   圆角   —— 全站非圆形圆角值不超过 3 种，且只能是 6 / 10 / 14
 *   字号   —— hero ≥ 正文 × 3；标题层级步进落在 1.25~1.5
 *   间距   —— 令牌严格落在 8px 基准网格上；section 上下 padding ≥ 64
 *   对齐   —— 标题默认左对齐，不居中
 *   布局   —— 1600/1280/900/390/320 五档都不横向溢出
 *   对比度 —— 关键文字/背景组合过 WCAG AA（正文 4.5、大字 3.0）
 *   弹窗   —— 破坏性操作真的弹自定义弹窗（不是 window.confirm），能开能关
 *   隐藏   —— 带 hidden 的元素必须真的 display:none（作者样式的 display 会盖掉它）
 *
 * 跑法：
 *   NODE_PATH="<node workspace>/node_modules" node tools/e2e_design.js
 * 自己起独立端口 + 假板子，不碰 8000。
 */
'use strict';

const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.E2E_PORT || 8097);
const BASE = 'http://127.0.0.1:' + PORT;
const PY = process.env.E2E_PYTHON || 'python';
const DATA_DIR = path.join(os.tmpdir(), 'rw1-design-data');
const SHOT_DIR = path.join(ROOT, '.workbuddy-ai', 'tmp');

const procs = [];
const logs = [];
function start(args, tag) {
  const env = Object.assign({}, process.env, {
    no_proxy: '127.0.0.1,localhost', NO_PROXY: '127.0.0.1,localhost',
  });
  const fs = require('fs');
  const logPath = path.join(SHOT_DIR, 'design-' + tag + '.log');
  const fd = fs.openSync(logPath, 'w');
  logs.push(logPath);
  const p = spawn(PY, args, { cwd: ROOT, env, stdio: ['ignore', fd, fd] });
  /* 父进程这一份 fd 必须关掉：spawn 已经把副本给子进程了，自己留着等于泄漏，
     也会让 node 在收尾时多一个未释放的句柄（2026-09-29 修）。 */
  try { fs.closeSync(fd); } catch (e) { /* 已关 */ }
  procs.push(p);
  return p;
}
const cleanup = () => { for (const p of procs) { try { p.kill(); } catch (e) { /* 没了 */ } } };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* 关浏览器可能挂住：仪表盘有 SSE 长连接，Chrome 关不掉时 browser.close() 会一直等。
   "跑完断言却卡在收尾"在 CI 里等于永不结束，所以给它一个上限。 */
async function closeBrowser(b) {
  try { await Promise.race([b.close(), sleep(8000)]); } catch (e) { /* 已经关了 */ }
}

/* 显式退出：playwright + spawn 出来的子进程会留下活跃 handle，事件循环不空，
   node 自己就永远不退 —— 实测跑完 29 条断言后进程仍挂着，被 timeout 杀掉
   （退出码 124）。flush 用 race 兜底：Windows 下 write('') 空写不触发回调，
   等它就会卡死（第一次修就踩了这个）。 */
async function exitNow(code) {
  await Promise.race([
    new Promise(r => { try { process.stdout.write('\n', () => r()); } catch (e) { r(); } }),
    sleep(1500),
  ]);
  process.exit(code);
}

/* 等 HTTP 真的能应答再往下走。
   固定 sleep 是**不可靠的**：服务端起得慢一点、或端口被占直接退出，
   后面的断言就会拿到"连接被拒"这种看起来像产品 bug 的假象。
   （踩过：某次 plexmono-600.woff2 报 ERR_CONNECTION_REFUSED，其实跟字体毫无关系。） */
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

const problems = [];
let passed = 0, skipped = 0;
async function step(name, fn) {
  try { await fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) {
    const msg = String(e.message || e).split('\n').slice(0, 3).join(' / ');
    console.log('  \u2717 ' + name + '  \u2192  ' + msg);
    problems.push('[' + name + '] ' + msg);
  }
}
function skip(name, why) { skipped++; console.log('  \u2013 ' + name + '（跳过：' + why + '）'); }
/* ---------------------------------------------------------------------------
 * 颜色与对比度是**唯一会随主题变化的规则**，所以抽成函数，切一次主题调一次。
 * 浅色主题恰恰是「禁纯白大面积」「主色只有一个」「对比度过 AA」最容易翻车的地方 ——
 * 只在深色下验过，等于没验。
 * ------------------------------------------------------------------------- */
/* page 在下面那个 IIFE 里才创建 —— 所以这里先声明，IIFE 里赋值。
   （模块级函数看不到 IIFE 的局部 const，直接引用会 ReferenceError。） */
let page = null;
function currentTheme(){
  return page.evaluate(() => document.documentElement.getAttribute('data-theme'));
}
function themeZh(t){ return t === 'dark' ? '深色' : '明亮'; }

/* 横向溢出时**报出是谁溢出的** —— 只说"溢出 44px"等于让下一个人从头查一遍。
   返回被撑出视口的元素（含祖先链和文字片段）。 */
async function overflowInfo(){
  return page.evaluate(() => {
    const docW = document.documentElement.clientWidth;
    const out = [];
    document.querySelectorAll('*').forEach(el => {
      const b = el.getBoundingClientRect();
      if (b.width < 1) return;
      if (b.right <= docW + 1) return;
      const cn = (el.className && typeof el.className === 'string')
        ? '.' + el.className.trim().split(/\s+/).join('.') : '';
      let chain = [], n = el;
      while (n && n !== document.body) {
        const c2 = (n.className && typeof n.className === 'string')
          ? '.' + n.className.trim().split(/\s+/)[0] : '';
        chain.push(n.tagName + (n.id ? '#' + n.id : '') + c2);
        n = n.parentElement;
      }
      out.push({ el: el.tagName + (el.id ? '#' + el.id : '') + cn,
                 w: Math.round(b.width), right: Math.round(b.right),
                 text: (el.textContent || '').slice(0, 50),
                 chain: chain.join(' < ') });
    });
    return { docW, scrollW: document.documentElement.scrollWidth, out: out.slice(0, 8) };
  });
}
async function assertNoOverflow(where){
  const r = await overflowInfo();
  const over = r.scrollW - r.docW;
  if (over <= 2) return over;
  throw new Error(where + '横向溢出 ' + over + 'px（视口 ' + r.docW
    + '）→ ' + JSON.stringify(r.out));
}

async function checkColors(){
  const th = themeZh(await currentTheme());
  console.log('     \u2014\u2014 主题：' + th + ' \u2014\u2014');
    await step('[' + th + '] 颜色令牌用 OKLCH 定义', async () => {
      const r = await page.evaluate(() => {
        const cs = getComputedStyle(document.documentElement);
        const names = ['--color-bg', '--color-surface', '--color-surface-2', '--color-line',
          '--color-text', '--color-text-muted', '--color-text-faint',
          '--color-action-primary', '--color-still', '--color-walk', '--color-move',
          '--color-shake', '--color-fall'];
        const out = {};
        for (const n of names) out[n] = cs.getPropertyValue(n).trim();
        return out;
      });
      const notOklch = Object.entries(r).filter(([, v]) => !/^oklch\(/.test(v));
      if (notOklch.length) throw new Error('不是 oklch 的令牌：' + JSON.stringify(notOklch));
      console.log('     13 个令牌全部 oklch：' + r['--color-bg'] + ' … ' + r['--color-action-primary']);
    });

    await step('[' + th + '] 没有纯黑 #000 / 纯白 #fff 大面积使用', async () => {
      const r = await page.evaluate((js) => {
        const toRGB = eval(js);
        const cs = getComputedStyle(document.documentElement);
        const get = n => cs.getPropertyValue(n).trim();
        const keys = ['--color-bg', '--color-surface', '--color-surface-2', '--color-surface-3',
                      '--color-text', '--color-line'];
        const out = {};
        for (const k of keys) out[k] = toRGB(get(k)).slice(0, 3);
        return out;
      }, TO_RGB);
      const pureBlack = Object.entries(r).filter(([k, v]) => k !== '--color-text'
        && v[0] === 0 && v[1] === 0 && v[2] === 0);
      const pureWhite = Object.entries(r).filter(([k, v]) => v[0] === 255 && v[1] === 255 && v[2] === 255);
      if (pureBlack.length) throw new Error('纯黑：' + JSON.stringify(pureBlack));
      if (pureWhite.length) throw new Error('纯白：' + JSON.stringify(pureWhite));
      console.log('     bg=' + r['--color-bg'] + '  surface=' + r['--color-surface']
                  + '  text=' + r['--color-text']);
    });

    await step('[' + th + '] 主色只有 1 个（action-primary 是唯一的"可操作"色）', async () => {
      const n = await page.evaluate(() => {
        const cs = getComputedStyle(document.documentElement);
        let c = 0;
        for (const s of cs) { if (/^--color-action-/.test(s) && !/-hi$|-ink$|-soft$/.test(s)) c++; }
        return c;
      });
      if (n !== 1) throw new Error('action 主色令牌数 = ' + n);
    });
}

async function checkContrast(){
  const th = themeZh(await currentTheme());
    await step('[' + th + '] 关键文字/背景组合过 WCAG AA', async () => {
      const r = await page.evaluate((args) => {
        const [toRGBJs, contrastJs] = args;
        const toRGB = eval(toRGBJs);
        const contrast = eval(contrastJs);
        const cs = getComputedStyle(document.documentElement);
        const t = n => cs.getPropertyValue(n).trim();
        const pairs = [
          // 卡片是 surface-2 → surface 的渐变，**最亮的那一端才是最坏情况**；
          // 只拿 surface 去算会得出偏乐观的数（第一版就这么漏掉了 0.6 个点）。
          ['正文 / 页面底', t('--color-text'), t('--color-bg'), 4.5],
          ['正文 / 卡片最亮处', t('--color-text'), t('--color-surface-2'), 4.5],
          ['次级文字 / 卡片最亮处', t('--color-text-muted'), t('--color-surface-2'), 4.5],
          ['次级文字 / 状态块底', t('--color-text-muted'), t('--color-surface-3'), 4.5],
          ['弱文字 / 卡片最亮处', t('--color-text-faint'), t('--color-surface-2'), 4.5],
          ['弱文字 / 页面底', t('--color-text-faint'), t('--color-bg'), 4.5],
          ['主按钮文字 / 主色', t('--color-action-primary-ink'), t('--color-action-primary'), 4.5],
          ['危险按钮文字 / 危险色', t('--color-danger-ink'), t('--color-danger'), 4.5],
          ['静置 / 页面底', t('--color-still'), t('--color-bg'), 4.5],
          ['步行 / 页面底', t('--color-walk'), t('--color-bg'), 4.5],
          ['运动 / 页面底', t('--color-move'), t('--color-bg'), 4.5],
          ['晃动 / 页面底', t('--color-shake'), t('--color-bg'), 4.5],
          ['跌落 / 页面底', t('--color-fall'), t('--color-bg'), 4.5],
        ];
        return pairs.map(([name, fg, bg, need]) => ({
          name, need, ratio: Math.round(contrast(toRGB(fg), toRGB(bg)) * 100) / 100,
        }));
      }, [TO_RGB, CONTRAST]);
      const fail = r.filter(x => x.ratio < x.need);
      for (const x of r) console.log('     ' + (x.ratio >= x.need ? '\u2713' : '\u2717')
        + ' ' + x.ratio.toFixed(2) + '  ' + x.name);
      if (fail.length) throw new Error('不达标：' + JSON.stringify(fail));
    });
}


/* 把任意 CSS 颜色解析成 sRGB —— 用 canvas 落一个像素读回来，
   这样 oklch()/color()/rgba() 全都能拿到真实分量，不用自己写解析。 */
const TO_RGB = `(function(){
  var c = document.createElement('canvas'); c.width = c.height = 1;
  var g = c.getContext('2d');
  return function(css){
    g.fillStyle = '#000'; g.fillRect(0,0,1,1);
    g.fillStyle = css;    g.fillRect(0,0,1,1);
    var d = g.getImageData(0,0,1,1).data;
    return [d[0], d[1], d[2], d[3] / 255];
  };
})()`;

/* 对比度（WCAG 2.x）。alpha 忽略：这里的组合都是不透明底。
   ⚠️ 必须写成**带括号的函数表达式** —— 裸 `function(){}` 在 eval 里是函数声明，
   没有名字会直接 SyntaxError（第一版就栽在这，测出来的"不达标"全是假的）。 */
const CONTRAST = `(function(rgb1, rgb2){
  function lum(c){ var f = c.slice(0,3).map(function(v){ v/=255;
    return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); });
    return 0.2126*f[0] + 0.7152*f[1] + 0.0722*f[2]; }
  var l1 = lum(rgb1), l2 = lum(rgb2);
  return (Math.max(l1,l2) + 0.05) / (Math.min(l1,l2) + 0.05);
})`;

(async () => {
  start(['-u', 'server/server.py', '--port', String(PORT),
         '--data-dir', DATA_DIR, '--retain-days', '0'], 'server');
  const up = await waitHttp(BASE + '/api/latest');
  if (!up) {
    throw new Error('服务端在 ' + PORT + ' 端口起不来（看 .workbuddy-ai/tmp/design-server.log）');
  }
  // 一块板子 + mixed 场景：有步数、有曲线、有事件，页面处于"正常有数据"的样子
  const board = start(['-u', 'tools/fake_board.py', '--url', BASE, '--device', '第三组-07',
         '--orient', '3', '--scenario', 'mixed', '--seconds', '240', '--quiet'], 'board');
  // 等板子真的上报（否则首屏是"等待数据…"，字号/对比度断言仍成立但截图不好看）
  const t0 = Date.now();
  while (Date.now() - t0 < 15000) {
    try {
      const j = await (await fetch(BASE + '/api/latest')).json();
      if (j && j.device_online) break;
    } catch (e) { /* 再等 */ }
    await sleep(500);
  }
  await sleep(3000);

  const browser = await chromium.launch({
    channel: 'chrome', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
           '--enable-unsafe-swiftshader'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  page = await ctx.newPage();

  page.on('console', m => { if (m.type() === 'error') problems.push('CONSOLE: ' + m.text()); });
  page.on('pageerror', e => problems.push('PAGEERROR: ' + e.message));
  page.on('requestfailed', r => problems.push('REQFAIL: ' + r.url()));
  const fontHits = [];
  page.on('response', r => {
    if (r.url().indexOf('/vendor/fonts/') >= 0) fontHits.push([r.status(), r.url()]);
    else if (r.status() >= 400 && r.url().indexOf('favicon') < 0) {
      problems.push('HTTP ' + r.status() + ' ' + r.url());
    }
  });

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => {
    const a = document.getElementById('act');
    return a && a.textContent && a.textContent !== '\u2026';
  }, { polling: 200, timeout: 30000 }).catch(() => {});
  await sleep(3000);

  console.log('\n【字体】');
  await step('页面真的请求了自托管字体，且全部 200', async () => {
    if (!fontHits.length) throw new Error('一个 /vendor/fonts/ 请求都没有 —— @font-face 没生效');
    const bad = fontHits.filter(h => h[0] !== 200);
    if (bad.length) throw new Error('有非 200：' + JSON.stringify(bad));
    const names = [...new Set(fontHits.map(h => h[1].split('/').pop()))];
    console.log('     请求到 ' + fontHits.length + ' 个字体文件：' + names.join(', '));
  });

  await step('Sora / PlexMono 真的加载成功（不是回落到系统字体）', async () => {
    const r = await page.evaluate(async () => {
      await document.fonts.ready;
      return {
        sora400: document.fonts.check('400 15px Sora'),
        sora600: document.fonts.check('600 19px Sora'),
        sora700: document.fonts.check('700 48px Sora'),
        mono400: document.fonts.check('400 12px PlexMono'),
        mono600: document.fonts.check('600 24px PlexMono'),
        loaded: [...document.fonts].map(f => f.family + '/' + f.weight + '/' + f.status),
      };
    });
    if (!r.sora400 || !r.sora600 || !r.sora700) throw new Error('Sora 没加载：' + JSON.stringify(r));
    if (!r.mono400 || !r.mono600) throw new Error('PlexMono 没加载：' + JSON.stringify(r));
    console.log('     ' + r.loaded.join('  '));
  });

  await step('font-family 里没有 Inter / Roboto / 泛型 sans-serif 兜底', async () => {
    const r = await page.evaluate(() => {
      const g = (sel) => getComputedStyle(document.querySelector(sel)).fontFamily;
      return { body: g('body'), hero: g('.hero-word'), h2: g('.region-head h2'),
               mono: g('.mono'), btn: g('button') };
    });
    const banned = ['inter', 'roboto', 'helvetica', 'arial'];
    for (const [k, v] of Object.entries(r)) {
      const low = v.toLowerCase();
      for (const b of banned) {
        if (low.indexOf(b) >= 0) throw new Error(k + ' 里出现禁用字体 ' + b + '：' + v);
      }
      // 泛型 sans-serif 只允许作为整条栈的最后一档（前面必须点名到具体字体）
      const parts = v.split(',').map(s => s.trim().replace(/^["']|["']$/g, ''));
      if (parts[parts.length - 1] === 'sans-serif' && parts.length < 3) {
        throw new Error(k + ' 直接落到泛型 sans-serif：' + v);
      }
    }
    console.log('     body: ' + r.body);
    console.log('     mono: ' + r.mono);
  });

  await checkColors();

  console.log('\n【圆角】');
  await step('全站非圆形圆角值 ≤ 3 种，且只有 6 / 10 / 14', async () => {
    const r = await page.evaluate(() => {
      const sels = ['.pill', 'button', 'input', 'select', '.dev', '.st', '.card', '.reply',
        '.shot', '.camview', '#cv', '.bar .t', '.legend i', '.brand .mark', '.item .name',
        '.region', '.stage', '.appbar'];
      const seen = {};
      for (const s of sels) {
        const el = document.querySelector(s);
        if (!el) continue;
        const cs = getComputedStyle(el);
        for (const p of ['borderTopLeftRadius', 'borderTopRightRadius',
                         'borderBottomLeftRadius', 'borderBottomRightRadius']) {
          const v = cs[p];
          if (!v || v === '0px') continue;
          if (v === '50%') continue;                 // 圆形是形状，不占圆角额度
          seen[v] = (seen[v] || []).concat(s);
        }
      }
      return seen;
    });
    const vals = Object.keys(r);
    const allowed = ['6px', '10px', '14px'];
    const bad = vals.filter(v => allowed.indexOf(v) < 0);
    if (bad.length) throw new Error('出现了额度外的圆角：' + JSON.stringify(bad.map(v => v + ' @ ' + r[v])));
    if (vals.length > 3) throw new Error('圆角值有 ' + vals.length + ' 种：' + JSON.stringify(vals));
    console.log('     ' + vals.map(v => v + '（' + r[v].join('/') + '）').join('  '));
  });

  console.log('\n【字号与间距】');
  await step('hero 字号 ≥ 正文 × 3，且标题层级步进 1.25~1.5', async () => {
    const r = await page.evaluate(() => {
      const px = s => parseFloat(getComputedStyle(document.querySelector(s)).fontSize);
      return { body: px('body'), hero: px('.hero-word'), region: px('.region-head h2'),
               card: px('.card-head h3') };
    });
    const ratio = r.hero / r.body;
    if (ratio < 3) throw new Error('hero/正文 = ' + ratio.toFixed(2) + '（要求 ≥ 3）');
    /* 标题阶梯：正文 → 卡片标题 → 区域标题。
       hero 是**展示级字号**（规则单独要求它 ≥ 正文×3），不参与这条 1.25~1.5 的链 ——
       它天生要跳一大步，否则就压不住整页。 */
    const ladder = [r.body, r.card, r.region].sort((a, b) => a - b);
    const steps = [];
    for (let i = 1; i < ladder.length; i++) steps.push(ladder[i] / ladder[i - 1]);
    const bad = steps.filter(s => s < 1.25 - 1e-6 || s > 1.5 + 1e-6);
    if (bad.length) throw new Error('标题步进越界 ' + JSON.stringify(steps.map(s => +s.toFixed(3))));
    /* 层级也不能太多：一屏里字号种类越多越"乱"。 */
    if (ladder.length > 3) throw new Error('标题层级有 ' + ladder.length + ' 级：' + JSON.stringify(ladder));
    console.log('     正文 ' + r.body + ' → ' + ladder.slice(1).join(' → ')
                + ' → hero ' + r.hero + '（hero/正文 = ' + ratio.toFixed(2) + '×）');
    console.log('     标题步进 ' + steps.map(s => s.toFixed(3) + '×').join('  '));
  });

  await step('间距令牌全部落在 8px 基准网格上', async () => {
    const r = await page.evaluate(() => {
      const cs = getComputedStyle(document.documentElement);
      const want = { '--sp-1': 4, '--sp-2': 8, '--sp-3': 12, '--sp-4': 16, '--sp-6': 24,
                     '--sp-8': 32, '--sp-12': 48, '--sp-16': 64, '--sp-24': 96 };
      const out = {};
      for (const k of Object.keys(want)) out[k] = [cs.getPropertyValue(k).trim(), want[k]];
      return out;
    });
    const bad = Object.entries(r).filter(([, v]) => parseFloat(v[0]) !== v[1]);
    if (bad.length) throw new Error('间距令牌不对：' + JSON.stringify(bad));
    console.log('     ' + Object.entries(r).map(([k, v]) => v[0]).join(' / '));
  });

  await step('每个 section 上下 padding ≥ 64px', async () => {
    const r = await page.evaluate(() => {
      const out = {};
      for (const s of ['.stage', '.region']) {
        const el = document.querySelector(s);
        if (!el) continue;
        const cs = getComputedStyle(el);
        out[s] = [parseFloat(cs.paddingTop), parseFloat(cs.paddingBottom)];
      }
      return out;
    });
    for (const [k, v] of Object.entries(r)) {
      if (v[0] < 64 || v[1] < 64) throw new Error(k + ' 上下 padding = ' + JSON.stringify(v) + '（要求都 ≥ 64）');
    }
    console.log('     ' + Object.entries(r).map(([k, v]) => k + ' ' + v[0] + '/' + v[1]).join('  '));
  });

  console.log('\n【对齐与布局】');
  await step('标题默认左对齐（没有居中大标题）', async () => {
    const r = await page.evaluate(() => {
      const out = {};
      for (const s of ['.hero-word', '.region-head h2', '.card-head h3', '.panel-title', '.eyebrow']) {
        const el = document.querySelector(s);
        if (el) out[s] = getComputedStyle(el).textAlign;
      }
      return out;
    });
    const centered = Object.entries(r).filter(([, v]) => v === 'center');
    if (centered.length) throw new Error('被居中的标题：' + JSON.stringify(centered));
    console.log('     ' + JSON.stringify(r));
  });

  await step('1600/1280/900/390/320 五档都不横向溢出', async () => {
    for (const w of [1600, 1280, 900, 390, 320]) {
      await page.setViewportSize({ width: w, height: 900 });
      await sleep(600);
      await assertNoOverflow(w + 'px 下');
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    await sleep(400);
  });

  await step('带 hidden 的元素真的不显示（作者 display 没盖掉它）', async () => {
    const bad = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('[hidden]').forEach(el => {
        if (getComputedStyle(el).display !== 'none') {
          out.push((el.id || el.className) + ' → display:' + getComputedStyle(el).display);
        }
      });
      return out;
    });
    if (bad.length) throw new Error('hidden 失效：' + JSON.stringify(bad));
  });

  await step('主操作按钮只有一个"实心"样式（其余都退下去）', async () => {
    const r = await page.evaluate((js) => {
      const toRGB = eval(js);
      const cs = getComputedStyle(document.documentElement);
      const prim = toRGB(cs.getPropertyValue('--color-action-primary').trim());
      const btns = [...document.querySelectorAll('#cmdbts button, .card button, .stage-actions button')]
        .filter(b => b.offsetParent !== null);
      const solid = btns.filter(b => {
        const bg = toRGB(getComputedStyle(b).backgroundColor);
        return Math.abs(bg[0] - prim[0]) < 6 && Math.abs(bg[1] - prim[1]) < 6 && Math.abs(bg[2] - prim[2]) < 6;
      });
      return { total: btns.length, solid: solid.map(b => b.textContent.trim()) };
    }, TO_RGB);
    if (r.solid.length !== 1) throw new Error('实心主色按钮有 ' + r.solid.length + ' 个：' + JSON.stringify(r.solid));
    console.log('     ' + r.total + ' 个按钮里，实心的只有「' + r.solid[0] + '」');
  });

  await step('主操作在首屏内（900px 高的笔记本也不用滚）', async () => {
    /* 规则要求"每个页面主操作必须跳出来"。整条 hero 带子约 900px 高，
       主操作原来是**排在曲线之后**的 —— 在 900px 视口下正好被切掉。
       现在把它移到曲线之前，这条断言把位置锁住。 */
    await page.setViewportSize({ width: 1440, height: 900 });
    await sleep(900);
    const r = await page.evaluate(() => {
      const b = document.querySelector('#cmdbts button.primary');
      if (!b) return null;
      const bb = b.getBoundingClientRect();
      return { top: Math.round(bb.top), bottom: Math.round(bb.bottom),
               innerH: window.innerHeight, text: b.textContent.trim() };
    });
    await page.setViewportSize({ width: 1600, height: 1000 });
    await sleep(400);
    if (!r) throw new Error('找不到主操作按钮');
    if (r.bottom > r.innerH) {
      throw new Error('主操作「' + r.text + '」在首屏之外：底边 ' + r.bottom + ' > 视口 ' + r.innerH);
    }
    console.log('     1440x900 下主操作「' + r.text + '」底边 ' + r.bottom + 'px / 视口 ' + r.innerH + 'px');
  });

  await step('破坏性指令和安全指令分开（不挨着排）', async () => {
    const r = await page.evaluate(() => {
      const bar = document.getElementById('cmdbts');
      const btns = [...bar.querySelectorAll('button')];
      const i = btns.findIndex(b => b.dataset.cmd === 'sd_format');
      const danger = i >= 0 ? btns[i] : null;
      const prev = i > 0 ? btns[i - 1] : null;
      return {
        hasGap: !!bar.querySelector('.cmd-gap'),
        sep: (danger && prev)
          ? Math.round(danger.getBoundingClientRect().left - prev.getBoundingClientRect().right) : -1,
        prevName: prev ? (prev.textContent || '').trim() : null,
        dangerName: danger ? (danger.textContent || '').trim() : null,
      };
    });
    if (!r.hasGap) throw new Error('指令栏里没有分组间隔 .cmd-gap');
    if (r.sep < 24) {
      throw new Error('「' + r.dangerName + '」离「' + r.prevName + '」只有 ' + r.sep + 'px，太近（误点代价太大）');
    }
    console.log('     「' + r.dangerName + '」与「' + r.prevName + '」相距 ' + r.sep + 'px');
  });

  await checkContrast();

  console.log('\n【弹窗】');
  await step('破坏性操作弹自定义弹窗（不是 window.confirm），能开能关', async () => {
    let nativeCalled = false;
    page.on('dialog', async d => { nativeCalled = true; await d.dismiss(); });
    const sentBefore = [];
    page.on('request', r => {
      if (r.method() === 'POST' && r.url().indexOf('/api/command') >= 0) sentBefore.push(r.postData());
    });
    await page.click('#cmdbts button[data-cmd="sd_format"]');
    await page.waitForSelector('dialog.modal[open]', { timeout: 5000 });
    if (nativeCalled) throw new Error('弹的是原生 window.confirm');
    const title = await page.textContent('#cfm-title');
    if (title.indexOf('格式化') < 0) throw new Error('弹窗标题不对：' + title);
    // 取消 → 关闭，且**不能**下发任何指令
    // 注意：dialog 关闭后**仍留在 DOM 里**，只是没有 open 属性 —— 不能等 detached
    await page.click('#cfm-cancel');
    await page.waitForFunction(() => !document.querySelector('dialog.modal[open]'),
                              { polling: 100, timeout: 5000 });
    await sleep(500);
    if (sentBefore.length) throw new Error('取消之后居然还是下发了：' + JSON.stringify(sentBefore));
    // Esc 也能关（原生 dialog 行为）
    await page.click('#cmdbts button[data-cmd="sd_format"]');
    await page.waitForSelector('dialog.modal[open]', { timeout: 5000 });
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('dialog.modal[open]'),
                              { polling: 100, timeout: 5000 });
    console.log('     标题「' + title + '」，取消/Esc 都能关，且取消时不下发指令');
  });

  console.log('\n【主题】');
  await step('默认是明亮主题（用户 2026-09-26 要求）', async () => {
    const th = await currentTheme();
    if (th !== 'light') throw new Error('默认主题是 ' + th + '，要求 light');
    const stored = await page.evaluate(() => {
      try { return localStorage.getItem('rw1-theme'); } catch (e) { return null; }
    });
    if (stored) throw new Error('全新会话不该有已存的主题偏好，实际 = ' + stored);
  });

  await step('摄像头没有帧时不显示"图片裂了"的破图标', async () => {
    const r = await page.evaluate(() => {
      const im = document.getElementById('camimg');
      return { hidden: im.hidden, src: im.getAttribute('src'), alt: im.getAttribute('alt') };
    });
    if (!r.hidden) throw new Error('#camimg 在没有帧时应该 hidden，实际 src=' + r.src);
    if (r.alt) throw new Error('alt 必须为空，否则破图时浏览器会把 alt 文字画出来：' + r.alt);
  });

  await step('切到深色：属性 / 存储 / 令牌 / 图标全都真的变了', async () => {
    const before = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--color-bg').trim());
    await page.click('#theme');
    await sleep(900);
    const r = await page.evaluate(() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      stored: (() => { try { return localStorage.getItem('rw1-theme'); } catch (e) { return null; } })(),
      bg: getComputedStyle(document.documentElement).getPropertyValue('--color-bg').trim(),
      title: (document.getElementById('theme') || {}).title,
      sun: getComputedStyle(document.querySelector('#theme .i-sun')).display !== 'none',
      moon: getComputedStyle(document.querySelector('#theme .i-moon')).display !== 'none',
    }));
    if (r.theme !== 'dark') throw new Error('切完 data-theme = ' + r.theme);
    if (r.stored !== 'dark') throw new Error('没写进 localStorage：' + r.stored);
    if (r.bg === before) throw new Error('--color-bg 没变 —— 浅色令牌没生效');
    if (!r.sun || r.moon) throw new Error('深色下图标不对：sun=' + r.sun + ' moon=' + r.moon);
    console.log('     --color-bg ' + before + ' \u2192 ' + r.bg + '；按钮：' + r.title);
    await page.screenshot({ path: path.join(SHOT_DIR, 'design-dark.png') });
    console.log('     已写入 .workbuddy-ai/tmp/design-dark.png');
  });

  await checkColors();
  await checkContrast();

  await step('切回明亮，且刷新后仍记得选择（无闪烁 = head 内联脚本生效）', async () => {
    await page.click('#theme');
    await sleep(700);
    if (await currentTheme() !== 'light') throw new Error('切不回明亮');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await sleep(3000);
    const r = await page.evaluate(() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      sun: getComputedStyle(document.querySelector('#theme .i-sun')).display !== 'none',
    }));
    if (r.theme !== 'light') throw new Error('刷新后主题丢了：' + r.theme);
    if (r.sun) throw new Error('明亮下不该显示太阳图标');
  });

  console.log('\n【截图】');
  await page.setViewportSize({ width: 1600, height: 1000 });
  await sleep(1200);
  await page.screenshot({ path: path.join(SHOT_DIR, 'design-desktop.png') });
  await page.screenshot({ path: path.join(SHOT_DIR, 'design-desktop-full.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await sleep(1200);
  await page.screenshot({ path: path.join(SHOT_DIR, 'design-mobile.png'), fullPage: true });
  console.log('     已写入 .workbuddy-ai/tmp/design-{desktop,desktop-full,mobile}.png');

  console.log('\n【离线空状态】');
  await step('板子离线时，设计规则同样成立（不塌陷、不掉字号、按钮不消失）', async () => {
    /* 这是**用户实际最常看到的那一屏**（还没开机 / 板子掉线）。
       2026-09-26 用户截图认可的就是这个状态 —— 而上面所有断言都跑在有数据时，
       空状态从来没被验过。空状态最容易出的问题是"数据没了整块塌掉"、
       "字号跟着数据一起缩水"、"按钮因为 disabled 干脆不渲染"。 */
    try { board.kill(); } catch (e) { /* 已经没了 */ }
    await sleep(8000);                       // 服务端 DEVICE_TIMEOUT = 5s，等它判离线
    /* 前面截手机图时把视口留在了 390px —— 必须先恢复，
       否则下面量到的是移动端字号，会把"手机端规则"误当成"桌面端塌了"。 */
    await page.setViewportSize({ width: 1600, height: 1000 });
    await sleep(800);
    const r = await page.evaluate(() => {
      const px = s => { const el = document.querySelector(s); return el ? parseFloat(getComputedStyle(el).fontSize) : 0; };
      const stage = document.querySelector('.stage').getBoundingClientRect();
      return {
        hero: px('.hero-word'),
        heroText: (document.getElementById('act') || {}).textContent,
        region: px('.region-head h2'),
        card: px('.card-head h3'),
        stageH: Math.round(stage.height),
        devPill: (document.getElementById('dev') || {}).className,
        // 按钮必须**还在**（只是 disabled）—— 设备离线不是把操作区藏起来的理由
        cmdButtons: document.querySelectorAll('#cmdbts button').length,
        cmdDisabled: [...document.querySelectorAll('#cmdbts button')].every(b => b.disabled),
        // 曲线画布也不能塌成 0 高
        cvH: document.getElementById('cv').getBoundingClientRect().height,
      };
    });
    if (r.devPill.indexOf('off') < 0) throw new Error('板子没被判离线：' + r.devPill);
    if (r.hero < 45) throw new Error('离线时 hero 字号缩水到 ' + r.hero);
    if (r.heroText !== '等待数据' && r.heroText !== '等待') {
      throw new Error('离线时活动词应回到等待态，实际：' + r.heroText);
    }
    if (Math.abs(r.region - 24) > 0.5 || Math.abs(r.card - 19) > 0.5) {
      throw new Error('离线时标题字号变了：区域 ' + r.region + ' / 卡片 ' + r.card);
    }
    if (r.stageH < 400) throw new Error('离线时 hero 舞台塌了：' + r.stageH + 'px');
    if (r.cmdButtons < 1) throw new Error('离线时指令按钮整个消失了');
    if (!r.cmdDisabled) throw new Error('离线时指令按钮居然是可点的');
    if (r.cvH < 100) throw new Error('离线时曲线画布塌了：' + r.cvH + 'px');
    console.log('     hero ' + r.hero + 'px「' + r.heroText + '」· 舞台 ' + r.stageH
                + 'px · 曲线 ' + r.cvH + 'px · ' + r.cmdButtons + ' 个按钮全部 disabled');
    await page.screenshot({ path: path.join(SHOT_DIR, 'design-offline.png') });
    console.log('     已写入 .workbuddy-ai/tmp/design-offline.png');

    /* 窄屏的 hero 也必须 ≥ 正文 × 3 —— 这条曾经真的被违反过：
       手机端把 hero 缩到 24px（= 1.6 倍正文）来省空间，规则直接破了。
       正解是上下堆叠而不是缩字号。这里把它锁死，防止再被"优化"回去。 */
    await page.setViewportSize({ width: 320, height: 800 });
    await sleep(800);
    const m = await page.evaluate(() => ({
      hero: parseFloat(getComputedStyle(document.querySelector('.hero-word')).fontSize),
      body: parseFloat(getComputedStyle(document.body).fontSize),
    }));
    if (m.hero / m.body < 3) {
      throw new Error('窄屏 hero/正文 = ' + (m.hero / m.body).toFixed(2) + '（' + m.hero + '/' + m.body + '），要求 ≥ 3');
    }
    await assertNoOverflow('320px 离线状态下');
    console.log('     320px 窄屏：hero ' + m.hero + 'px / 正文 ' + m.body + 'px = '
                + (m.hero / m.body).toFixed(2) + '×，无溢出');
  });

  await step('服务端全程没挂（否则上面的"请求失败"全是它引起的假象）', async () => {
    const alive = await waitHttp(BASE + '/api/latest', 3);
    if (!alive) throw new Error('服务端已经不应答了，看 .workbuddy-ai/tmp/design-server.log');
  });

  await step('全程没有 JS 报错 / 请求失败 / 4xx', async () => {
    if (problems.length) throw new Error(problems.slice(0, 5).join(' | '));
  });

  await closeBrowser(browser);
  cleanup();
  console.log('\n结果: ' + passed + ' 通过'
    + (skipped ? ', ' + skipped + ' 跳过' : '')
    + (problems.length ? ', ' + problems.length + ' 处问题' : ', 无问题'));
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

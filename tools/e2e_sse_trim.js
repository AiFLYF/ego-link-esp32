/* SSE 裁剪后的前端回归：两处"只发最近 N 条"带来的逻辑是否还成立。
 *
 * 覆盖：
 *   1. 事件计数不再恒为 40（服务端发 ev_total=137，40 条可见 → 文案要体现 137）
 *   2. 存储面板不会因为"后面又发了 8 条命令"而退回"还没读过"
 *
 * 手法：用 addInitScript 把 window.EventSource 换成一个可以由测试驱动的桩，
 * 于是页面走的是**自己的真实渲染路径**（render → renderEvents/renderSd），
 * 只是数据源被我们接管 —— 和 README 里"拦截 fetch 喂数据"是同一招。
 *
 * 跑法：NODE_PATH=<node_modules> E2E_PORT=8123 node tools/e2e_sse_trim.js
 */
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = parseInt(process.env.E2E_PORT || '8123', 10);

function startServer() {
  const p = spawn(process.env.PYTHON || 'python', ['-u', 'server/server.py',
    '--port', String(PORT), '--data-dir', path.join(ROOT, '_local_tmp', 'e2e_sse')],
    { cwd: ROOT, stdio: 'ignore', env: { ...process.env, no_proxy: '127.0.0.1,localhost' } });
  return p;
}

async function waitReady() {
  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/api/devices`);
      if (r.ok) return;
    } catch (e) { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error('服务端没起来');
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? '  — ' + detail : ''}`);
}

function frame(over) {
  return Object.assign({
    device: 'E2E-SSE', device_online: true, source: 'IMU', orient: 0,
    sample_hz: 100, posts_ok: 1, step_count: 0, shake_count: 0,
    activity: '静置·水平', ai_mode: '规则AI', ai_pending: false, ai_reply: '',
    voice_state: 'idle', voice_text: '', voice_err: '',
    voice_ms: 0, ai_ms: 0, total_ms: 0,
    latest: [Date.now() / 1000, 0, 0, 1],
    devices: [{ id: 'E2E-SSE', online: true, age: 0.1, source: 'IMU', orient: 0,
                activity: '静置·水平', sample_hz: 100, step_count: 0, shake_count: 0, posts_ok: 1 }],
    events: [], commands: [], now: Date.now() / 1000,
  }, over);
}

(async () => {
  const srv = startServer();
  let browser;
  try {
    await waitReady();
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    const page = await browser.newPage();

    // 把 EventSource 换成测试可驱动的桩；页面自己的 stream() 会 new 它
    await page.addInitScript(() => {
      class StubES {
        constructor(url) { this.url = url; this.onmessage = null; this.onerror = null; window.__es = this; }
        close() {}
      }
      window.EventSource = StubES;
      window.__push = (obj) => { if (window.__es && window.__es.onmessage) window.__es.onmessage({ data: JSON.stringify(obj) }); };
    });

    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => window.__es !== undefined, null, { timeout: 10000 });

    /* ---- 1. 事件计数：40 条可见，但缓冲里共 137 条 ---- */
    const events = Array.from({ length: 40 }, (_, i) => ({
      ts: Date.now() / 1000 - i, kind: 'info', text: '事件 ' + i,
    }));
    await page.evaluate(f => window.__push(f), frame({ events, ev_total: 137 }));
    await page.waitForTimeout(150);
    let txt = await page.$eval('#evcount', el => el.textContent);
    check('事件计数体现真实总条数', /137/.test(txt), `文案「${txt}」`);

    /* ---- 2. 存储面板：先来一条成功的 sd_ls ---- */
    const sdCmd = {
      id: 'c-1', name: 'sd_ls', state: 'done', source: 'web', created: 1, sent: 1, done: 1,
      params: {}, history: [['queued', 1], ['sent', 1], ['done', 1]],
      result: { id: 'c-1', ok: true, ms: 30, n: 0,
                note: 'SPACE,31914983424,12345678;A.JPG|12345;B.JPG|6789;' },
    };
    await page.evaluate(f => window.__push(f), frame({ events, ev_total: 137, commands: [sdCmd] }));
    await page.waitForTimeout(150);
    let ring = await page.$eval('#sdring', el => el.textContent);
    check('存储面板显示容量圆环', !/还没读过/.test(ring) && ring.length > 0, ring.slice(0, 24).replace(/\s+/g, ' '));

    /* ---- 3. 再来 8 条别的命令，把 sd_ls 挤出 SSE 窗口 ---- */
    const others = Array.from({ length: 8 }, (_, i) => ({
      id: 'c-' + (10 + i), name: 'led_blink', state: 'done', source: 'web',
      created: 10 + i, sent: 10 + i, done: 10 + i, params: { n: 3 },
      history: [['queued', 10 + i], ['done', 10 + i]], result: { id: 'c-' + (10 + i), ok: true, ms: 5 },
    }));
    await page.evaluate(f => window.__push(f), frame({ events, ev_total: 137, commands: others }));
    await page.waitForTimeout(200);
    ring = await page.$eval('#sdring', el => el.textContent);
    check('sd_ls 被挤出 8 条窗口后，存储面板不退回空态', !/还没读过/.test(ring),
          ring.slice(0, 24).replace(/\s+/g, ' '));

    check('页面无 JS 异常', errors.length === 0, errors.join(' | '));
  } finally {
    if (browser) await browser.close().catch(() => {});
    srv.kill();
  }
  const bad = results.filter(r => !r.ok).length;
  console.log(`\n结果: ${results.length - bad}/${results.length} 通过`);
  process.exit(bad ? 1 : 0);
})();

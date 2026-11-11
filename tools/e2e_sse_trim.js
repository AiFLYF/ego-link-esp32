/* SSE 裁剪后的前端回归：「只发最近 N 条」带来的逻辑是否还成立。
 *
 * 覆盖：
 *   1. 事件计数不再恒为 40（服务端发 ev_total=137，40 条可见 → 文案要体现 137）
 *   2. 存储面板不会因为"后面又发了 8 条命令"而退回"还没读过"
 *   3. 事件流「查看全部」：展开取 /api/events、SSE 不覆盖、收起回到最近 40 条
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

    /* ---- 4. 事件流「查看全部」：从 /api/events 取回长列表 ---- */
    /* 服务端那侧（按设备过滤、limit 生效）由 verify_server 覆盖；这里只验
       前端的展开/收起两条路径，所以直接给 /api/events 喂一份 137 条的响应 ——
       页面仍然走自己的 apiFetch → renderEvents，只是数据源被接管。 */
    const longEvents = Array.from({ length: 137 }, (_, i) => ({
      ts: Date.now() / 1000 - i, kind: i % 3 ? 'info' : 'cmd', text: '历史事件 ' + i,
    }));
    await page.route('**/api/events*', route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, device: 'E2E-SSE', events: longEvents, kept: 137 }),
    }));
    await page.click('#evmore');
    await page.waitForTimeout(300);
    let n = await page.$$eval('#feed .item', els => els.length);
    check('点「查看全部」渲染出全部 137 条', n === 137, `实际 ${n} 条`);
    let label = await page.$eval('#evcount', el => el.textContent);
    check('展开态文案为「全部 137 条」', /全部\s*137\s*条/.test(label), `文案「${label}」`);
    let btn = await page.$eval('#evmore', el => el.textContent.trim());
    check('按钮变成「只看最近」', btn === '只看最近', `实际「${btn}」`);

    /* ---- 5. 展开期间 SSE 不能把长列表盖回 40 条 ---- */
    /* 这是这条功能最容易坏的地方：SSE 是 2 Hz，不挡住的话展开后 0.5 秒就被盖掉。 */
    await page.evaluate(f => window.__push(f), frame({ events, ev_total: 140 }));
    await page.waitForTimeout(300);
    n = await page.$$eval('#feed .item', els => els.length);
    check('展开期间 SSE 重绘不覆盖长列表', n === 137, `实际 ${n} 条`);

    /* ---- 6. 收起后回到「最近 40 条 / 共 140 条」 ---- */
    /* 收起不能靠 pull()：/api/latest 不带 events，那样会停在长列表上。
       页面要拿最近一帧 SSE 的缓存重绘。 */
    await page.click('#evmore');
    await page.waitForTimeout(300);
    n = await page.$$eval('#feed .item', els => els.length);
    label = await page.$eval('#evcount', el => el.textContent);
    check('收起后回到最近 40 条', n === 40 && /共\s*140\s*条/.test(label),
          `${n} 条 / 「${label}」`);

    /* ---- 7. 滚动锚定：往下读旧事件时，新事件不该把阅读位置推移 ---- */
    /* 事件是**头部插入**的：不做锚定的话，每来一条新事件，用户正在读的那行
       就往下滑一格（读得多快滑得多快）。锚定 = 把正在读的那行送回原视口位置。
       停在**列表中部**测：贴底时尾行被挤出窗口、scrollTop 已被夹住，锚定无从谈起。 */
    await page.$eval('#feed', el => { el.scrollTop = 400; });
    await page.waitForTimeout(100);
    const scBefore = await page.$eval('#feed', el => el.scrollTop);
    /* 顶行必须用 getBoundingClientRect 量：offsetTop 是相对 offsetParent 的，
       #feed 不是定位元素，offsetTop 里带着 feed 自己在页面里的偏移，
       拿它跟 scrollTop 比永远命中第 0 行（第一版就是这么误判的）。 */
    const topRow = () => page.evaluate(() => {
      const el = document.getElementById('feed');
      const top = el.getBoundingClientRect().top;
      const k = Array.prototype.find.call(el.children, c => c.getBoundingClientRect().bottom > top + 1);
      return k ? k.textContent.replace(/\s+/g, ' ').slice(0, 24) : '';
    });
    const pinnedBefore = await topRow();
    const fresh = { ts: Date.now() / 1000, kind: 'info', text: '新来的事件' };
    await page.evaluate(f => window.__push(f), frame({ events: [fresh, ...events], ev_total: 141 }));
    await page.waitForTimeout(200);
    const scAfter = await page.$eval('#feed', el => el.scrollTop);
    const pinnedAfter = await topRow();
    check('新事件头部插入时阅读位置不滑走（滚动锚定）',
          scAfter > scBefore && pinnedBefore === pinnedAfter,
          `scrollTop ${scBefore} → ${scAfter}；视口顶行「${pinnedBefore}」→「${pinnedAfter}」`);

    /* ---- 8. 空态与正常态共用一份缓存：SSE 说 0 条就必须画空态 ---- */
    /* 旧实现空态走 setHTML（缓存在 el.__html）、正常态直接写 innerHTML，
       两套缓存各说各话：再回到 0 条时 setHTML 看见 __html 没变就跳过写入，
       屏幕上留着上一批事件。这里验「有 → 无 → 有」来回切都跟得上。 */
    await page.evaluate(f => window.__push(f), frame({ events: [], ev_total: 0 }));
    await page.waitForTimeout(200);
    const emptyShown = await page.$eval('#feed', el => !!el.querySelector('.empty'));
    check('SSE 报 0 条时画空态（不被旧缓存挡住）', emptyShown);
    await page.evaluate(f => window.__push(f), frame({ events, ev_total: 141 }));
    await page.waitForTimeout(200);
    n = await page.$$eval('#feed .item', els => els.length);
    check('空态之后再来事件能画回来', n === 40, `实际 ${n} 条`);

    check('页面无 JS 异常', errors.length === 0, errors.join(' | '));
  } finally {
    if (browser) await browser.close().catch(() => {});
    srv.kill();
  }
  const bad = results.filter(r => !r.ok).length;
  console.log(`\n结果: ${results.length - bad}/${results.length} 通过`);
  process.exit(bad ? 1 : 0);
})();

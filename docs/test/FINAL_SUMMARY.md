# 最终总结（FINAL_SUMMARY）

> 分支 `test/qa-full-review`（基线 `origin/develop` @ `a1e1d56`）
> 时间：2026-11-10
> 前置文档：`UI_INVENTORY.md`（112 个元素）、`TEST_PLAN.md`（113 条用例）、`TEST_REPORT.md`（缺陷详情）

---

## 一、结论

**8 个缺陷全部修复，0 遗留。** 115 条用例全过，项目自带的 9 套门禁全绿，固件编译 0 error。

| | 修复前 | 修复后 |
|---|---|---|
| 用例通过率 | 106 / 113（93.8%） | **115 / 115（100%）** |
| P0 / P1 | 0 / 0 | 0 / 0 |
| P2 | 4 | **0** |
| P3 | 4 | **0** |
| 固件编译 | 未验证（配网页改动） | **0 error，产出 `rw1_device.bin`** |

---

## 二、修了什么

| 编号 | 级别 | 一句话 | 改法 | 提交 |
|---|---|---|---|---|
| BUG-001 | P2 | 单板场景方向档位永远显示「还没收到」 | 把 `devOrient` 的更新提到 `list.length<=1` 的早退之前 | `5c93a82` |
| BUG-002 | P2 | 删一个 SD 文件要确认两次，第二次谎称「格式化」 | `renderSd()` 改走 `doSendCmd()`，跳过 `sendCmd()` 的通用确认 | `267b3b2` |
| BUG-003 | P3 | 存储面板空态引导永远不显示 | `renderCmds()` 空列表分支里补一次 `renderSd()` | `13ebdf6` |
| BUG-004 | P3 | 事件流空态一片空白 | 拿到**空数组**时画空态；`!evts`（通道不负责）直接返回 | `ea9dd2e` + `1e1306b` |
| BUG-005 | P3 | 曲线空态提示和 y 轴「2.0」读成一行 | 提示画到绘图区正中 | `72cdeae` |
| BUG-006 | P2 | 项目主页 375px 横向溢出、代码块被裁 | `.payloads>*{min-width:0}` | `7a0c933` |
| BUG-007 | P3 | 配网页每次打开一条 `/favicon.ico` 404 | 补 `<link rel="icon" href="data:,">` | `0e95449` |
| BUG-008 | P2 | 摄像头空态文案「点右侧」窄屏指错方向 | 去掉方位词 | `e7a5167` |

### 修复过程中自己引入又抓回来的一个回归

`ea9dd2e` 给事件流补空态时，把 `!evts || !evts.length` 合并判断了。
但 `/api/latest` 是 5Hz 轮询、**不带 events 字段** —— 于是每 200ms 会把 SSE 刚画好的
事件列表换成「还没有事件」。这正是文件顶部注释警告过的"跟 SSE 打架"，只是换了个方向。

`1e1306b` 拆开判断，并补了用例 `H01b`（有事件时连等 2.5s，断言既有 `.item` 又没有 `.empty`）。
**这条记在这里的原因**：它说明"补空态"这种改动必须区分「字段缺失」和「真的空」，
否则修一个 P3 会换来一个更严重的抖动。

---

## 三、遗留

**没有未修复的缺陷。** 以下 5 项因缺硬件/外部服务无法测试，需要真机时补：

| 项 | 原因 | 替代验证 |
|---|---|---|
| 板端 240×240 真机显示 | 无已配网的板子 | `ui_preview.py` 渲染 7 个状态 + 布局自检 |
| 真机录音 / ASR / 语音链路 | 同上，且无 ASR 服务 | 服务端假识别链路 + 页面三态文案分支 |
| 真实摄像头画面 | 无硬件 | 用真 JPEG 走 `/api/frame` 全链路 |
| 语音播报的实际发声 | headless 无语音引擎 | 桩替换 `speechSynthesis.speak`，验开关是否真的影响调用 |
| 20 块真板 | 只有假板子 | `load_classroom.py`：p95 4ms、20/20 在册、广播 20/20 |

---

## 四、建议后续改进（不是缺陷，是"下次别再踩"）

1. **`tools/e2e_dashboard.js` 永远起两块假板子** —— `list.length <= 1` 这条分支从没被执行，
   BUG-001 才藏了那么久。建议补一个"单板"用例（或把它参数化成 1 块）。
2. **`tools/e2e_sse_trim.js` 一上来就下发 `sd_ls`** —— 存储面板永远有内容，BUG-003 同理。
   建议加一个"零指令"的初始态断言。
3. **`sendCmd()` 里 danger 的确认文案是写死的「格式化 SD 卡」** —— 现在只有 `sd_format`
   走这条路；以后再加危险指令，要么在 `CMD_UI` 里带 `title/body`，要么直接走 `doSendCmd`。
4. **`devOrient` 以设备名为键、只增不删** —— 一学期换几十个设备名就会积累几十个键。
   可以在 `renderDevices()` 里按当前 `list` 修剪一次（影响很小，但符合"外部输入当键就要有界"）。
5. **`fake_board.py` 不实现 `sd_ls` / `cam_*`** —— 每个测存储/摄像头的人都得自己写桩。
   本报告的 `tests/e2e/qa_dashboard.js` 里有一个 60 行的 JS 假板子可以直接搬。
6. **`e2e_design.js` 在干净 worktree 里会 FATAL** —— 缺 `.workbuddy-ai/tmp/`。
   建议脚本自己 `mkdir -p`，否则每个新 clone 第一次跑都撞。
7. **`ui_preview.py` 依赖 Pillow，而本机 `python` 没有** —— 要用
   `C:/Users/FLYF/AppData/Local/Programs/Python/Python312/python.exe`。
   建议在 README 的"跑真浏览器回归"旁边补一句，或把 Pillow 写进依赖说明。
8. **"空态"逻辑散在四个地方各写各的** —— 这次一口气漏了三处（事件流、存储、曲线）。
   可以考虑抽一个 `emptyState(el, text)`，至少让"该有文案"变成显式约定。
9. **就绪探测别只看 HTTP 200** —— 这台机器 8080–8090 有一堆别的服务（8081 是
   opencode2api 控制台），任意路径都回 200。这次就把"端口被占"误判成"起来了"，
   测了半天别人的页面。四个 QA 脚本已改成校验 `/api/latest` 的 JSON 形状。

---

## 五、最终门禁基线（全部实跑）

| 命令 | 结果 |
|---|---|
| `python tools/lint_py.py` | 16 个文件，**0 个问题** |
| `python -m unittest discover -s tools -p "test_*.py"` | **74 条全过** |
| `python tools/verify_server.py` | **163/163 通过** |
| `node tools/e2e_dashboard.js` | **18 通过，无问题** |
| `node tools/e2e_design.js` | **33 通过，无问题** |
| `node tools/e2e_sse_trim.js` | **9/9 通过** |
| `node tools/check_poll_backpressure.js` | 峰值在飞 **1**（上限 1） |
| `python tools/load_classroom.py --boards 20` | **达标**（20/20 在册，广播人人有份） |
| `python tools/ui_preview.py` | 107 个宏，**布局自检全过** |
| `build_device.bat` | **0 error**，产出 `rw1_device.bin` |
| `tests/e2e/qa_dashboard.js` | **79 / 79** |
| `tests/e2e/qa_llm.js` | **6 / 6** |
| `tests/e2e/qa_provisioning.js` | **16 / 16** |
| `tests/e2e/qa_landing.js` | **11 / 11** |

---

## 六、这次沉淀下来的两条方法论

1. **"跑通一条路径"不等于"修好了"**：BUG-001 和 BUG-003 都藏在"默认形态"里
   （单板 / 零指令），而现有测试恰好都避开了这两个形态。补测试时要问一句
   "这个用例覆盖的是产品默认的那个形态吗"。
2. **探针失败先怀疑探针**：这次有 6 次"看起来像产品 bug、其实是探针有问题"
   （详见 `TEST_REPORT.md` 第四节），包括端口被占、懒加载图片、C 注释里的引号、
   假 JPEG、`textContent` 读进 script 源码。**每一次都先做基线对照再下结论**，
   否则会把 6 个假缺陷写进报告。

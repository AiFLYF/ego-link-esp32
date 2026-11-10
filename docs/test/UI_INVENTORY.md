# UI 清单（UI_INVENTORY）

> 依据：**代码**（`server/web/index.html` 3083 行、`server/server.py` 2498 行、
> `device/main/provisioning_page.h`、`docs/index.html`、`device/main/ui.c`），
> 不凭印象。编号规则：`<页面>-<区块>-<类型><序号>`，类型 B=按钮 / I=输入 / S=下拉 /
> C=容器·卡片 / T=展示元素 / K=可点击的非按钮元素。
>
> 项目类型：**本机 Web 应用（Python 标准库 HTTP 服务）+ 嵌入式固件 UI**。
> 没有前端框架、没有路由库、没有 package.json / requirements.txt（服务端零第三方依赖）。
> 四个 UI 面：
>
> | 前缀 | 界面 | 入口 | 能否自动化 |
> |---|---|---|---|
> | `D` | 网页仪表盘 | `GET /` | ✅ Playwright |
> | `P` | 板端配网页（SoftAP） | 板子热点 `192.168.4.1` | ✅ 抽 HTML + mock 接口 |
> | `L` | 项目主页（GitHub Pages） | `docs/index.html` | ✅ Playwright |
> | `V` | 板端 240×240 LVGL 界面 | `device/main/ui.c` | ⚠️ 只能渲染预览图（`tools/ui_preview.py`），真机不可用 |
>
> **路由清单（服务端）**：`GET /`、`GET /vendor/three.min.js`、`GET /vendor/fonts/*`、
> `GET /api/devices`、`/api/latest`、`/api/commands`、`/api/events`、`/api/logs`、
> `/api/frame`、`/api/shots`、`/api/shots/<dev>/<name>`、`/api/stream`（SSE）；
> `POST /api/telemetry`、`/api/command`、`/api/frame`、`/api/audio`、`/api/ask`、
> `/api/ask/cancel`、`/api/shots/delete`。未匹配路径 → **404 JSON**（无自定义 404 页面，
> 不是 UI 缺陷，是 API 风格）。**没有登录页 / 权限页 / 重定向 / 动态路由**。

---

## D · 网页仪表盘（`server/web/index.html`）

### 页面状态矩阵

| 状态 | 触发方式 | 代码依据 |
|---|---|---|
| 正常（在线 + 有数据） | 板子上报中 | `render()` |
| 空数据（无板子） | 刚启动，`/api/latest` 返回空 | `drawChart()` 的 `n<2` 分支、`#act`="等待数据" |
| 离线陈旧 | 板子 10s 没上报 | `.stage.stale` 降饱和 |
| 加载中 | 大模型生成中 | `#replybox.pending`、`#mode` 加"生成中…" |
| 报错（链路断） | 服务端挂掉 / 断网 | `#linkp` → "重连中…"（`syncLink()`） |
| 无权限（401） | 服务端设了 `RW1_API_TOKEN` | `apiFetch()` → 摊开页脚密钥框 |
| 超长内容 | AI 回复很长 / 设备名很长 / 文件名很长 | `#reply` 换行、`.item` 的 `overflow-wrap:anywhere` |
| 多设备 | ≥2 台板上报 | `#devcard` 由 hidden 转显示 |

### D01 顶栏 `header.appbar`

- [ ] `D01-T01` 品牌区（Ego Link / 实时仪表盘）
- [ ] `D01-T02` 设备在线胶囊 `#dev`（在线/离线 + 呼吸点）
- [ ] `D01-T03` 采样率胶囊 `#hzp` / `#hzv`
- [ ] `D01-T04` 上报帧数胶囊 `#postsp` / `#postv`
- [ ] `D01-T05` 链路状态胶囊 `#linkp` / `#linkv`（实时 / 重连中…）
- [ ] `D01-B01` 主题切换按钮 `#theme`（明 ↔ 暗，写 localStorage `rw1-theme`）
- [ ] `D01-T06` 滚动后顶栏阴影（`.appbar.scrolled`）

### D02 设备卡片 `#devcard`（单板时 `hidden`）

- [ ] `D02-T01` 设备数 / 在线数 `#devcount`
- [ ] `D02-B01` 全选 / 取消全选 `#devall`（文案随选中数变）
- [ ] `D02-K01` 设备行（点行 = 切换查看的设备；`role=button` + `tabindex=0`）
- [ ] `D02-I01` 每行复选框 `.devchk`（勾选 = 批量下发目标）
- [ ] `D02-T02` 每行状态文本「在线 · x 秒前 · 来源 · oN」（1Hz 重算）
- [ ] `D02-T03` 每行当前活动 `m-act`

### D03 Hero 实时状态 `section.stage`

- [ ] `D03-T01` 眉标「实时状态 · 最近 8 秒」
- [ ] `D03-T02` 数据来源 `#src`
- [ ] `D03-T03` 合加速度环（`#ring` 弧长 + `#abs` 读数）
- [ ] `D03-T04` 活动词 `#act`（语义色 + pop 动画）
- [ ] `D03-T05` 步数 / 8s `#steps`
- [ ] `D03-T06` 晃动 / 8s `#shakes`
- [ ] `D03-T07` AI 来源 `#mode`（规则AI / 大模型 + 生成中…）
- [ ] `D03-T08` 倾角 `#tilt`
- [ ] `D03-T09` 姿态球 `#ball`
- [ ] `D03-T10` 三轴对称条 `#bx/#by/#bz` + 数值 `#vx/#vy/#vz`
- [ ] `D03-T11` AI 回复正文 `#reply`（`#replybox.has` / `.pending`）
- [ ] `D03-T12` 语音状态提示 `#voicehint`
- [ ] `D03-T13` 计时 `#timing`（识别/生成/共，无数据时 hidden）
- [ ] `D03-B01` 停止 `#aicancel`（只在 `ai_pending` 时显示）
- [ ] `D03-I01` 文字入口 `#asktext`（maxlength=200）
- [ ] `D03-B02` 发送 `#asksend`
- [ ] `D03-B03` 播报开关 `#ttsbtn`（aria-pressed，写 localStorage `rw1-tts`）
- [ ] `D03-B04` 快捷 chip「📷 拍张照」
- [ ] `D03-B05` 快捷 chip「💡 闪三下」
- [ ] `D03-B06` 快捷 chip「🔄 调到第3档」
- [ ] `D03-B07` 快捷 chip「⏱ 状态分析」

### D04 指令按钮栏 `.stage-actions`

- [ ] `D04-T01` 指令按钮容器 `#cmdbts`（按 `/api/commands` 的 `names` 动态生成）
- [ ] `D04-B01` 「采集一次」（primary，全页唯一实心按钮）
- [ ] `D04-B02` 「闪灯 ×3」
- [ ] `D04-B03` 「LED 常亮 / LED 熄灭」（toggle）
- [ ] `D04-B04` 「设置方向档位」
- [ ] `D04-B05` 「下发板子设置」
- [ ] `D04-B06` 「读取存储信息」
- [ ] `D04-B07` 「拍照」
- [ ] `D04-B08` 「格式化 SD 卡」（danger，点后弹确认）
- [ ] `D04-T02` 下发结果提示 `#cmdhint`

### D05 曲线 `.stage-block`

- [ ] `D05-C01` 画布 `#cv`（8s |a| 曲线、1g 参考线、0.35g 失重线、末端读数胶囊）
- [ ] `D05-T01` 图例（曲线 / 1g / 失重阈值）

### D06 指令记录 `#cardcmd`

- [ ] `D06-S01` 方向档位下拉 `#orientsel`（o0–o15，16 项）
- [ ] `D06-B01` 应用档位 `#orientapply`
- [ ] `D06-T01` 当前档位提示 `#orientnow`
- [ ] `D06-T02` 指令列表 `#cmds`（最多 8 条；状态徽章 queued/sent/done/failed/timeout）

### D07 事件流 `#cardfeed`

- [ ] `D07-T01` 事件计数 `#evcount`（"最近 40 条 / 共 N 条"）
- [ ] `D07-B01` 查看全部 / 只看最近 `#evmore`
- [ ] `D07-T02` 事件列表 `#feed`（时间 + 语义色圆点 + 文本）

### D08 摄像头 `#cardcam`

- [ ] `D08-T01` 归属设备 `#camwho`
- [ ] `D08-T02` 画面 `#camimg`（无帧时 hidden）
- [ ] `D08-T03` 无信号提示 `#camnosig`
- [ ] `D08-B01` 拍照 `#camshot`（下发 `cam_capture` + 存 IndexedDB）
- [ ] `D08-B02` 开启/关闭实时画面 `#camlive`（下发 `cam_stream`）
- [ ] `D08-S01` 帧率 `#camfps`（1/2/4/6）
- [ ] `D08-T04` 画面状态 `#camstat`
- [ ] `D08-T05` 本机照片存档标题 / 归属 `#shotwho`
- [ ] `D08-C01` 照片列表 `#shotlist`
- [ ] `D08-B03` 每张照片的「删除」（弹确认；本机走 IndexedDB，服务器走 `/api/shots/delete`）

### D09 3D 姿态 `#card3d`（初始化成功后显示）

- [ ] `D09-T01` 数据来源 `#d3src`
- [ ] `D09-S01` 页面取数频率 `#pollsel`（16/33/50/100/200/500/2000 ms）
- [ ] `D09-C01` 画布 `#board3d`（three.js；WebGL 不可用时保持 hidden）

### D10 板子设置 `#cardcfg`

- [ ] `D10-I01` WiFi 名称 `#cfgssid`
- [ ] `D10-I02` 密码 `#cfgpass`（提交后清空）
- [ ] `D10-I03` 服务器地址 `#cfgurl`（placeholder = 当前 origin）
- [ ] `D10-I04` 上报周期 `#cfgper`（number，min=100 max=2000 step=10）
- [ ] `D10-B01` 下发到板子 `#cfgapply`
- [ ] `D10-T01` 结果提示 `#cfghint`

### D11 存储管理 `#cardsd`

- [ ] `D11-T01` 归属设备 `#sdwho`
- [ ] `D11-B01` 读取存储信息 `#sdrefresh`
- [ ] `D11-C01` 占用圆环 `#sdring`（空态 / 百分比 / 三档颜色）
- [ ] `D11-K01` 圆环可点（`role=button` + `tabindex=0`，Enter/Space 触发滚动高亮）
- [ ] `D11-C02` 文件列表 `#sdfiles`
- [ ] `D11-B02` 每个文件的「删除」（弹确认 → `sd_rm`）

### D12 页脚 `footer`

- [ ] `D12-T01` 服务器地址 `#host`
- [ ] `D12-T02` 落盘目录 `#logdir`
- [ ] `D12-T03` 指令白名单 `#names`
- [ ] `D12-B01` 🔑 密钥 `#keybtn`（摊开/收起输入框）
- [ ] `D12-I01` 密钥输入 `#keyinput`
- [ ] `D12-B02` 保存密钥 `#keysave`

### D13 确认弹窗 `dialog#cfm`

- [ ] `D13-T01` 标题 `#cfm-title`
- [ ] `D13-T02` 正文 `#cfm-body`
- [ ] `D13-B01` 取消 `#cfm-cancel`
- [ ] `D13-B02` 确定 `#cfm-ok`（danger 色）

---

## P · 板端配网页（`device/main/provisioning_page.h`）

单页、自包含、零外部请求。手机连板子热点后打开 `192.168.4.1`。

- [ ] `P01-T01` 标题「Ego Link 配网」+ 说明
- [ ] `P02-S01` WiFi 下拉 `#ssid`（自动扫描）
- [ ] `P02-B01` 重新扫描（`GET /scan`）
- [ ] `P03-I01` 手动填 SSID `#ssid2`（隐藏网络）
- [ ] `P04-I01` 密码 `#pass`
- [ ] `P04-B01` 显示/隐藏密码 `#pwb`
- [ ] `P05-I01` 服务器地址 `#url`
- [ ] `P05-B01` 测试连接（`GET /testurl?url=`）
- [ ] `P06-I01` 设备名 `#device`
- [ ] `P07-I01` 上报周期 `#period`（inputmode=numeric）
- [ ] `P08-B01` 保存并连接（`POST /save`，先试连再写 NVS）
- [ ] `P09-B01` 清除配置并重启（`POST /clear`，原生 confirm）
- [ ] `P10-T01` 消息区 `#msg`（ok/err/info 三种配色）

---

## L · 项目主页（`docs/index.html`）

- [ ] `L01-A01` 顶部导航 6 个锚点（#loop/#ui/#features/#quickstart/#api/#roadmap）
- [ ] `L01-A02` GitHub 外链
- [ ] `L02-T01` Hero 标题 / 副文案
- [ ] `L02-A01` CTA「看数据怎么跑 →」（#loop）
- [ ] `L02-A02` CTA「四步跑通」（#quickstart）
- [ ] `L02-T02` 5 个数字滚动统计（500ms / ±16g / 8 方向 / 0 依赖 / 180KB）
- [ ] `L02-C01` three.js 场景 `#scene`（WebGL 不可用时显示 `.hero-fallback`）
- [ ] `L03-C01` 技术栈跑马灯 `#mq`
- [ ] `L04-C01` 闭环 5 张步骤卡
- [ ] `L04-B01` / `L04-B02` 两段代码块的「复制」
- [ ] `L05-C01` 4 张板端界面截图
- [ ] `L05-C02` 3 张特性卡
- [ ] `L06-C01` 6 张工程决定卡
- [ ] `L07-C01` 四步上手（4 段代码 + 4 个「复制」按钮 `L07-B01`–`L07-B04`）
- [ ] `L08-C01` 接口一览表
- [ ] `L09-C01` 18 周路线时间线 `#tl`
- [ ] `L10-A01` 页脚 GitHub 链接
- [ ] `L11-B01` 回到顶部 `#top`
- [ ] `L12-T01` 加载遮罩 `#loader`（JS 正常时淡出并 `pointer-events:none`）

---

## V · 板端 240×240 界面（`device/main/ui.c`）

只能通过 `tools/ui_preview.py`（解析 `ui.c` 的 `UI_*` 宏）出图验证，**真机不可用**。

- [ ] `V01-T01` 状态胶囊（在线/无服务 + 100Hz + 上报计数）
- [ ] `V02-T02` 活动环（活动词 + |a| 量程 0–2g + 语义色）
- [ ] `V03-T03` 姿态球（重力方向点）
- [ ] `V04-T04` 倾角 + 档位 `oN`
- [ ] `V05-T05` 三轴对称条（X/Y/Z，±2g，0 在中点）
- [ ] `V06-T06` AI 回复气泡（自动分页 + 页码点）
- [ ] `V07-T07` 远程指令状态 `···/OK/NG`
- [ ] `V08-T08` 跌落呼吸红光
- [ ] `V09-T09` 七种状态：等待 / 静置 / 倾斜 / 步行 / 晃动 / 跌落 / 离线

---

## 清单统计

| 页面 | 元素数 |
|---|---|
| D 仪表盘 | 72 |
| P 配网页 | 13 |
| L 主页 | 18 |
| V 板端 | 9 |
| **合计** | **112** |

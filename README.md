# Ego Link 教学版实现 · AI交互课项目仓库

> 《AI交互原型与用户体验设计》（18 周，贯穿样例"Ego Link 随身智能终端"）的课程项目实现仓库，按周推进。
> 当前进度 **第2周**（已实机跑通）：在周一"采集 → 上报 → 存储 → 展示"闭环之上，
> 加上**网页远程下发「采集一次」指令、按 request_id 反馈执行结果**的反向通道 —— 即下图全链路。
>
> 🏠 项目主页（three.js 数据闭环可视化）：<https://aiflyf.github.io/ego-link-esp32/>

## 课程路线（本仓库逐周生长）

| 周 | 任务 | 状态 |
|---|---|---|
| 1 | 传感数据采集 + 服务器接收/存储 + Web 展示 | ✅ |
| 2 | Web 远程"采集一次"指令（request_id）与执行结果反馈 | ✅ 本仓库当前内容 |
| 3 | 按键触发 + 本地/远端物理反馈闭环 | BOOT 提问按钮已是雏形 |
| 4–6 | 自然语言查询/请求、按键说话语音链路、澄清与停止 | |
| 7–9 | 按需取图、视觉推理反馈、视觉事件主动询问 | |
| 10–12 | 观测 vs 当前状态（数据年龄）、多源上下文、纠错记忆 | |
| 13–15 | 端侧推理、断网缓存补传、迟到/过期任务处理 | |
| 16–18 | 体验测试、模块迁移、验收复盘 | |

## 整体流程（数据闭环）

```
┌──────────────────┐  WiFi   ┌────────────────────────────────────────────┐
│  ESP32-S3-EYE    │ ──────► │        你的电脑 = 服务器 (server.py)          │
│  ┌────────────┐  │  HTTP   │  ① 接收 IMU 遥测  POST /api/telemetry       │
│  │ IMU 100Hz  │──┼───────► │     每 500ms 一批（默认 50 个样本）           │
│  │ 本地缓冲   │  │  批量   │  ② AI 分析姿态/晃动/计步/跌落（规则引擎，      │
│  ├────────────┤  │  JSON   │     配了大模型 key 时提问改由真 LLM 回答）     │
│  │ LVGL 屏幕  │◄─┼──────── │  ③ 结果回传开发板显示                        │
│  ├────────────┤  │  响应   │  ④ 网页仪表盘实时推送 GET /  (SSE)           │
│  │ BOOT 按键  │  │         │  ⑤ 遥测与事件落盘 server/data/*.jsonl        │
│  └────────────┘  │         └────────────────────────────────────────────┘
│  单击 = 向AI提问  │               浏览器打开 http://<电脑IP>:8000/
│  长按 = 切方向校准│
└──────────────────┘
```

1. **板子**以 100Hz 采样加速度计，在本地缓冲成一批，每 500ms POST 一次到电脑的服务器；
2. **服务器**把整批样本并入滑动窗口，做运动分类（静置/倾斜方向/晃动/步行计步/疑似跌落）；
3. 服务器把 `当前活动 + AI回复` 放进 HTTP 响应里回传，**板子屏幕实时显示**；
4. **单击 BOOT 键** → 下一帧遥测带上 `ask` 标志 → 服务器**立刻**回一句"正在思考…"，
   大模型在后台线程生成答案，下一帧起板子就能读到真正的回复（板端不会被大模型拖住）；
5. **长按 BOOT 键** → 循环切换倾斜方向校准（0–7），存进 NVS，不用重编译；
6. 电脑浏览器打开仪表盘，实时看板子的姿态球、|a| 曲线、事件流和 AI 对话。

> **为什么是"100Hz 采样 + 批量上报"？** 走路频率约 1.5–2.5Hz、自由落体不到 0.5 秒。
> 如果按每 500ms 采一个点（2Hz）直接上报，计步和跌落检测在物理上就不可能做对，
> 只能算出假的数字。现在 HTTP 频率仍是 2Hz，但服务端拿到的是真实波形。

## 目录

| 路径 | 内容 |
|---|---|
| `server/server.py` | 电脑服务器（仅 Python 标准库，无需 pip 安装） |
| `server/data/` | **运行时生成**的 JSONL 日志（git-ignored，见下文"数据落盘"） |
| `device/` | 开发板 ESP-IDF 工程（ESP-IDF v5.4.x，目标 esp32s3） |
| `device/main/` | `main.c` 启动、`accel_input.c` IMU驱动（SC7A20/LIS3DH/MPU6050/QMA7981 自动识别）、`wifi_link.c`、`transport.c` 采样+HTTP遥测、`led_feedback.c` LED图案播放器、`ui.c` LVGL 图形化仪表盘（见下文"板端界面"） |
| `device/main/Kconfig.projbuild` | WiFi 账号、服务器 URL、采样周期、上报周期（占位默认值，用 menuconfig 配自己的） |
| `tools/gen_font.py` | **必跑**：中文字体子集生成器（生成 git-ignored 的 `device/main/rw1_font.c`） |
| `tools/fake_board.py` | 假开发板：不接硬件就能灌数据、调仪表盘 |
| `tools/ui_preview.py` | 板端 240x240 界面预览渲染器：**解析 `ui.c` 里的 `UI_*` 宏**出图，没有硬件也能验证界面 |
| `tools/verify_server.py` | 服务端回归测试（跑完自报「N/N 通过」，断言数随功能增长；含"大模型不阻塞板端"验证） |
| `tools/idf_build.py` | 在 Git Bash 里调用 `idf.py` 的包装器（见常见问题） |
| `tools/tools_serial_capture.py` | 非交互串口抓取（复位→打印N秒→退出，需 pyserial） |
| `build_device.bat` / `flash_device.bat` | 干净环境编译 / 烧录+串口抓取（参数化 COM 口与秒数） |
| LICENSE / NOTICE | MIT；第三方组件与字体授权说明 |

以下内容由构建/组件管理器生成、**不入库**：`device/build/`、`device/managed_components/`、
`device/sdkconfig`、`device/main/rw1_font.*`、`server/data/`。

## 首次上手（4 步）

```powershell
# ① 生成中文字体（任意带 fontTools 的 Python；从本机字体裁剪，见 NOTICE 授权说明）
python tools/gen_font.py
python tools/gen_font.py --list-fonts   # 看看本机有哪些可用中文字体

# ② 配置自己的 WiFi 与电脑 IP（Kconfig → "RW1 AI Interaction"，值存于本地 sdkconfig）
cd device && idf.py -D SDKCONFIG_DEFAULTS=sdkconfig.bsp.esp32_s3_eye menuconfig

# ③ 编译 + 烧录（首次编译会从组件仓库拉取 espressif/esp32_s3_eye 等到 managed_components/）
cd .. && build_device.bat
flash_device.bat COM3 30        # 端口按设备管理器改；默认 COM10

# ④ 启动服务器（同一直连网络/热点即可）
python server\server.py
```

浏览器打开 <http://localhost:8000/> 即见仪表盘。串口日志出现
`Detected accelerometer: SC7A20` → `Got IP` → `activity: 静置·…` 即闭环成功。

- 服务器 URL 填电脑 `ipconfig` → WLAN 的 IPv4（**不能** 127.0.0.1）；IP 变化后 menuconfig 改 `RW1_SERVER_URL` 重编译。
- Windows 防火墙弹窗请**允许**；否则以管理员执行：
  `netsh advfirewall firewall add rule name="rw1-server" dir=in action=allow protocol=TCP localport=8000`
- **离线/国内网络**：首次编译在线拉取组件可先走代理：`set https_proxy=http://127.0.0.1:10808`（按自己代理端口）；完全离线则把已解析的 `managed_components/` 拷入 `device/`（`dependencies.lock` 已提供，保证版本一致）。
- 断网后板子**无限重试**（前8次每2s，之后每15s），网络恢复即自动重连，无需重新烧录。

### 采样与上报节奏（menuconfig）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `RW1_SAMPLE_PERIOD_MS` | 10 | 本地 IMU 采样周期，10ms = 100Hz。**不要超过 20ms**，否则计步/跌落做不了 |
| `RW1_TELEMETRY_PERIOD_MS` | 500 | 每 500ms 把缓冲的样本打包成一次 HTTP POST |

### API 协议

| 方法/路径 | 说明 |
|---|---|
| `POST /api/telemetry` | 请求 `{batch:[[x,y,z],…], x, y, z, source, ask, q[, result]}` → 响应 `{ok, activity, reply, pending[, cmd]}` |
| `POST /api/command` | 下发远程指令 `{"name":"capture_once"}` → 响应 `{ok, id, device_online}` |
| `GET /api/commands` | 最近 20 条指令及其状态、执行结果 |
| `GET /api/latest` | 快照（状态 + 8s 曲线降采样 + 事件流 + 指令列表），JSON |
| `GET /api/stream` | SSE 实时推送（仪表盘用） |
| `GET /api/logs` | 落盘目录与文件大小 |
| `GET /` | 网页仪表盘 |

- `batch` 是本次周期内的全部样本；`x/y/z` 是最后一帧，供旧版服务端或快速查看。
  只发 `x/y/z`（不带 `batch`）也能用，服务端按单帧处理。
- **`x/y` 是屏幕坐标系（+x 右、+y 下）**：板端在发送前应用了自己的 NVS 方向校准，
  所以服务端说的"上/下/左/右"和板子屏幕上显示的永远一致。
- `pending: true` 表示服务端正在后台调大模型，此时的 `reply` 是占位文案
  （"正在思考…"），下一帧或之后几帧会带回真正的答案。
- `cmd` / `result` 是第 2 周的远程指令字段，见下。

### 远程指令通道（第 2 周）

板子是**纯客户端**：它只会周期性地 POST，没有监听端口、收不到服务端主动推送。
所以指令用「搭车」的方式走：

```
网页点「采集一次」
   │  POST /api/command {"name":"capture_once"}
   ▼
服务器  ── queued ──►  把指令挂进**下一帧**遥测的响应里
   │                        {"cmd":{"id":"c-…","name":"capture_once"}}
   ▼                                   │  第 N 帧
板子   ── 用正常的 10ms 采样节拍累积 20 个样本（200ms），算平均与标准差
   │                                   │
   │  POST /api/telemetry              ▼
   │  {"…","result":{"id":"c-…","ok":true,"ms":200,"n":20,"x":…,"y":…,"z":…,"std":…}}
   │                                   │  第 N+1 帧
   ▼
服务器  ── done ──►  按 request_id 匹配，写进指令历史 + 事件流 + SSE
```

一次往返 = **2 个遥测周期**（默认 1 秒）。网页上能看到
`queued → sent → done` 的完整过程，`/api/commands` 里能查到每次采集的
平均值、样本数、耗时和标准差。

- 指令名走白名单（`CMD_NAMES`），不认识的名字直接 400。
- 服务器**一次只发一条**，板子也一次只执行一条，语义简单不会乱序。
- 下发后 `CMD_TIMEOUT_S`（默认 10 秒，`--cmd-timeout` 可调）没回音就判 `timeout`，
  网页不会一直转圈。
- 板端也有一层本地保护：2 秒内凑不齐样本就回 `ok:false` + 原因。
- 结果**上传成功后才清除**，失败会在下一帧重发（与 `ask` 的重试策略一致）。
- 采集复用正常的采样节拍，**不额外阻塞**；屏幕上数据行会显示 `采集中 / 采集OK / 采集NG`。


### 板端界面（240x240 图形化仪表盘）

早期版本的 `ui.c` 是四个居中的纯文本 label：把服务器返回的整句活动文案
（如 `运动/步行 (峰值 1.2g, 约8步)`）直接塞进 240px 宽的 label，被 `LV_LABEL_LONG_DOT`
截成 `…`，现场看不出重点。现在改成图形化仪表：

```
+--------------------------------------------+
| * 在线   100Hz   ^1234    SC7A20 o0        | 状态胶囊：链路/采样率/上报数/IMU与校准档
+----------------------+---------------------+
|        /-----\\       |     /-------\\       |
|        | 静置 |       |     |   *   |       | 左：活动环（量程=|a| 0..2g）
|        \\-----/       |     \\-------/       | 右：姿态球（重力方向）
|         水平         |      倾角 12°        |
+----------------------+---------------------+
| X ===------   Y ==-----   Z =====-----     | 三轴对称条（±2g，0 在中点）
+--------------------------------------------+
| [AI]                             (o)  1/2  |
|  服务器回复（超长自动分页，每 4 秒翻一页）    |
+--------------------------------------------+
```

- **活动环**：活动不再是一句话，而是「活动词 + 语义色 + 环」。颜色跟着活动走：
  静置绿 / 步行紫 / 运动蓝 / 晃动琥珀 / 跌落红。服务器原句里的细节
  （水平 / 向左倾斜 / 峰值 / 约 N 步）**被解析出真实数值**后另起一行小字，不再被截断。
- **姿态球**：直接用 `transport_status_t.x_g/y_g`（屏幕坐标系）放点，与网页仪表盘、
  与上报给服务端的数据是**同一套方向语义**，不做二次翻转。
- **三轴对称条**：±2g 对称条，0 在中点，正负一眼分得开。
- **AI 回复自动分页**：按显示宽度切页（中文记 2 列，优先在空格/句读处断），每 4 秒翻一页，
  右下角显示 `1/2`。长回复不再被 `…` 吃掉，而是**逐页读全**。
- **状态即颜色**：跌落时活动环外多一圈呼吸红光；断链时状态点呼吸，在线时保持常亮
  （不做无意义重绘，避免 240x240 SPI 屏撕裂）；远程指令状态在胶囊右侧显示 `···/OK/NG`。
- **几何零硬编码**：尺寸与配色全部集中在 `ui.c` 顶部的 `UI_*` 宏里，
  `tools/ui_preview.py` **解析同一份宏**渲染预览图 —— 改布局时预览自动跟着变，
  不会出现"代码改了、预览图还是旧的"。

没有硬件也能看界面（Pillow 渲染，与固件同源）：

```powershell
python tools\ui_preview.py            # 输出 docs/ui-preview/screen-*.png + contact-sheet.png
python tools\ui_preview.py --scale 3  # 拼接图放大 3 倍
```

![板端界面预览（含改造前对比）](docs/ui-preview/contact-sheet.png)

### 数据落盘（第 1 周的"存储"）

服务器默认把数据写到 `server/data/`，按天分文件、JSONL 追加：

| 文件 | 内容 |
|---|---|
| `telemetry-YYYY-MM-DD.jsonl` | 原始波形，默认降采样到 10Hz，一行一批：`{"k":"t","ts":…,"src":…,"hz":…,"s":[[x,y,z],…]}` |
| `events-YYYY-MM-DD.jsonl` | 事件流：连接/断开、活动变化、晃动、跌落、AI 提问与回复 |

写盘在独立后台线程里做，磁盘慢或写满都不会拖慢遥测响应。相关参数：

```powershell
python server\server.py --data-dir D:\rw1data     # 换目录
python server\server.py --log-hz 50               # 波形按 50Hz 存（更占空间）
python server\server.py --retain-days 30          # 启动时清理 30 天前的文件（0=不清理）
python server\server.py --no-log-telemetry        # 只存事件，不存波形
```

粗略占用：默认 10Hz 波形约 15–20 MB/天，配合 `--retain-days` 可控制总量。

### 不用开发板也能跑

```powershell
python server\server.py                                   # 一个终端起服务器
python tools\fake_board.py --scenario walk --seconds 20    # 另一个终端灌数据
```

`--scenario` 可选 `idle / tilt / walk / shake / fall / mixed`；`--ask-at 5` 模拟按 BOOT 提问；
`--expect-steps 8` 会在结束时断言服务端真的数出了步数（非 0 退出码 = 失败）。

假开发板**也会执行远程指令**，时序与固件一致（第 N 帧收到、第 N+1 帧回传），
所以第 2 周的整条往返不需要硬件就能验证：

```powershell
python tools\fake_board.py --scenario tilt --seconds 20   # 一个终端
# 另一个终端：下发一条指令并等结果
curl -X POST http://127.0.0.1:8000/api/command -H "Content-Type: application/json" -d "{\"name\":\"capture_once\"}"
curl http://127.0.0.1:8000/api/commands
```

或者直接在浏览器仪表盘上点「采集一次」按钮。

`--no-cmd` 会让假开发板**故意不执行**指令，用来验证服务端的超时判定。

改完服务端逻辑跑一遍回归测试：

```powershell
python tools\verify_server.py
```

它会自己拉起一个临时服务器、灌入各场景、检查分类/计步/跌落/落盘/超时解耦/畸形载荷/
**指令往返与超时**，共 47 项断言，全程不需要硬件，也不需要真实大模型
（用一个故意慢 6 秒的假大模型验证不阻塞）。

### 可选：接入真实大模型

不配也能完整跑通（内置规则 AI 会给出真实分析摘要）。配置后"提问"改为大模型生成：

```powershell
set RW1_LLM_API_KEY=sk-xxx
set RW1_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 或任意 OpenAI 兼容端点
set RW1_LLM_MODEL=qwen-turbo
python server\server.py
```

大模型调用跑在后台线程里，HTTP 响应立刻返回，所以模型再慢也不会让板端超时。

## 常见问题

| 现象 | 处理 |
|---|---|
| 屏幕显示"服务器:无连接" | 服务器没启动 / 防火墙拦截 / `RW1_SERVER_URL` IP 不对 |
| 连不上 WiFi（反复 retry） | 路由器侧问题（密码改了/AP重启/信道）；板子会自动无限重试，恢复后自动重连 |
| 编译报 `rw1_font.c missing` | 先跑 `python tools/gen_font.py`（见首次上手①） |
| 屏幕中文显示成方块 | 跑 `gen_font.py`，它会报告"板端文案缺字"；换了显示文案后要重跑再编译 |
| 首次编译卡在拉取组件 | 离线场景拷入 `managed_components/`；在线场景检查能否访问 components.espressif.com |
| 改了 `idf_component.yml` 后编译报 `[safe-delete]` 或重新下载组件 | 删掉 `device/managed_components/` 让它按 `dependencies.lock` 重建即可 |
| **在 Git Bash 里跑 `idf.py` 只打印一句 "MSys/Mingw is no longer supported" 就退出** | ESP-IDF 5.4 的 `idf.py` 只要环境里存在 `MSYSTEM` 变量（Git for Windows 会强制注入，`unset` 也删不掉）就**直接跳过 `main()`**。用 `build_device.bat`，或 `python tools/idf_build.py -D SDKCONFIG_DEFAULTS=sdkconfig.bsp.esp32_s3_eye build` |
| 倾斜方向左右/上下反了 | **长按 BOOT 键**循环切换校准值（屏幕上 `o0`–`o7`），存在 NVS 里，不用重编译 |
| 板子日志每隔 10 秒出现一行 `link OK` | 正常心跳（每 20 次上报一次） |
| 改 WiFi/服务器地址 | `idf.py menuconfig` → `RW1 AI Interaction`（值只存在本地 sdkconfig，不会被提交） |
| **`git switch -c feature/xxx` 建出来的分支是 unborn**（`git branch` 里看不到、`git status` 把全部文件显示成 `A`） | `.git/refs/heads/feature/` 这个**目录不存在**：松散引用写不进去时 git 会**静默成功**（退出码 0 但不落盘），`git switch -c` 只改得动 HEAD → HEAD 指向一个不存在的 ref。用**写文件的方式**建出该目录（bash 里 `mkdir -p .git/refs/heads/feature` 会被沙箱回滚），之后 `git branch` / `git switch -c` 即正常。注意 `packed-refs` 乱序是**另一个**独立故障 |
| 两个会话在同一工作区里干活，工作区文件大面积消失 / 引用丢失 | 一个工作区同时只能有一个 HEAD，两个 `git switch` 并发会把索引和工作区交替写坏。**并发必须各用独立工作区**：`git worktree add ../rw1-x <branch>` |
| 编译报 `ninja: error: build.ninja:30: loading 'CMakeFiles/rules.ninja': The system cannot find the path specified` | `device/build/` 被中断的构建或分支切换弄成了半残状态。先 `idf.py reconfigure` 再 build；还不行就删掉 `device/build/` 全量重编 |

## 安全提示

服务器默认监听 `0.0.0.0:8000`、无鉴权、CORS 放开，遥测接口任何同网设备都能写。
这在课堂/家庭局域网里没问题，但**不要把它暴露到公网**。需要时用
`--host 127.0.0.1` 限制为本机访问，或自行加一层反向代理与鉴权。

## License

MIT（见 LICENSE）。注意 NOTICE：内嵌中文字体由你本机字体裁出，发布固件前请换用开源字体（如思源黑体/Noto Sans SC，OFL 协议）。

# Ego Link 教学版实现 · AI交互课项目仓库

> 《AI交互原型与用户体验设计》（18 周，贯穿样例"Ego Link 随身智能终端"）的课程项目实现仓库，按周推进。
> 当前进度 **第3周**（已实机跑通）：在第 1 周"采集→上报→存储→展示"、第 2 周"网页远程指令"之上，
> 加上**按键触发 + 本地/远端物理反馈闭环** —— BOOT 键按下板子立刻闪灯（不等网络），
> 服务器判定跌落时自动下发指令让板子快闪告警。
>
> 🏠 项目主页（three.js 数据闭环可视化）：<https://aiflyf.github.io/ego-link-esp32/>

## 课程路线（本仓库逐周生长）

| 周 | 任务 | 状态 |
|---|---|---|
| 1 | 传感数据采集 + 服务器接收/存储 + Web 展示 | ✅ |
| 2 | Web 远程"采集一次"指令（request_id）与执行结果反馈 | ✅ |
| 3 | 按键触发 + 本地/远端物理反馈闭环 | ✅ |
| — | **运行期配置：SoftAP 配网 + NVS**（工程改进，非课程周次） | ✅ 本仓库当前内容 |
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
| `device/main/` | `main.c` 启动、`accel_input.c` IMU驱动（SC7A20/LIS3DH/MPU6050/QMA7981 自动识别）、`wifi_link.c` WiFi STA、`net_config.c` 运行期配置、`provisioning.c` SoftAP 配网、`transport.c` 采样+HTTP遥测、`ui.c` LVGL界面、`led_feedback.c` LED 物理反馈 |
| `device/main/net_config.c` | **运行期配置的唯一入口**：NVS 优先，为空时逐项回退 Kconfig（向后兼容） |
| `device/main/provisioning.c` | SoftAP + `esp_http_server` 配网页；`provisioning_page.h` 是内联的单页 HTML |
| `device/main/prov_form.c` | 配网表单的解析与校验。**刻意零 IDF 依赖**，可用 host 侧编译器直接测 |
| `device/main/Kconfig.projbuild` | WiFi 账号、服务器 URL、采样周期、上报周期——现在只是**出厂默认值**，优先用配网页改 |
| `tools/gen_font.py` | **必跑**：中文字体子集生成器（生成 git-ignored 的 `device/main/rw1_font.c`） |
| `tools/fake_board.py` | 假开发板：不接硬件就能灌数据、调仪表盘 |
| `tools/verify_server.py` | 服务端回归测试（断言数见运行输出，含"大模型不阻塞板端"验证） |
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

# ② 编译 + 烧录（首次编译会从组件仓库拉取 espressif/esp32_s3_eye 等到 managed_components/）
build_device.bat
flash_device.bat COM3 30        # 端口按设备管理器改；默认 COM10

# ③ 启动服务器（同一直连网络/热点即可）
python server\server.py

# ④ 配网 —— 不用再 menuconfig 了
#    首次上电板子会自己开热点 EGO-LINK-XXXX
#    手机连上（密码看板子屏幕）→ 浏览器打开 http://192.168.4.1
#    选 WiFi、填电脑 IP、点「保存并连接」；板子先试连，成功才保存
```

浏览器打开 <http://localhost:8000/> 即见仪表盘。串口日志出现
`Detected accelerometer: SC7A20` → `Got IP` → `activity: 静置·…` 即闭环成功。

- **WiFi 和服务器地址现在是运行期配置**，配一次就存进 NVS，断电重启直接连。
  要换网络/换电脑 IP：**双击 BOOT** 重新进配网，或长按……都不用，双击就行。
- 服务器地址填电脑 `ipconfig` → WLAN 的 IPv4（**不能** 127.0.0.1）；
  配网页上可以点「测试连接」当场验证通不通。
- `menuconfig` 里的 `RW1_WIFI_*` / `RW1_SERVER_URL` 现在只是**出厂默认值**——
  NVS 为空时才生效。老 `sdkconfig` 一字不改仍然能跑（向后兼容）。
- Windows 防火墙弹窗请**允许**；否则以管理员执行：
  `netsh advfirewall firewall add rule name="rw1-server" dir=in action=allow protocol=TCP localport=8000`
- **离线/国内网络**：首次编译在线拉取组件可先走代理：`set https_proxy=http://127.0.0.1:10808`（按自己代理端口）；完全离线则把已解析的 `managed_components/` 拷入 `device/`（`dependencies.lock` 已提供，保证版本一致）。
- 断网后板子**无限重试**（前8次每2s，之后每15s），网络恢复即自动重连，无需重新烧录。

> 为什么值得做这件事：原来 WiFi 密码是**编译进固件**的，把固件发给同学等于把
> 自己的 WiFi 密码一起发出去。现在密码只在你自己的 NVS 里。

### 采样与上报节奏（menuconfig）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `RW1_SAMPLE_PERIOD_MS` | 10 | 本地 IMU 采样周期，10ms = 100Hz。**不要超过 20ms**，否则计步/跌落做不了 |
| `RW1_TELEMETRY_PERIOD_MS` | 500 | 每 500ms 把缓冲的样本打包成一次 HTTP POST |

### API 协议

| 方法/路径 | 说明 |
|---|---|
| `POST /api/telemetry` | 请求 `{batch:[[x,y,z],…], x, y, z, source, ask, q[, result][, btn]}` → 响应 `{ok, activity, reply, pending[, cmd]}` |
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

### 物理反馈闭环（第 3 周）

板载硬件：ESP32-S3-EYE 在 **GPIO3 上有一颗 LED**（`BSP_CAPS_LED=1`）；
**没有喇叭**（`BSP_CAPS_AUDIO_SPEAKER=0`，只有麦克风），所以"物理反馈"就用这颗灯。

三个方向都做了，闭环是完整的：

```
① 本地反馈 —— 不等网络
   BOOT 单击 ──► LED 立刻闪 1 下（60ms）
              └► 同时把 ask=true 带进下一帧遥测

② 远端反馈 —— 服务器 → 板子
   服务器判定跌落 ──► 自动下发 led_blink{n:3,on_ms:80,off_ms:80}
                    └► 板子快闪 3 次做物理告警（不用人点）
   网页点「闪灯 ×3」/「LED 常亮」 ──► 同样走指令通道

③ 回复到达
   服务器/AI 的回复文本变了 ──► LED 闪 2 下（"远端真的回话了"的物理信号）
```

LED 图案由一个小播放器驱动（`device/main/led_feedback.c`）：每个图案是一串
「亮/灭 + 持续多久」的段，用 `esp_timer` 逐段推进，**不阻塞任何任务**。
BSP 自带的 4 个效果（on/off/快闪/慢闪）都是**无限循环**，做不了"闪 3 次然后停"，
所以没有直接用。

| 图案 | 含义 |
|---|---|
| 1 短闪（60ms） | 按键被识别 |
| 2 短闪 | 服务器/AI 回复到达 |
| 2 中闪（120ms） | 收到并开始执行远程指令 |
| 3 快闪（80ms） | 远端告警（跌落） |
| 长亮 1 秒 | 链路或指令失败 |

按键次数也会随遥测上报（`btn` 字段），网页和日志里能看到"物理动作真的发生了"，
而不只是间接看到 `ask`。

新增的两条指令：

| 指令 | 参数 | 说明 |
|---|---|---|
| `led_blink` | `{n, on_ms, off_ms}` | 闪 n 次；n ≤ 12，时长 20–5000 ms |
| `led_set` | `{on}` | 常亮 / 熄灭 |

参数**两侧都夹**：服务端 `sanitize_params()` 先夹一道，板端 `led_feedback_blink()` 再夹一道——
外部输入不信任，谁也别指望对方把好关。


### 运行期配置：SoftAP 配网

**要解决的问题**：WiFi SSID / 密码 / 服务器地址原来只能靠 menuconfig 改，改一次要
全量重编译 + 烧录；PC 换个 DHCP 地址就得再来一遍。**更要命的是编译出来的固件里
WiFi 密码是明文**——把固件发给同学/老师，等于把自己的 WiFi 密码一起发出去。

现在改成运行期配置：

```
首次上电 / NVS 里没有凭据 / 双击 BOOT
            │
            ▼
  板子开热点 EGO-LINK-<MAC后2字节>，密码是屏幕上的 4 位随机数
            │   手机连上 → 浏览器打开 192.168.4.1
            ▼
  填表：① 扫描选 WiFi  ② 服务器地址（可当场「测试连接」）  ③ 设备名  ④ 上报周期
            │
            ▼
  点「保存并连接」→ 板子**先试连**（最长 15 秒）
            │
       ┌────┴────┐
     失败        成功
       │          │
  页面红字报原因   写 NVS → 转 STA → 屏幕显示新 IP
  **不写 NVS、     AP 自动关闭
   不关 AP，可重试**
```

**为什么是"先试连再保存"**：用户永远知道失败在哪一步；也不会把一份连不上的凭据
存进去，导致下次开机直接失联。这正是"配完了没数据、不知道是 WiFi 还是服务器地址错"
那个排查地狱的解药——所以服务器地址旁边还有个「测试连接」按钮当场验。

**向后兼容**：`net_config_load()` 在 NVS 为空时**逐项**回退 `CONFIG_RW1_*`，
所以老 `sdkconfig` 一字不改仍然能跑，Kconfig 那两项从"唯一来源"平滑降级为"出厂默认值"。

**三条重新配网的入口**（缺一条就可能"配错了只能重烧"）：

1. NVS 里没有凭据 → 开机自动进 AP（首次上电的自然路径）
2. **双击 BOOT** → 强制重新配网（刻意不动现有的单击提问 / 长按校准两个手势）
3. 配网页上的「清除配置并重启」

AP 存活 **5 分钟无操作自动关闭**回 STA（避免忘记关热点长期占道 + 耗电），
屏幕上提示"配网超时"。

| 路由 | 说明 |
|---|---|
| `GET /` | 配网页。**内联在固件里、零外部请求**——配网时手机连的是板子自己的热点，根本没有外网，引 CDN 必然白屏 |
| `GET /scan` | 扫描周边 AP，返回 `{nets:[{ssid,rssi,open}]}` |
| `GET /testurl?url=` | 当场测试服务器地址通不通 |
| `POST /save` | 解析表单 → 校验 → **先试连** → 成功才写 NVS |
| `POST /clear` | 清 NVS 并重启 |

开机自检会打印一行 `net: ssid=... url=... source=NVS|Kconfig`，一眼看清配置从哪来。

**一个刻意偏离原设计的地方**：原方案建议"配网期间用纯 `WIFI_MODE_AP`，不做 APSTA"，
但验收要求"密码错误时页面红字报错、**不关 AP**"。如果试连时切成纯 STA，手机连接会
立刻断，用户永远看不到那个错误提示。所以页面/扫描阶段保持**纯 AP**（规避信道干扰），
只在"试连"那一小段切 **APSTA**（手机保持连着才能收到结果）。

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
**指令往返与超时**/**物理反馈指令**/**跌落自动告警**，共 56 项断言，全程不需要硬件，
也不需要真实大模型（用一个故意慢 6 秒的假大模型验证不阻塞）。

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

## 安全提示

服务器默认监听 `0.0.0.0:8000`、无鉴权、CORS 放开，遥测接口任何同网设备都能写。
这在课堂/家庭局域网里没问题，但**不要把它暴露到公网**。需要时用
`--host 127.0.0.1` 限制为本机访问，或自行加一层反向代理与鉴权。

## License

MIT（见 LICENSE）。注意 NOTICE：内嵌中文字体由你本机字体裁出，发布固件前请换用开源字体（如思源黑体/Noto Sans SC，OFL 协议）。

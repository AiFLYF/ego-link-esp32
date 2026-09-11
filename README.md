# Ego Link 教学版实现 · AI交互课项目仓库

> 《AI交互原型与用户体验设计》（18 周，贯穿样例"Ego Link 随身智能终端"）的课程项目实现仓库，按周推进。
> 当前进度 **第1周**（已实机跑通）：把开发板的一项真实传感数据（ESP32-S3-EYE 板载 SC7A20 加速度计）采集出来，送到自己的服务器（没有 VPS，用电脑代替）并验证 —— 即下图全链路。

## 课程路线（本仓库逐周生长）

| 周 | 任务 | 状态 |
|---|---|---|
| 1 | 传感数据采集 + 服务器接收/存储 + Web 展示 | ✅ 本仓库当前内容 |
| 2 | Web 远程"采集一次"指令（request_id）与执行结果反馈 | 可基于 `transport.c` 直接扩展 |
| 3 | 按键触发 + 本地/远端物理反馈闭环 | BOOT 提问按钮已是雏形 |
| 4–6 | 自然语言查询/请求、按键说话语音链路、澄清与停止 | |
| 7–9 | 按需取图、视觉推理反馈、视觉事件主动询问 | |
| 10–12 | 观测 vs 当前状态（数据年龄）、多源上下文、纠错记忆 | |
| 13–15 | 端侧推理、断网缓存补传、迟到/过期任务处理 | |
| 16–18 | 体验测试、模块迁移、验收复盘 | |

## 整体流程（数据闭环）

```
┌─────────────────┐  WiFi   ┌──────────────────────────────────────────┐
│  ESP32-S3-EYE   │ ──────► │        你的电脑 = 服务器 (server.py)        │
│  ┌───────────┐  │  HTTP   │  ① 接收 IMU 遥测  POST /api/telemetry     │
│  │ IMU 采样   │──┼───────► │  ② AI 分析姿态/晃动/计步（规则引擎，        │
│  ├───────────┤  │  JSON   │     可配大模型 key 自动升级成真 LLM）      │
│  │ LVGL 屏幕  │◄─┼──────── │  ③ 结果回传开发板显示                      │
│  ├───────────┤  │  响应   │  ④ 网页仪表盘实时推送 GET /  (SSE)        │
│  │ BOOT 按键  │  │         │                                          │
│  └───────────┘  │         └──────────────────────────────────────────┘
│  单击=向AI提问   │                 浏览器打开 http://<电脑IP>:8000/
└─────────────────┘
```

1. **板子**每 500ms 读一次加速度计（g 值），POST 到电脑的服务器；
2. **服务器**用滑动窗口做运动分类（静置/倾斜方向/晃动/步行计步/疑似跌落），即"AI 分析"；
3. 服务器把 `当前活动 + AI回复` 放进 HTTP 响应里回传，**板子屏幕实时显示**；
4. **按 BOOT 键** → 下一帧遥测带上 `ask` 标志 → 服务器生成一段中文 AI 摘要（或调用大模型）→ 板子屏幕显示回复；
5. 电脑浏览器打开仪表盘，实时看板子的姿态球、|a| 曲线、事件流和 AI 对话。

## 目录

| 路径 | 内容 |
|---|---|
| `server/server.py` | 电脑服务器（仅 Python 标准库，无需 pip 安装） |
| `device/` | 开发板 ESP-IDF 工程（ESP-IDF v5.4.x，目标 esp32s3） |
| `device/main/` | `main.c` 启动、`accel_input.c` IMU驱动（SC7A20/LIS3DH/MPU6050/QMA7981 自动识别）、`wifi_link.c`、`transport.c` HTTP遥测、`ui.c` LVGL界面 |
| `device/main/Kconfig.projbuild` | WiFi 账号、服务器 URL、上报周期（占位默认值，用 menuconfig 配自己的） |
| `tools/gen_font.py` | **必跑**：中文字体子集生成器（生成 git-ignored 的 `device/main/rw1_font.c`） |
| `build_device.bat` / `flash_device.bat` | 干净环境编译 / 烧录+串口抓取（参数化 COM 口与秒数） |
| `tools_serial_capture.py` | 非交互串口抓取（复位→打印N秒→退出，需 pyserial） |
| LICENSE / NOTICE | MIT；第三方组件与字体授权说明 |

以下内容由构建/组件管理器生成、**不入库**：`device/build/`、`device/managed_components/`、`device/sdkconfig`、`device/main/rw1_font.*`。

## 首次上手（4 步）

```powershell
# ① 生成中文字体（任意带 fontTools 的 Python；从本机字体裁剪，见 NOTICE 授权说明）
python tools/gen_font.py

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

### API 协议

| 方法/路径 | 说明 |
|---|---|
| `POST /api/telemetry` | 请求 `{x,y,z,source,ask,q}` → 响应 `{ok,activity,reply}` |
| `GET /api/latest` | 快照（状态 + 8s 样本 + 事件流），JSON |
| `GET /api/stream` | SSE 实时推送（仪表盘用） |
| `GET /` | 网页仪表盘 |

### 可选：接入真实大模型

不配也能完整跑通（内置规则 AI 会给出真实分析摘要）。配置后"提问"改为大模型生成：

```powershell
set RW1_LLM_API_KEY=sk-xxx
set RW1_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 或任意 OpenAI 兼容端点
set RW1_LLM_MODEL=qwen-turbo
python server\server.py
```

## 常见问题

| 现象 | 处理 |
|---|---|
| 屏幕显示"服务器:无连接" | 服务器没启动 / 防火墙拦截 / `RW1_SERVER_URL` IP 不对 |
| 连不上 WiFi（反复 retry） | 路由器侧问题（密码改了/AP重启/信道）；板子会自动无限重试，恢复后自动重连 |
| 编译报 `rw1_font.c missing` | 先跑 `python tools/gen_font.py`（见首次上手①） |
| 首次编译卡在拉取组件 | 离线场景拷入 `managed_components/`；在线场景检查能否访问 components.espressif.com |
| 中文显示为方块/乱码 | LVGL 自带"演示CJK字体"缺字严重，不要启用；用本仓库的 gen_font 子集 + tiny_ttf 方案。改了显示文案后重跑 gen_font 再编译 |
| 改 WiFi/服务器地址 | `idf.py menuconfig` → `RW1 AI Interaction`（值只存在本地 sdkconfig，不会被提交） |

## License

MIT（见 LICENSE）。注意 NOTICE：内嵌中文字体由你本机字体裁出，发布固件前请换用开源字体（如思源黑体/Noto Sans SC，OFL 协议）。

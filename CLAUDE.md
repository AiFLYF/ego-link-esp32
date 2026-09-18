# CLAUDE.md

本仓库的完整工作规则在 [AGENTS.md](AGENTS.md)（Git Flow 分支铁律 + 多 agent 共享记忆协议），
开始任何任务前必须先读：

@AGENTS.md

## 项目速览

- `server/server.py`：PC 端服务器，**仅 Python 标准库**，无 pip 依赖；端口 8000
- `tools/verify_server.py`：回归测试（27 项断言，无需硬件/真大模型），服务端与工具改动必跑
- `device/`：ESP32-S3-EYE 固件（ESP-IDF v5.4.3）；编译用 `build_device.bat`，
  Git Bash 环境改用 `python tools/idf_build.py -D SDKCONFIG_DEFAULTS=sdkconfig.bsp.esp32_s3_eye build`
- `tools/fake_board.py`：无硬件灌数据（idle/tilt/walk/shake/fall/mixed）
- 改板端中文文案后必须重跑 `python tools/gen_font.py` 再编译（字体产物 git-ignored）
- `.agents/`：多会话共享记忆（sessions / worklog / inbox），看板命令
  `python tools/agents_status.py --fetch`

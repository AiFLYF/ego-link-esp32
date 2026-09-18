# `.agents/` — 多 Agent 共享记忆区

这里是所有 AI 编码会话之间的**公告板 + 交接班本**，随 git 跟踪、通过远程仓库同步。
行为规则见根目录 [AGENTS.md](../AGENTS.md)，本文件只说明文件格式。

## 目录结构

```
.agents/
├── README.md                 # 本文件
├── sessions/                 # 每个活跃会话一个文件：我是谁、在哪个分支、心跳
│   ├── _template.md
│   └── <会话ID>.md
├── worklog/                  # 每天一个文件，只追加：做完了什么
│   └── YYYY-MM-DD.md
└── inbox/                    # 每个问题/交接一个文件：问其他 agent 的事
    ├── _template.md
    └── Q-YYYY-MM-DD-NN.md
```

为什么这样分片：多个 agent 同时写同一个文件必然冲突。**一会话一文件、一问题一文件、
一天一文件**，各写各的，合并时取并集即可。

## 会话文件 `sessions/<会话ID>.md`

- 文件名即会话 ID：`<agent>-<YYMMDD-HHMM>-<4位随机>`，如 `trae-260918-1115-a3f9`
- 开头的键值块供 `tools/agents_status.py` 解析，**键名不要改**，时间一律
  `YYYY-MM-DD HH:MM`（本机时区，24 小时制）
- status：`active`（干活中）/ `blocked`（被卡住，见 inbox）/ `done`（已完成）/ `abandoned`
- heartbeat：每 ≤ 15 分钟或每完成一阶段更新；超过 15 分钟没更新视为失活
- 收工把 status 置 `done`；没做完要走，填 `handoff` 并在 inbox 留交接问题

## 工作日志 `worklog/YYYY-MM-DD.md`

- **只追加，不修改历史条目**（订正就新追加一条并注明）
- 每条用二级标题 `## HH:MM · <会话ID>`，正文写：分支、做了什么、commit、测试结果
- 粒度：一个可汇报的阶段一条，不用每条命令都记

## 问事板 `inbox/Q-YYYY-MM-DD-NN.md`

- 一个文件一件事；`to:` 写会话 ID 或 `any`
- status：`open` → `answered`（回复写进同文件 `## 回复`）
- 提问三要素：**背景 / 我已尝试 / 需要你做什么**
- 开工先看有没有点名自己的 open 问题；离开前把能答的答掉

## 查看全景

```powershell
git fetch origin --prune
python tools/agents_status.py          # 活跃会话 + 今日日志 + 未答问题（自动扫所有远程分支）
```

## 注意

- 这里会被推到 GitHub：**不要写密钥、WiFi 密码、API key**
- 与代码放同一个 feature 分支提交，注册/更新后立即 push，别人才看得见

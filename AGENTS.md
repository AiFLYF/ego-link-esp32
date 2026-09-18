# AGENTS.md — 多 Agent 协作总章（Git Flow + 共享记忆协议）

> 本文件是所有 AI 编码 agent（Trae / WorkBuddy / Claude Code / 其他）在本仓库工作的
> **唯一权威规则**，随仓库 git 历史同步。各平台的本地入口文件（`.workbuddy-ai/`、
> `.claude/commands/`、`CLAUDE.md`）都只是本文件的入口或镜像；
> **一旦内容不一致，一律以本文件为准**。修改协作规则只能改本文件。

你是一个严格遵守 Git Flow 工作流的开发助手。你的首要职责不是快，而是
**不破坏仓库、不与其他 agent 互相覆盖工作**，在此前提下完成任务。

---

## 1. 分支铁律

| 分支 | 用途 | 允许的合并来源 |
|---|---|---|
| `main` | 生产分支 | 仅 `release/*`、`hotfix/*` |
| `develop` | 开发主干 | 仅 `feature/*`、`release/*`、`hotfix/*` 的 PR |
| `feature/*` | 新功能/改动，从 `develop` 切出 | 完成后 PR 合回 `develop` |
| `release/*` | 发版准备，从 `develop` 切出 | 合并到 `main` 和 `develop` |
| `hotfix/*` | 线上紧急修复，从 `main` 切出 | 合并到 `main` 和 `develop` |

**禁止操作（无例外）**：

- 禁止直接 commit / push 到 `main` 或 `develop`（所有改动走 PR）
- 禁止 `git push --force` / `--force-with-lease` 到任何共享分支
- 禁止删除、移动、重建 `.git` 目录；禁止对共享分支做历史改写（rebase 已推送分支等）
- 禁止删除别的 agent 的分支、stash、session 记录
- 禁止测试未通过就提交、推送、开 PR
- 禁止 `git add -A` / `git add .`：只 add 本任务相关文件，防止把
  `docs/index.original.html`、`device/sdkconfig`、本地字体产物等误入库
- 禁止把密钥、WiFi 密码、API key 写进任何被 git 跟踪的文件（含 `.agents/`）

---

## 2. Commit 规范

格式：`type(scope): 描述`

- `type`：`feat` / `fix` / `docs` / `refactor` / `test` / `chore`
- `scope`：受影响模块，如 `device` / `server` / `tools` / `git` / `agents`
- 描述用中文祈使句，一行 ≤ 50 字符；背景用正文段落说明，说明 **为什么**
- 一个 commit 只做一件事；半成品不要 commit

---

## 3. 标准开发流程（每个任务都按此执行）

```
开工 ─► 协调检查 ─► 切分支 ─► 开发 ─► 测试 ─► 提交/更新记忆 ─► 推送 ─► PR ─► 汇报
```

1. **开工协调（必做，先于任何写操作）**
   - `git fetch origin --prune`
   - `python tools/agents_status.py`（聚合所有远程分支上的会话心跳、工作日志、待答问题）
   - 读 `.agents/inbox/` 中点名给你或 `to: any` 的未答问题
   - 读 `.agents/worklog/` 最近 1–2 天日志，了解别人刚做了什么
   - 若已有活跃会话占用同一分支/同一批文件：**不要抢**，在 inbox 留问或等其失活
2. **切到 develop 并拉最新**：`git switch develop && git pull --ff-only`
3. **创建分支**：`git switch -c feature/<任务名>`（任务名用小写英文+连字符）
4. **注册会话**：复制 `.agents/sessions/_template.md` 创建自己的 session 文件，
   随**第一个 commit** 提交并**立即 push**（让其他 agent 第一时间看见你）
5. **开发并 commit**：只 add 本任务文件；每完成一个阶段更新 session 心跳与进度
6. **运行测试（提交前门禁）**
   - 服务端/工具/文档改动：`python tools/verify_server.py`（当前 27 项断言）
   - 固件改动：`build_device.bat`（或 `python tools/idf_build.py ... build`）编译通过
   - 脚本改动：先跑脚本的 `--selfcheck`（如有）
   - 不相关的测试也必须保持绿；测试失败先修代码，不许改测试迁就
7. **推送 + 更新记忆**：每次 push 前更新 session 文件（heartbeat/进度），
   与代码放同一 commit；push 要及时（让协作信息有意义）
8. **创建 PR 到 develop**：`gh pr create --base develop ...`，PR 描述含改了什么、为什么、测试证据
9. **收尾**：PR 合并后把 session 置 `done`、在 worklog 写总结（可随下次 PR 或单独 docs PR）
10. **每次操作后向用户汇报四件套**：当前分支 / commit message / 变更文件 / 测试结果

---

## 4. 多 Agent 共享记忆协议

共享记忆全部放在 **`.agents/`（复数，被 git 跟踪，通过远程仓库同步）**，分三类，
一类一个文件，天然避免多人改同一文件的冲突。

> 命名不要搞混：**`.agents/`（复数）= 团队共享、入库**；`.agent/`（单数）与
> `.claude/`、`.workbuddy-ai/` 一样是**各机本地私有目录（.gitignore 已忽略，不入库）**。
> 只有放进 `.agents/` 的内容才会同步给其他 agent。

| 路径 | 作用 | 写入规则 |
|---|---|---|
| `.agents/sessions/<会话ID>.md` | 谁在哪个分支做什么、心跳 | 每个会话只改自己的文件 |
| `.agents/worklog/<YYYY-MM-DD>.md` | 当日已完成事项的时间线 | 只追加（append-only），末尾追加 |
| `.agents/inbox/Q-<日期>-<序号>.md` | 给其他 agent 的问题/提示/交接 | 一个问题一个文件，回复写进同一文件 |

**会话 ID**：`<agent>-<YYMMDD-HHMM>-<4位随机>`，如 `trae-260918-1115-a3f9`、
`buddy-260918-1042-7f3c`、`claude-260918-1420-91bd`。

**心跳与失活**：

- 活跃期间每 ≤ 15 分钟、或每个阶段完成时更新一次 `heartbeat` 并 push
- 心跳超过 **15 分钟** 视为失活（stale）。要接管失活会话的分支：
  先在 inbox 发一条接管声明（等 5 分钟无反对）→ 从其分支切新分支继续，
  **保留并注明其全部工作**，不得删除其提交

**跨分支可见性**：未合并分支上的记忆文件，其他人通过
`python tools/agents_status.py`（自动扫描 `origin/develop` 与全部
`origin/feature/*、release/*、hotfix/*`）读取。所以**注册和更新后必须立刻 push**。

**合并冲突处理**：`.agents/` 文件冲突时一律取**并集**——保留所有会话条目、
所有日志段落、所有问答，只能多不能少。

**提问礼仪**：

- 标题写明对象与主题；`to:` 写具体会话 ID 或 `any`
- 一个文件只问一件事；问题要给出背景、你已尝试什么、需要对方做什么
- 被问方把答案写进同一文件的 `## 回复`，并把 `status` 改为 `answered`
- 离开前扫一眼 inbox：能答的顺手答，答不了写明你预计何时回来

**交接（handoff）**：任务没做完要离开时，在 session 文件写清
`下一步 / 阻塞点 / 相关 PR / 未提交文件位置`，并在 inbox 给接手方留一条。

---

## 5. 事故应急规则（来自 2026-09-18 真实事故：两个会话互删 .git、远程被重建）

出现以下任一**异常信号**，立即停止一切写操作（只读排查 + 通知用户）：

- `git status` 报 `not a git repository`，但目录里明明有 `.git`
- 出现**不是你创建**的分支 / unborn 分支 / 暂存区
- `git fetch` 后远程分支 hash 大量变化、你推过的分支消失
- `.git/refs`、`.git/objects` 的修改时间在**当前时刻附近跳动**（有别的 git 进程在跑）
- 工作区文件被大面积回退（文件大小、内容与你已知历史不符）

应急动作：

1. **停手**：不 init、不 reset、不删 `.git`、不 force push、不再 `git checkout`
2. 用只读命令取证（`git ls-remote`、`Get-Item .git`、列分支与时间线）
3. 等 `.git` 静默 ≥ 2 分钟，或请用户确认另一个会话已停止
4. 在 `.agents/sessions/`、`.agents/inbox/` 记录事故时间线
5. 恢复分支对齐需要 `reset --hard` 等破坏性操作时，**必须先向用户说明风险并获明确授权**

---

## 6. 环境速查（本仓库）

- 服务端：`server/server.py`（仅 Python 标准库）；回归测试 `python tools/verify_server.py`
- 固件：ESP-IDF v5.4.3，`device/`；编译 `build_device.bat`（Git Bash 里用 `python tools/idf_build.py -D SDKCONFIG_DEFAULTS=sdkconfig.bsp.esp32_s3_eye build`）
- 无硬件调试：`python tools/fake_board.py --scenario mixed`
- 中文显示前先跑 `python tools/gen_font.py`（产物 git-ignored）
- 本机访问 GitHub 不稳：推送失败先重试；只有用户明确提供代理时才用代理

## 7. 各平台入口

- **Claude Code**：仓库根 `CLAUDE.md`（入库、自动加载）已引入本文件全部规则。
  斜杠命令 `/git-flow <任务描述>` 为**可选本地安装**：`.claude/` 已被 .gitignore 忽略，
  需要时把入库的 [docs/agents/claude-git-flow-command.md](docs/agents/claude-git-flow-command.md)
  复制为 `.claude/commands/git-flow.md` 即可。
- **WorkBuddy**：本地入口 `.workbuddy-ai/git-flow-agent.md`（git-ignored 镜像）。
  若其与本文件不一致，以本文件为准并用本文件覆盖镜像
- 任何平台的 agent：第一次开工时把本文件完整读入，再开始第 3 节流程

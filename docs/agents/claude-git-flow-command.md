# Claude Code `/git-flow` 斜杠命令（本地安装源）

`.claude/` 已在 `.gitignore` 中（本地私有，不入库），所以命令文件通过本入库文档分发。
首次使用执行一次：

```powershell
New-Item -ItemType Directory -Force -Path .claude\commands | Out-Null
Copy-Item docs\agents\claude-git-flow-command.md .claude\commands\git-flow.md
```

之后在 Claude Code 里输入 `/git-flow <任务描述>` 即可。若 AGENTS.md 更新了规则，
重新复制一次本文件即可（规则本体在 AGENTS.md，本文件只是入口）。

---

<!-- ===== 以下内容复制为 .claude/commands/git-flow.md ===== -->

---
description: 按 Git Flow + 多 agent 共享记忆协议执行一个完整开发任务（开分支→注册→开发→测试→PR）
argument-hint: <任务描述>
---

@AGENTS.md

# 任务

$ARGUMENTS

# 执行要求

严格按 AGENTS.md 第 3 节「标准开发流程」执行，并在每一步用中文向用户汇报
**当前分支 / commit message / 变更文件 / 测试结果**。

开工检查清单（任何写操作之前）：

1. `git fetch origin --prune`
2. `python tools/agents_status.py` —— 确认没有活跃会话占用同一分支或同一批文件；
   若有冲突，先在 `.agents/inbox/` 留问，不要抢活
3. 读 `.agents/inbox/` 中点名 claude / any 的未答问题与近两天 worklog

随后：从最新 `develop` 切 `feature/<任务名>` → 复制
`.agents/sessions/_template.md` 注册本会话（ID `claude-<YYMMDD-HHMM>-<4位随机>`）
→ 开发（只 add 本任务文件，禁止 `git add -A`）→ 跑测试门禁 → 首个 commit 立即 push →
每 ≤15 分钟更新心跳并推送 → `gh pr create --base develop`（PR 描述写改动/原因/测试证据）
→ session 置 done、worklog 追加总结。

遇到 AGENTS.md 第 5 节的异常信号（.git 被别的进程修改、出现来历不明的分支、
远程历史突变）时立即停手，只读取证并通知用户，不得 init / reset / force push / 删 .git。

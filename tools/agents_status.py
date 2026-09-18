#!/usr/bin/env python3
"""多 agent 协作看板：聚合 .agents/ 下的会话心跳、工作日志、未答问题。

数据来源（只读，不修改任何东西）：
  1. 工作区里的 .agents/（含未提交的本地改动）
  2. origin/develop 与所有 origin/feature|release|hotfix/* 分支上的 .agents/
这样未合并分支里的注册信息也能被其他 agent 看到。

用法：
  python tools/agents_status.py            # 看板（建议先 git fetch origin --prune）
  python tools/agents_status.py --fetch    # 先自动 fetch 再看
  python tools/agents_status.py --selfcheck
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

STALE_MINUTES = 15
REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ".agents"
TIME_FMT = "%Y-%m-%d %H:%M"


def git(*args: str) -> str:
    p = subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip())
    return p.stdout.decode("utf-8", "replace")


def parse_kv_header(text: str) -> tuple[str, dict[str, str]]:
    """解析 markdown 头部的 '- key: value' 块，返回(一级标题, 键值字典)。"""
    title = ""
    kv: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            break
        if s.startswith("# "):
            title = s[2:].strip()
        elif s.startswith("- "):
            body = s[2:]
            if ":" in body:
                k, v = body.split(":", 1)
                kv[k.strip().lower()] = v.strip()
    return title, kv


def parse_time(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, TIME_FMT)
    except ValueError:
        return None


@dataclass
class Session:
    sid: str
    title: str
    kv: dict[str, str]
    sources: set[str] = field(default_factory=set)

    @property
    def heartbeat(self) -> datetime | None:
        return parse_time(self.kv.get("heartbeat", ""))

    @property
    def status(self) -> str:
        return self.kv.get("status", "?")


@dataclass
class Question:
    qid: str
    title: str
    kv: dict[str, str]
    sources: set[str] = field(default_factory=set)


@dataclass
class WorklogDay:
    day: str
    text: str
    sources: set[str] = field(default_factory=set)


def collect_sources() -> list[tuple[str, str, str]]:
    """返回 (来源标签, 相对路径, 文本) 列表；来源含工作区与相关远程分支。"""
    items: list[tuple[str, str, str]] = []

    local = REPO_ROOT / AGENTS_DIR
    if local.is_dir():
        for p in sorted(local.rglob("*.md")):
            rel = p.relative_to(REPO_ROOT).as_posix()
            try:
                items.append(("worktree", rel, p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass

    refs: list[str] = []
    try:
        out = git("for-each-ref", "--format=%(refname)", "refs/remotes/origin")
    except RuntimeError:
        return items
    for line in out.splitlines():
        name = line.strip()
        short = name.removeprefix("refs/remotes/origin/")
        if short == "develop" or short.startswith(("feature/", "release/", "hotfix/")):
            refs.append((name, short))

    for ref, short in refs:
        try:
            listing = git("ls-tree", "-r", "--name-only", ref, "--", AGENTS_DIR)
        except RuntimeError:
            continue
        for path in listing.splitlines():
            path = path.strip()
            if not path.endswith(".md") or path.endswith("README.md"):
                continue
            try:
                blob = subprocess.run(
                    ["git", "show", f"{ref}:{path}"],
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if blob.returncode == 0:
                    items.append(
                        (f"origin/{short}", path, blob.stdout.decode("utf-8", "replace"))
                    )
            except RuntimeError:
                continue
    return items


def build_board(items: list[tuple[str, str, str]], now: datetime):
    sessions: dict[str, Session] = {}
    questions: dict[str, Question] = {}
    worklogs: dict[str, WorklogDay] = {}

    for source, path, text in items:
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != AGENTS_DIR:
            continue
        kind, fname = parts[1], parts[2]
        if fname.startswith("_"):
            continue

        if kind == "sessions":
            sid = fname[:-3]
            title, kv = parse_kv_header(text)
            cur = sessions.get(sid)
            if cur is None:
                cur = Session(sid=sid, title=title, kv=kv)
                sessions[sid] = cur
            cur.sources.add(source)
            hb_new = parse_time(kv.get("heartbeat", ""))
            hb_old = parse_time(cur.kv.get("heartbeat", ""))
            if hb_new and (hb_old is None or hb_new >= hb_old):
                cur.kv, cur.title = kv, title

        elif kind == "inbox":
            qid = fname[:-3]
            title, kv = parse_kv_header(text)
            cur = questions.get(qid)
            if cur is None:
                cur = Question(qid=qid, title=title, kv=kv)
                questions[qid] = cur
            cur.sources.add(source)
            ct_new = parse_time(kv.get("answered", "")) or parse_time(kv.get("created", ""))
            ct_old = parse_time(cur.kv.get("answered", "")) or parse_time(
                cur.kv.get("created", "")
            )
            if ct_new and (ct_old is None or ct_new >= ct_old):
                cur.kv, cur.title = kv, title

        elif kind == "worklog":
            day = fname[:-3]
            cur = worklogs.get(day)
            if cur is None:
                worklogs[day] = WorklogDay(day=day, text=text, sources={source})
            else:
                cur.sources.add(source)
                if len(text) > len(cur.text):
                    cur.text = text

    return sessions, questions, worklogs


def render(sessions, questions, worklogs, now: datetime) -> str:
    lines: list[str] = []
    cutoff = now - timedelta(minutes=STALE_MINUTES)
    today, yesterday = now.strftime("%Y-%m-%d"), (now - timedelta(days=1)).strftime("%Y-%m-%d")

    active, stale, done_today = [], [], []
    for s in sorted(sessions.values(), key=lambda x: x.heartbeat or datetime.min, reverse=True):
        if s.status in ("done", "abandoned"):
            hb = s.heartbeat
            if hb and hb.strftime("%Y-%m-%d") == today:
                done_today.append(s)
            continue
        hb = s.heartbeat
        (active if hb and hb >= cutoff else stale).append(s)

    lines.append("=" * 68)
    lines.append(f"多 agent 协作看板 · {now.strftime(TIME_FMT)} · 活跃判定 {STALE_MINUTES} 分钟内心跳")
    lines.append("=" * 68)

    lines.append(f"\n[活跃会话] {len(active)}")
    if not active:
        lines.append("  （无）")
    for s in active:
        lines.append(f"  ● {s.sid}")
        lines.append(f"      分支: {s.kv.get('branch', '?')}   agent: {s.kv.get('agent', '?')}")
        lines.append(f"      任务: {s.kv.get('task', '?')}")
        lines.append(f"      心跳: {s.kv.get('heartbeat', '?')}   PR: {s.kv.get('pr') or '无'}")
        lines.append(f"      来源: {', '.join(sorted(s.sources))}")

    lines.append(f"\n[失活/卡住] {len(stale)}（心跳超时，接管前请先在 inbox 发声明）")
    for s in stale:
        lines.append(
            f"  ○ {s.sid} [{s.status}] 分支 {s.kv.get('branch', '?')} "
            f"最后心跳 {s.kv.get('heartbeat', '?')}"
        )

    open_q = [q for q in questions.values() if q.kv.get("status", "open") != "answered"]
    lines.append(f"\n[未答问题] {len(open_q)}")
    if not open_q:
        lines.append("  （无）")
    for q in sorted(open_q, key=lambda x: x.kv.get("created", ""), reverse=True):
        lines.append(f"  ? {q.qid}: {q.title.removeprefix(q.qid + ':').strip() or q.title}")
        lines.append(f"      {q.kv.get('from', '?')} → {q.kv.get('to', '?')}（{q.kv.get('created', '?')}）")

    if done_today:
        lines.append(f"\n[今日已收尾] {', '.join(s.sid for s in done_today)}")

    for day in (today, yesterday):
        w = worklogs.get(day)
        if not w:
            continue
        heads = [ln for ln in w.text.splitlines() if ln.startswith("## ")]
        lines.append(f"\n[工作日志 {day}] {len(heads)} 条（.agents/worklog/{day}.md）")
        for h in heads[-8:]:
            lines.append(f"  {h[3:]}")

    lines.append("\n操作提示: git fetch origin --prune; python tools/agents_status.py")
    return "\n".join(lines) + "\n"


def selfcheck() -> int:
    sample_session = """# Session: demo-260918-1115-ab12

- agent: trae
- branch: feature/x
- task: 自检样例
- status: active
- started: 2026-09-18 10:00
- heartbeat: 2026-09-18 11:15
- pr:

## 当前进度
- 无关正文
"""
    sample_inbox = """# Q-2026-09-18-99: 自检问题

- from: a
- to: any
- status: open
- created: 2026-09-18 11:00
- answered:
"""
    title, kv = parse_kv_header(sample_session)
    assert kv["agent"] == "trae" and kv["branch"] == "feature/x", kv
    assert parse_time(kv["heartbeat"]) == datetime(2026, 9, 18, 11, 15)
    assert title == "Session: demo-260918-1115-ab12"

    items = [
        ("worktree", ".agents/sessions/demo-260918-1115-ab12.md", sample_session),
        ("origin/feature/x", ".agents/sessions/demo-260918-1115-ab12.md", sample_session),
        ("origin/feature/x", ".agents/inbox/Q-2026-09-18-99.md", sample_inbox),
        ("worktree", ".agents/sessions/_template.md", "# x\n"),
    ]
    sessions, questions, worklogs = build_board(items, datetime(2026, 9, 18, 11, 20))
    assert len(sessions) == 1, "模板文件应被跳过且会话应按 ID 去重"
    assert sessions["demo-260918-1115-ab12"].sources == {"worktree", "origin/feature/x"}
    assert len(questions) == 1 and questions["Q-2026-09-18-99"].kv["status"] == "open"
    out = render(sessions, questions, worklogs, datetime(2026, 9, 18, 11, 20))
    assert "demo-260918-1115-ab12" in out and "自检问题" in out
    print("selfcheck: PASS（解析/去重/聚合/渲染 4 组断言通过）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="多 agent 协作看板（.agents/ 聚合器）")
    ap.add_argument("--fetch", action="store_true", help="先执行 git fetch origin --prune")
    ap.add_argument("--selfcheck", action="store_true", help="内置解析自检")
    args = ap.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.selfcheck:
        return selfcheck()

    if args.fetch:
        try:
            git("fetch", "origin", "--prune")
        except RuntimeError as e:
            print(f"[warn] fetch 失败（继续用本地缓存的远程信息）: {e}", file=sys.stderr)

    if not (REPO_ROOT / ".git").exists():
        print("[error] 这里不是 git 仓库根目录；按 AGENTS.md 第 5 节，仓库异常时应停手排查。",
              file=sys.stderr)
        return 2

    items = collect_sources()
    sessions, questions, worklogs = build_board(items, datetime.now())
    sys.stdout.write(render(sessions, questions, worklogs, datetime.now()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

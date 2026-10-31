#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSONL 落盘：遥测与事件的异步追加写。

**为什么单独一个文件**（2026-11-01 从 server.py 拆出）：

这个类和 HTTP、IMU、指令通道**一点关系都没有** —— 依赖图算下来它引用的
模块级名字是 **0 个**。留在 server.py 里唯一的效果是让那个文件更长。
拆出来之后它可以被单独 import、单独测（`JsonlLogger(tmpdir)` 就行）。

设计要点（都是踩过坑之后定下来的，别随手改）：

- 写盘在后台线程里做，业务线程只往队列里丢一条记录 —— 磁盘慢/满都不拖慢遥测。
- 队列满时**丢最旧的**，不是丢最新的（2026-10-12 订正过，原来写反了）。
- `close()` 会把队列排空再退出 —— 否则 Ctrl-C 会把最多 2 万条没落盘的记录扔掉。
- `purge_old()` 必须是**公开名**：看护线程调的就是 `LOGGER.purge_old()`。
  2026-10-25 它曾叫 `_purge_old`，服务端连跑 24 小时后那里抛 AttributeError，
  **看护线程整个死掉**（离线判定 / 指令超时 / 日志清理全部静默停摆）。
  `tools/verify_server.py` 第 [0] 节因此加了一条静态门禁点名这个方法的存在。
"""
import datetime
import json
import os
import queue
import threading
import time


class JsonlLogger:
    """把遥测与事件追加到 <root>/telemetry-YYYY-MM-DD.jsonl 与 events-*.jsonl。

    写盘在后台线程里做，业务线程只往队列里丢一条记录，所以磁盘慢/满都不会
    拖慢遥测响应。队列满时丢最旧的（宁可丢日志也不丢实时性）。
    """

    def __init__(self, root, retain_days=7, log_telemetry=True, log_hz=10.0):
        self.root = root
        self.retain_days = retain_days
        self.log_telemetry = log_telemetry
        self.log_hz = max(1.0, float(log_hz))
        self.q = queue.Queue(maxsize=20000)
        self.dropped = 0
        self.written = 0
        self._fhs = {}                 # path -> 文件句柄（遥测与事件是两个文件，
                                       # 必须各持一个句柄，否则会互相串写）
        os.makedirs(root, exist_ok=True)
        self.purge_old()
        # 句柄留给 close()：退出时要等这条线程把队列排空，
        # 否则 Ctrl-C 会把最多 2 万条还没落盘的记录直接扔掉
        # （2026-10-30 前的实际行为：daemon 线程随进程一起消失）。
        self._thread = threading.Thread(target=self._run, name="jsonl", daemon=True)
        self._thread.start()

    # ---- 生产者 ----------------------------------------------------------
    def telemetry(self, ts, device, source, pts, dt):
        if not self.log_telemetry or not pts:
            return
        hz = 1.0 / dt if dt > 0 else 0.0
        step = max(1, int(round(hz / self.log_hz))) if hz else 1
        thin = pts[::step]
        self._put({
            "k": "t",
            "ts": round(ts, 3),
            "dev": device,
            "src": source,
            "hz": round(hz, 1),
            "s": [[round(x, 3), round(y, 3), round(z, 3)] for (x, y, z) in thin],
        })

    def event(self, device, kind, text):
        self._put({"k": "e", "ts": round(time.time(), 3), "dev": device,
                   "kind": kind, "text": text})

    def _put(self, rec):
        """入队。队列满时**丢最旧的**再进新的（类文档承诺的语义：
        "宁可丢日志也不丢实时性" —— 旧日志的价值低于新日志，课堂掉线重连时
        newest 恰是最该留下的现场）。

        2026-10-12 订正：原实现是 put_nowait 失败就 dropped += 1 直接返回，
        丢的是**最新**一条，与上面的承诺正好相反。
        这里用有界重试而不是无锁 get+put：两个生产者线程（遥测/事件）可能
        在同一毫秒都撞上 Full，简单的"出一个进一个"在极端时序下仍会丢新的。
        """
        try:
            self.q.put_nowait(rec)
            return
        except queue.Full:
            pass
        for _ in range(3):
            try:
                self.q.get_nowait()          # 丢最旧的
                self.dropped += 1
            except queue.Empty:
                pass                         # 被消费线程抢先取走了，静默即可
            try:
                self.q.put_nowait(rec)
                return
            except queue.Full:
                continue
        self.dropped += 1                    # 抢了三次还没进（消费线程停摆），丢新的

    # ---- 消费者 ----------------------------------------------------------
    def _run(self):
        while True:
            rec = self.q.get()
            if rec is None:
                break
            try:
                self._write(rec)
            except OSError:
                pass          # 磁盘满/权限问题不该拖垮服务器

    def _path(self, prefix, ts):
        day = datetime.date.fromtimestamp(ts).isoformat()
        return os.path.join(self.root, "%s-%s.jsonl" % (prefix, day))

    def _write(self, rec):
        day = datetime.date.fromtimestamp(rec["ts"]).isoformat()
        path = self._path("telemetry" if rec["k"] == "t" else "events", rec["ts"])
        fh = self._fhs.get(path)
        if fh is None:
            # 跨天时把昨天的句柄关掉，别让长跑的服务攒一堆打开的 fd
            for old in [p for p in self._fhs if day not in p]:
                try:
                    self._fhs.pop(old).close()
                except OSError:
                    pass
            fh = open(path, "a", encoding="utf-8")
            self._fhs[path] = fh
        fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        self.written += 1

    def purge_old(self):
        """删掉超过保留期的日志。构造时跑一次；之后看护线程每天调一次（P1-11）。

        ⚠️ 必须是**公开名**：watchdog 调的就是 `LOGGER.purge_old()`。
        2026-10-25 事故：这个方法曾叫 `_purge_old`，而看护线程调的是不带下划线的
        名字 —— 服务端连跑 24 小时后那里抛 AttributeError，**看护线程整个死掉**
        （设备离线判定 / 指令超时 / 日志清理全部静默停摆）。短期回归全测不到它，
        谁也不会把测试跑满 24 小时；tools/verify_server.py 第 [0] 节因此加了
        一条静态门禁点名这个方法的存在。
        """
        if self.retain_days <= 0:
            return
        cutoff = time.time() - self.retain_days * 86400
        for name in os.listdir(self.root):
            if not (name.startswith("telemetry-") or name.startswith("events-")):
                continue
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    print("   [日志] 已清理过期文件 %s" % name)
            except OSError:
                pass

    def stats(self):
        files = []
        total = 0
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".jsonl"):
                continue
            size = os.path.getsize(os.path.join(self.root, name))
            files.append({"name": name, "bytes": size})
            total += size
        return {"dir": self.root, "files": files, "bytes": total,
                "written": self.written, "dropped": self.dropped}

    def close(self, timeout=2.0):
        """把队列里剩下的记录写完再退出（Ctrl-C / SIGTERM 时调用）。

        原来 `_run` 是 daemon 线程，进程一退队列里最多 2 万条记录直接消失 ——
        而"停在旧数据上"恰恰发生在最需要现场的时候（正在演示、突然 Ctrl-C）。
        现在发一个哨兵让消费线程自然收尾，最多等 `timeout` 秒。

        ⚠️ 队列满的时候**不能**只是 `put_nowait(None)`（会 Full），
        要先腾出空位再放哨兵，否则消费线程永远等不到它。
        """
        if self._thread is None or not self._thread.is_alive():
            return 0
        for _ in range(3):
            try:
                self.q.put_nowait(None)
                break
            except queue.Full:
                try:
                    self.q.get_nowait()     # 腾一格（丢最旧的，与 _put 的语义一致）
                    self.dropped += 1
                except queue.Empty:
                    pass
        self._thread.join(timeout=timeout)
        for fh in list(self._fhs.values()):
            try:
                fh.close()
            except OSError:
                pass
        self._fhs.clear()
        return self.written

# -*- coding: utf-8 -*-
"""RAG Daemon 客户端：通过子进程 stdin/stdout 与常驻 daemon 通信。"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import ROOT_DIR


class RagClient:
    _instance: Optional["RagClient"] = None
    _lock = threading.Lock()

    def __init__(self, env_extra: Optional[dict] = None):
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False
        self._pending = {}  # id -> event
        self._results = {}  # id -> response
        self._counter = 0
        self._read_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        # 2026-11-05：角色扮演的 RAG 用**另一个** daemon（小模型 + 独立数据目录）。
        # 这里只存 spawn 时要覆盖的环境变量，不影响主 RAG 的单例。
        self._env_extra = dict(env_extra or {})

    @classmethod
    def get(cls) -> "RagClient":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def is_ready(self) -> bool:
        """daemon 已经加载完模型、可以接请求了吗。

        调用方拿这个判断「这一轮要不要检索」——不能用 `_call` 来试，
        那个会先 `start()`，冷启动要等十几秒，聊天会被拖住。
        """
        return bool(self._ready and self._proc and self._proc.poll() is None)

    def start(self) -> bool:
        """启动 daemon 进程。返回是否成功启动。"""
        if self._proc and self._proc.poll() is None:
            return self._ready

        python_exe = sys.executable
        daemon_script = Path(__file__).parent / "rag_daemon.py"

        env = os.environ.copy()
        env.update(self._env_extra)
        env["PYTHONUNBUFFERED"] = "1"

        self._proc = subprocess.Popen(
            [python_exe, "-u", str(daemon_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT_DIR),
            text=True,
            # 必须显式指定编码（2026-11-08）：Windows 上 `text=True` 默认按
            # GBK 解码，daemon 输出的是 UTF-8 JSON，遇到第一个非 GBK 字节
            # （比如「」这类）读线程直接 UnicodeDecodeError 崩掉。
            # 崩了之后 `_ready` 永远设不上，`is_ready()` 恒 False ——
            # 表现就是「daemon 未就绪，本轮不检索」，一条记忆都进不来，
            # 模型只能从窗口里编（实测把「门板被撞见」编成「第一次」）。
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()

        # 等 ready 信号（最多 60 秒，模型加载可能慢）
        start = time.time()
        while time.time() - start < 60:
            if self._ready:
                return True
            if self._proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def _read_loop(self):
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")

            # 初始 ready 信号
            if msg_id == "0" and msg.get("event") == "ready":
                self._ready = True
                continue

            # 唤醒等待的调用
            if msg_id in self._pending:
                self._results[msg_id] = msg
                self._pending[msg_id].set()

    def _stderr_loop(self):
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            # 直接透传到主进程 stderr
            print(line, file=sys.stderr, end="")

    def _call(self, cmd: str, timeout: float = 30, allow_start: bool = True,
              **kwargs) -> dict:
        """同步调用 daemon。

        `allow_start=False` 用于聊天轮这类**不能等冷启动**的场景：
        没 ready 就直接返回失败，绝不在这里拉起模型。
        """
        if not allow_start:
            if not self.is_ready():
                return {"ok": False, "error": "RAG daemon 未就绪"}
        elif not self.start():
            return {"ok": False, "error": "RAG daemon 启动失败"}

        if not self._proc or self._proc.poll() is not None:
            return {"ok": False, "error": "RAG daemon 已退出"}

        self._counter += 1
        req_id = str(self._counter)
        event = threading.Event()
        self._pending[req_id] = event

        req = {"id": req_id, "cmd": cmd, **kwargs}
        try:
            assert self._proc.stdin
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            self._pending.pop(req_id, None)
            return {"ok": False, "error": f"写入 daemon 失败: {e}"}

        # 等超时。默认 30 秒是给建库/灌库的；检索可以传更短，
        # 聊天轮不能为了等一条回忆卡住（2026-11-05）。
        if not event.wait(timeout=timeout):
            self._pending.pop(req_id, None)
            return {"ok": False, "error": "RAG daemon 调用超时"}

        result = self._results.pop(req_id, {"ok": False, "error": "无响应"})
        return result

    # ─── 便捷方法 ───────────────────────────────────

    def search(self, kb_name: str, query: str, k: int = 4,
               timeout: float = 30, allow_start: bool = True,
               where: Optional[dict] = None) -> dict:
        return self._call("search", kb_name=kb_name, query=query, k=k,
                          timeout=timeout, allow_start=allow_start,
                          where=where)

    def ingest(self, kb_name: str, entries: list[dict]) -> dict:
        return self._call("ingest", kb_name=kb_name, entries=entries)

    def list_kb(self) -> dict:
        return self._call("list_kb")

    def delete_kb(self, kb_name: str) -> dict:
        return self._call("delete_kb", kb_name=kb_name)

    def delete_entry(self, kb_name: str, entry_id: str) -> dict:
        return self._call("delete_entry", kb_name=kb_name, entry_id=entry_id)

    def delete_entries(self, kb_name: str, entry_ids: list[str]) -> dict:
        return self._call("delete_entries", kb_name=kb_name, ids=entry_ids)

    def list_entries(self, kb_name: str, timeout: float = 30) -> dict:
        return self._call("list_entries", kb_name=kb_name, timeout=timeout)

    def ping(self) -> dict:
        return self._call("ping")

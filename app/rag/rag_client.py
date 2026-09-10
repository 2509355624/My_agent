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

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False
        self._pending = {}  # id -> event
        self._results = {}  # id -> response
        self._counter = 0
        self._read_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    @classmethod
    def get(cls) -> "RagClient":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def start(self) -> bool:
        """启动 daemon 进程。返回是否成功启动。"""
        if self._proc and self._proc.poll() is None:
            return self._ready

        python_exe = sys.executable
        daemon_script = Path(__file__).parent / "rag_daemon.py"

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        self._proc = subprocess.Popen(
            [python_exe, "-u", str(daemon_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT_DIR),
            text=True,
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

    def _call(self, cmd: str, **kwargs) -> dict:
        """同步调用 daemon。"""
        if not self.start():
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

        # 等 30 秒超时
        if not event.wait(timeout=30):
            self._pending.pop(req_id, None)
            return {"ok": False, "error": "RAG daemon 调用超时"}

        result = self._results.pop(req_id, {"ok": False, "error": "无响应"})
        return result

    # ─── 便捷方法 ───────────────────────────────────

    def search(self, kb_name: str, query: str, k: int = 4) -> dict:
        return self._call("search", kb_name=kb_name, query=query, k=k)

    def ingest(self, kb_name: str, entries: list[dict]) -> dict:
        return self._call("ingest", kb_name=kb_name, entries=entries)

    def list_kb(self) -> dict:
        return self._call("list_kb")

    def delete_kb(self, kb_name: str) -> dict:
        return self._call("delete_kb", kb_name=kb_name)

    def delete_entry(self, kb_name: str, entry_id: str) -> dict:
        return self._call("delete_entry", kb_name=kb_name, entry_id=entry_id)

    def ping(self) -> dict:
        return self._call("ping")

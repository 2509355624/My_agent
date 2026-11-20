# -*- coding: utf-8 -*-
"""进程托管器：控制台背后的真正执行者。

设计约束：
- 所有子进程都用 CREATE_NO_WINDOW 启动，桌面不再出现一堆 cmd 窗口。
- 每个服务只认自己那一份 PID/端口，重启前先按进程树清理，不按窗口标题。
- 状态刷新只读端口/PID，不启动、不杀进程；启动/停止/重启都在后台线程做，
  接口立刻返回，前端轮询看黄灯变绿灯。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - 项目环境已装，缺了也能降级跑
    psutil = None

from app.features.control.specs import ROOT, ServiceSpec, default_specs, status_color, status_label

log = logging.getLogger("control")

QQ_EXE = Path(r"D:\APP\qq\QQ.exe")
_SYS32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _sys_tool(name: str) -> str:
    """系统命令走全路径，别依赖 PATH（有些 cmd 环境里 System32 不在 PATH）。"""
    path = _SYS32 / name
    return str(path) if path.is_file() else name


@dataclass
class ServiceState:
    status: str = "stopped"
    pid: int | None = None
    detail: str = "未启动"
    started_at: float | None = None
    restarts: int = 0
    last_error: str = ""


class ServiceManager:
    def __init__(self, specs: list[ServiceSpec] | None = None, root: str | Path | None = None):
        self.root = Path(root or ROOT)
        self.specs = {spec.id: spec for spec in (specs or default_specs())}
        self._order = sorted(self.specs.values(), key=lambda s: s.order)
        self._states = {spec.id: ServiceState() for spec in self.specs.values()}
        self._procs: dict[str, subprocess.Popen] = {}
        self._logs: dict[str, tuple] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._control_dir = self.root / "state" / "control"
        self._log_dir = self.root / "logs" / "control"
        self._control_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._adopt_existing()

    # ── 对外 ───────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            self._refresh_locked()
            services = [self._serialize(spec) for spec in self._order]
        summary = {
            "green": sum(1 for s in services if s["color"] == "green"),
            "yellow": sum(1 for s in services if s["color"] == "yellow"),
            "red": sum(1 for s in services if s["color"] == "red"),
            "total": len(services),
        }
        return {"ok": True, "services": services, "summary": summary, "ts": time.time()}

    def start(self, sid: str) -> dict:
        spec = self._need(sid)
        with self._lock:
            # 先刷新一次：外部刚起来的服务可能端口已通，但状态还停在 stopped；
            # 不刷新就直接 start 会多起一个实例。
            self._refresh_locked()
            if self._busy(sid):
                return {"ok": True, "status": self._states[sid].status, "busy": True}
            if self._states[sid].status in ("running", "waiting", "starting", "restarting"):
                return {"ok": True, "status": self._states[sid].status, "noop": True}
            self._states[sid].status = "starting"
            self._states[sid].detail = "启动中"
        self._thread(sid, lambda: self._start_worker(spec))
        return {"ok": True, "status": "starting"}

    def restart(self, sid: str) -> dict:
        """重启：先停旧进程树，再起新的；接口立刻返回，黄灯由 worker 推进。"""
        spec = self._need(sid)
        with self._lock:
            if self._busy(sid):
                return {"ok": True, "status": self._states[sid].status, "busy": True}
            self._states[sid].status = "restarting"
            self._states[sid].detail = "重启中"
            self._states[sid].restarts += 1
        self._thread(sid, lambda: self._restart_worker(spec))
        return {"ok": True, "status": "restarting"}

    def stop(self, sid: str) -> dict:
        spec = self._need(sid)
        with self._lock:
            if self._busy(sid):
                return {"ok": True, "status": self._states[sid].status, "busy": True}
            if self._states[sid].status in ("stopped", "stopping"):
                return {"ok": True, "status": "stopped", "noop": True}
            self._states[sid].status = "stopping"
            self._states[sid].detail = "停止中"
        self._thread(sid, lambda: self._stop_worker(spec))
        return {"ok": True, "status": "stopping"}

    def restart_all(self) -> dict:
        """全部重启：按依赖顺序逐个发起，前端统一看黄灯。"""
        with self._lock:
            for spec in self._order:
                if self._busy(spec.id):
                    continue
                self._states[spec.id].status = "restarting"
                self._states[spec.id].detail = "重启中"
                self._states[spec.id].restarts += 1
                self._thread(spec.id, lambda s=spec: self._restart_worker(s))
        return {"ok": True, "status": "restarting", "count": len(self._order)}

    # ── worker ─────────────────────────────────────────

    def _start_worker(self, spec: ServiceSpec):
        self._launch(spec, "启动失败")

    def _restart_worker(self, spec: ServiceSpec):
        try:
            self._stop_sync(spec)
        except Exception as e:                              # noqa: BLE001
            with self._lock:
                st = self._states[spec.id]
                st.status = "error"
                st.detail = "重启失败"
                st.last_error = "%s: %s" % (type(e).__name__, e)
                st.pid = None
            log.warning("停止 %s 失败，未重启：%s", spec.id, e)
            return
        self._launch(spec, "重启失败")

    def _launch(self, spec: ServiceSpec, fail_label: str):
        """起进程并等就绪；启动和重启共用，避免两份几乎一样的代码。"""
        try:
            # ComfyUI 启动要几十秒；端口起来前不能当成“没进程”再拉一套。
            # 否则控制台 + 看门狗 + QQ 机器人会各拉一套，互相抢 8188，
            # 用户看到的就是“一直闪”的 cmd 窗口（2026-11-18 实测）。
            if spec.id == "comfyui":
                existing = self._existing_comfy_pids()
                if existing:
                    with self._lock:
                        st = self._states[spec.id]
                        st.status = "starting"
                        st.detail = "已有 ComfyUI 进程启动中（PID %s）" % "、".join(
                            str(pid) for pid in existing[:2])
                        st.pid = existing[0]
                        st.started_at = time.time()
                    status, detail = self._wait_existing(spec, existing)
                    with self._lock:
                        self._states[spec.id].status = status
                        self._states[spec.id].detail = detail
                        self._states[spec.id].pid = (existing[0]
                                                     if status == "running" else None)
                    return

            if spec.ensure_qq:
                self._ensure_qq()
            proc, handles = self._spawn(spec)
            with self._lock:
                self._procs[spec.id] = proc
                self._logs[spec.id] = handles
                self._states[spec.id].pid = proc.pid
                self._states[spec.id].started_at = time.time()
                self._states[spec.id].status = "starting"
                self._states[spec.id].detail = "启动中"
                self._write_control_pid(spec, proc.pid)
            status, detail = self._wait_ready(spec, proc)
            with self._lock:
                self._states[spec.id].status = status
                self._states[spec.id].detail = detail
                self._states[spec.id].pid = proc.pid if proc.poll() is None else None
        except Exception as e:                              # noqa: BLE001
            with self._lock:
                st = self._states[spec.id]
                st.status = "error"
                st.detail = fail_label
                st.last_error = "%s: %s" % (type(e).__name__, e)
                st.pid = None
            log.warning("%s %s：%s", fail_label, spec.id, e)

    def _stop_worker(self, spec: ServiceSpec):
        try:
            self._stop_sync(spec)
            with self._lock:
                st = self._states[spec.id]
                st.status = "stopped"
                st.detail = "已停止"
                st.pid = None
        except Exception as e:                              # noqa: BLE001
            with self._lock:
                st = self._states[spec.id]
                st.status = "error"
                st.detail = "停止失败"
                st.last_error = "%s: %s" % (type(e).__name__, e)
            log.warning("停止 %s 失败：%s", spec.id, e)

    # ── 进程操作 ───────────────────────────────────────

    def _spawn(self, spec: ServiceSpec):
        log_path = self._log_dir / ("%s.log" % spec.id)
        out = open(log_path, "ab", buffering=0)
        env = os.environ.copy()
        env.update(spec.env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.Popen(
            list(spec.command),
            cwd=spec.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            close_fds=True,
        )
        return proc, (out,)

    def _stop_sync(self, spec: ServiceSpec):
        targets: list[int] = []
        control = self._read_control_pid(spec)
        if control:
            targets.append(control)
        if spec.port:
            port_pid = self._pid_on_port(spec.port)
            if port_pid:
                targets.append(port_pid)
        if spec.pid_file:
            pid = self._read_pid_file(spec.pid_file)
            if pid:
                targets.append(pid)
        # ComfyUI 可能同时存在“端口 owner”和“还在启动、没占端口”的重复实例。
        # 只杀端口 owner 会留下启动中的那一套，下次又抢 8188。这里把同
        # main.py 的进程全收进来，按进程树清干净。
        if spec.id == "comfyui":
            targets.extend(self._existing_comfy_pids())
        # 无端口、无 PID 文件的（如看门狗）只能按命令行找；否则停止会变成
        # 「没杀旧进程又起一个新的」，两个看门狗一起跑。
        pid = self._effective_pid(spec)
        if pid:
            targets.append(pid)

        targets = [pid for pid in dict.fromkeys(targets) if pid and pid != os.getpid()]
        for pid in targets:
            # SnowLuma 的 cmd 可能把 QQ 一起带成子进程；QQ 不能杀，杀了就要重新扫码。
            skip = ("qq.exe",) if spec.ensure_qq else ()
            self._kill_pid(pid, tree=True, skip_names=skip)
        proc = self._procs.pop(spec.id, None)
        handles = self._logs.pop(spec.id, ())
        for h in handles:
            try:
                h.close()
            except Exception:                                # noqa: BLE001
                pass
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except Exception:                                # noqa: BLE001
                pass
        self._remove_control_pid(spec)
        deadline = time.time() + spec.stop_timeout
        while time.time() < deadline:
            if spec.port and self._port_open(spec.port):
                time.sleep(0.4)
                continue
            if self._effective_pid(spec):
                time.sleep(0.4)
                continue
            break
        with self._lock:
            st = self._states[spec.id]
            st.pid = None
            st.started_at = None

    def _kill_pid(self, pid: int, tree: bool = True, skip_names: tuple[str, ...] = ()):
        if os.name == "nt":
            if tree and skip_names and psutil is not None:
                try:
                    for child in psutil.Process(pid).children(recursive=True):
                        if child.name().lower() not in skip_names:
                            self._taskkill(child.pid, tree=False)
                except Exception:                            # noqa: BLE001
                    pass
                # 已经手动清了非 QQ 子进程；父进程单独收，避免 taskkill /T 把 QQ 也带走。
                self._taskkill(pid, tree=False)
                return
            self._taskkill(pid, tree=tree)
        else:  # pragma: no cover - 项目跑 Windows
            try:
                os.kill(pid, 15)
            except OSError:
                pass

    @staticmethod
    def _taskkill(pid: int, tree: bool):
        cmd = [_sys_tool("taskkill.exe"), "/PID", str(pid), "/F"]
        if tree:
            cmd.insert(3, "/T")
        try:
            subprocess.run(
                cmd,
                capture_output=True, timeout=10,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:                                    # noqa: BLE001
            pass

    def wait_idle(self, ids: list[str], timeout: float = 120.0) -> bool:
        """等一批服务重启线程收尾；隐藏重启助手用它避免进程提前退出。

        返回 True=都收尾了，False=超时。只读线程状态，不触发新动作。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                busy = any(self._busy(sid) for sid in ids)
            if not busy:
                return True
            time.sleep(0.5)
        return False

    def _ensure_qq(self):
        if self._find_by_name("qq.exe"):
            return
        if not QQ_EXE.is_file():
            raise RuntimeError("找不到 QQ.exe：%s" % QQ_EXE)
        subprocess.Popen(
            [str(QQ_EXE)],
            cwd=str(QQ_EXE.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            close_fds=True,
        )
        # 给 QQ 客户端 12 秒起来；SnowLuma 自己也会等，这里只是别让它抢跑。
        time.sleep(12)

    # ── 状态刷新 ───────────────────────────────────────

    def _refresh_locked(self):
        for spec in self._order:
            st = self._states[spec.id]
            proc = self._procs.get(spec.id)
            if proc is not None and proc.poll() is not None:
                # 进程自己退了：保留退出码，别让卡片一直显示运行中。
                if st.status not in ("stopping", "restarting"):
                    st.status = "error" if proc.returncode else "stopped"
                    st.detail = "进程退出（%s）" % proc.returncode
                st.pid = None
            pid = st.pid if (st.pid and self._pid_alive(st.pid)) else None
            if pid is None:
                pid = self._effective_pid(spec)
            if pid:
                st.pid = pid

            if spec.port:
                port_up = self._port_open(spec.port)
                if port_up:
                    if spec.login_port and not self._port_open(spec.login_port):
                        st.status = "waiting"
                        st.detail = "等待 QQ 登录"
                    elif st.status not in ("restarting", "stopping", "starting"):
                        st.status = "running"
                        st.detail = self._detail(spec, pid)
                elif st.status not in ("starting", "restarting", "stopping"):
                    st.status = "stopped"
                    st.detail = "端口未监听"
            else:
                if pid:
                    if spec.login_port and not self._port_open(spec.login_port):
                        st.status = "waiting"
                        st.detail = "等待协议端登录"
                    elif st.status not in ("restarting", "stopping", "starting"):
                        st.status = "running"
                        st.detail = self._detail(spec, pid)
                elif st.status not in ("starting", "restarting", "stopping"):
                    st.status = "stopped"
                    st.detail = "未启动"

    def _serialize(self, spec: ServiceSpec) -> dict:
        st = self._states[spec.id]
        uptime = int(time.time() - st.started_at) if st.started_at else 0
        detail = st.detail
        if st.last_error and st.status == "error":
            detail = st.last_error
        return {
            "id": spec.id,
            "name": spec.name,
            "group": spec.group,
            "status": st.status,
            "color": status_color(st.status),
            "label": status_label(st.status),
            "detail": detail,
            "pid": st.pid,
            "port": spec.port,
            "restarts": st.restarts,
            "uptime": uptime,
        }

    def _detail(self, spec: ServiceSpec, pid: int | None) -> str:
        parts = []
        if pid:
            parts.append("PID %d" % pid)
        if spec.port:
            parts.append(str(spec.port))
        if spec.login_port and not self._port_open(spec.login_port):
            parts.append("未登录")
        return " · ".join(parts) if parts else "运行中"

    def _wait_existing(self, spec: ServiceSpec, pids: list[int]) -> tuple[str, str]:
        """等已经在启动的 ComfyUI 自己把端口开出来；不再重复拉进程。"""
        deadline = time.time() + spec.start_timeout
        while time.time() < deadline:
            if self._port_open(spec.port or 0):
                return "running", self._detail(spec, pids[0])
            if not any(self._pid_alive(pid) for pid in pids):
                return "error", "已有的 ComfyUI 进程已退出"
            time.sleep(0.5)
        return "error", "已有 ComfyUI 进程启动超时（端口 %s 未监听）" % spec.port

    @staticmethod
    def _existing_comfy_pids() -> list[int]:
        """按 comfy_launch 的完整 main.py 路径找进程，避免误伤其它 python。"""
        try:
            from app.features.image import comfy_launch
            return list(comfy_launch.process_pids() or [])
        except Exception:                                # noqa: BLE001
            return []

    def _wait_ready(self, spec: ServiceSpec, proc: subprocess.Popen) -> tuple[str, str]:
        deadline = time.time() + spec.start_timeout
        started = time.time()
        while time.time() < deadline:
            if proc.poll() is not None:
                return "error", "进程退出（%s）" % proc.returncode
            if spec.port:
                if self._port_open(spec.port):
                    if spec.login_port and not self._port_open(spec.login_port):
                        return "waiting", "等待 QQ 登录"
                    return "running", self._detail(spec, proc.pid)
            elif time.time() - started >= 1.5:
                # 无端口服务：给它 1.5 秒确认没立刻退出，就算起来了。
                return "running", self._detail(spec, proc.pid)
            time.sleep(0.5)
        if spec.port:
            return "error", "启动超时，端口 %s 未监听" % spec.port
        return "running", self._detail(spec, proc.pid)

    # ── 发现/文件 ──────────────────────────────────────

    def _adopt_existing(self):
        for spec in self._order:
            pid = self._read_control_pid(spec)
            if not pid:
                if spec.port:
                    pid = self._pid_on_port(spec.port)
                elif spec.pid_file:
                    pid = self._read_pid_file(spec.pid_file)
                elif spec.id in ("watchdog", "qqbot"):
                    pid = self._find_by_cmdline("app.workers.watchdog" if spec.id == "watchdog" else "app.qq_bot")
            if not pid or not self._pid_alive(pid):
                continue
            st = self._states[spec.id]
            st.pid = pid
            st.status = "running"
            st.detail = self._detail(spec, pid)
            try:
                st.started_at = psutil.Process(pid).create_time() if psutil else time.time()
            except Exception:                                # noqa: BLE001
                st.started_at = time.time()
            if spec.port and spec.login_port and not self._port_open(spec.login_port):
                st.status = "waiting"
                st.detail = "等待 QQ 登录"

    def _effective_pid(self, spec: ServiceSpec) -> int | None:
        for pid in (
            self._read_control_pid(spec),
            self._read_pid_file(spec.pid_file) if spec.pid_file else None,
            self._pid_on_port(spec.port) if spec.port else None,
            self._find_by_cmdline("app.workers.watchdog") if spec.id == "watchdog" else None,
            self._find_by_cmdline("app.qq_bot") if spec.id == "qqbot" else None,
        ):
            if pid and self._pid_alive(pid):
                return pid
        return None

    def _read_control_pid(self, spec: ServiceSpec) -> int | None:
        return self._read_int(self._control_dir / ("%s.pid" % spec.id))

    def _write_control_pid(self, spec: ServiceSpec, pid: int):
        path = self._control_dir / ("%s.pid" % spec.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(str(pid), encoding="utf-8")
        os.replace(tmp, path)

    def _remove_control_pid(self, spec: ServiceSpec):
        try:
            (self._control_dir / ("%s.pid" % spec.id)).unlink()
        except OSError:
            pass

    def _read_pid_file(self, rel: str | None) -> int | None:
        if not rel:
            return None
        return self._read_int(self.root / rel)

    @staticmethod
    def _read_int(path: Path) -> int | None:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return None
        for token in reversed(text.split()):
            if token.isdigit():
                return int(token)
        return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if psutil is not None:
            try:
                p = psutil.Process(pid)
                return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            except Exception:                                # noqa: BLE001
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _port_open(port: int, timeout: float = 0.4) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _pid_on_port(port: int) -> int | None:
        if psutil is not None:
            try:
                for conn in psutil.net_connections(kind="tcp"):
                    if (conn.laddr and conn.laddr.port == port
                            and conn.status == psutil.CONN_LISTEN and conn.pid):
                        return int(conn.pid)
            except Exception:                                # noqa: BLE001
                pass
        try:
            out = subprocess.run(
                [_sys_tool("netstat.exe"), "-ano"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            ).stdout
        except Exception:                                    # noqa: BLE001
            return None
        for line in out.splitlines():
            if (":%d " % port) in line and "LISTENING" in line.upper():
                last = line.split()[-1]
                if last.isdigit():
                    return int(last)
        return None

    @staticmethod
    def _find_by_name(name: str) -> int | None:
        if psutil is None:
            return None
        try:
            for p in psutil.process_iter(["pid", "name"]):
                if (p.info.get("name") or "").lower() == name.lower():
                    return int(p.info["pid"])
        except Exception:                                    # noqa: BLE001
            pass
        return None

    @staticmethod
    def _find_by_cmdline(fragment: str) -> int | None:
        if psutil is None:
            return None
        try:
            for p in psutil.process_iter(["pid", "cmdline"]):
                cmd = " ".join(p.info.get("cmdline") or [])
                if fragment in cmd:
                    return int(p.info["pid"])
        except Exception:                                    # noqa: BLE001
            pass
        return None

    def _need(self, sid: str) -> ServiceSpec:
        if sid not in self.specs:
            raise KeyError(sid)
        return self.specs[sid]

    def _thread(self, sid: str, fn):
        old = self._threads.get(sid)
        if old is not None and old.is_alive():
            return
        t = threading.Thread(target=self._run_thread, args=(sid, fn), daemon=True)
        self._threads[sid] = t
        t.start()

    def _busy(self, sid: str) -> bool:
        old = self._threads.get(sid)
        return old is not None and old.is_alive()

    def _run_thread(self, sid: str, fn):
        try:
            fn()
        finally:
            with self._lock:
                if self._threads.get(sid) is threading.current_thread():
                    self._threads.pop(sid, None)

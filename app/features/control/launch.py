# -*- coding: utf-8 -*-
"""启动/替换 5177 控制台，但不碰它托管的子服务。

双击「启动控制台.bat」时：
- 5177 没在跑：正常启动，并按默认 autostart 补起缺失服务；
- 5177 已在跑：先拉起新控制台，再只杀旧 5177 的 PID（不加 /T），
  隧道、QQ、ComfyUI 等子进程保持不动。

这样刷新控制台代码不需要“全部重启”。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from app.features.control.specs import ROOT

CONTROL_PORT = int(os.getenv("CONTROL_PORT", "5177"))
LOG_PATH = ROOT / "logs" / "control-launch.log"
_SYS32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"

log = logging.getLogger("control.launch")


def _taskkill(pid: int):
    """只杀控制台 PID，绝不加 /T，避免把隧道/QQ 一起带走。"""
    cmd = [str(_SYS32 / "taskkill.exe"), "/PID", str(pid), "/F"]
    subprocess.run(
        cmd,
        capture_output=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def panel_pid(port: int = CONTROL_PORT) -> int | None:
    """只认监听 5177 且命令行确实是 control.panel 的进程。"""
    try:
        import psutil
    except ImportError:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if (conn.laddr and conn.laddr.port == port
                    and conn.status == psutil.CONN_LISTEN and conn.pid):
                proc = psutil.Process(conn.pid)
                if "app.features.control.panel" in " ".join(proc.cmdline()):
                    return int(conn.pid)
    except Exception:                                    # noqa: BLE001
        return None
    return None


def spawn_panel(autostart: bool, open_browser: bool) -> subprocess.Popen:
    env = os.environ.copy()
    env["CONTROL_AUTOSTART"] = "1" if autostart else "0"
    env["CONTROL_NO_BROWSER"] = "0" if open_browser else "1"
    env["CONTROL_WAIT_FREE"] = "1"
    out_path = ROOT / "logs" / "control-panel-launch.out"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(out_path, "ab", buffering=0)
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "app.features.control.panel"],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    finally:
        out.close()


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(str(LOG_PATH), encoding="utf-8")],
    )
    try:
        old_pid = panel_pid()
        if old_pid:
            log.info("发现旧控制台 PID %s，只替换 5177，不碰子服务", old_pid)
            spawn_panel(autostart=False, open_browser=True)
            _taskkill(old_pid)
            return 0
        log.info("5177 未运行，正常启动")
        spawn_panel(autostart=True, open_browser=True)
        return 0
    except Exception:                                    # noqa: BLE001
        log.exception("启动/替换控制台失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

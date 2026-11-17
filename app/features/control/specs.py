# -*- coding: utf-8 -*-
"""服务控制台的服务清单与状态词。

只描述「有哪些服务、怎么启动、看哪个端口」；不在这里启动任何进程。
这样测试可以只读清单，不会碰到真实服务。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _console_python(path: str) -> str:
    """pythonw 跑控制台时，子服务仍用 python.exe，保证 stdout 能落日志。"""
    p = Path(path)
    if p.name.lower() == "pythonw.exe":
        candidate = p.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return path


PY = _console_python(os.getenv("AGENT_PY") or sys.executable)
COMSPEC = os.getenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

# ComfyUI 用自己的 torch 环境；不能拿 agent 的 venv，否则 import torch 就炸。
COMFY_PY = r"D:\AI\confyui_env\Scripts\python.exe"
if not Path(COMFY_PY).is_file():
    COMFY_PY = r"D:\AI\comfy_env\Scripts\python.exe"
if not Path(COMFY_PY).is_file():
    COMFY_PY = _console_python(sys.executable)

SNOWLUMA_DIR = Path(r"D:\AI\SnowLuma")


@dataclass(frozen=True)
class ServiceSpec:
    """一个可被控制台管理的服务。"""

    id: str
    name: str
    group: str
    command: tuple[str, ...]
    order: int
    cwd: str = str(ROOT)
    port: int | None = None
    login_port: int | None = None
    pid_file: str | None = None
    start_timeout: float = 20.0
    stop_timeout: float = 15.0
    ensure_qq: bool = False
    autostart: bool = True
    env: dict[str, str] = field(default_factory=dict)


def _env(**kwargs: str) -> dict[str, str]:
    return {k: v for k, v in kwargs.items() if v}


def _core_specs() -> list[ServiceSpec]:
    return [
        ServiceSpec(
            id="agent",
            name="Agent Web",
            group="核心",
            command=(PY, "agent.py"),
            order=1,
            port=5174,
            start_timeout=20,
        ),
        ServiceSpec(
            id="friend",
            name="群友页",
            group="核心",
            command=(PY, "friend_app.py"),
            order=2,
            port=5175,
            start_timeout=20,
        ),
        ServiceSpec(
            id="admin",
            name="管理页网关",
            group="核心",
            command=(PY, "admin_app.py"),
            order=3,
            port=5176,
            start_timeout=20,
        ),
    ]


def _tunnel_specs() -> list[ServiceSpec]:
    return [
        ServiceSpec(
            id="friend_tunnel",
            name="群友隧道",
            group="入口",
            command=(PY, "friend_tunnel.py"),
            order=4,
            cwd=str(ROOT),
            pid_file="state/friend_url.pid",
            start_timeout=25,
            env=_env(
                FRIEND_TUNNEL_TARGET="http://127.0.0.1:5175",
                FRIEND_TUNNEL_URL_FILE="friend_url.txt",
            ),
        ),
        ServiceSpec(
            id="admin_tunnel",
            name="管理页隧道",
            group="入口",
            command=(PY, "friend_tunnel.py"),
            order=5,
            cwd=str(ROOT),
            pid_file="state/admin_url.pid",
            start_timeout=25,
            env=_env(
                FRIEND_TUNNEL_TARGET="http://127.0.0.1:5176",
                FRIEND_TUNNEL_URL_FILE="admin_url.txt",
            ),
        ),
    ]


def _qq_specs() -> list[ServiceSpec]:
    return [
        ServiceSpec(
            id="snowluma",
            name="SnowLuma",
            group="QQ",
            command=(COMSPEC, "/c", "启动SnowLuma.bat", "auto"),
            order=6,
            cwd=str(ROOT),
            port=5099,
            login_port=3000,
            ensure_qq=True,
            start_timeout=35,
        ),
        ServiceSpec(
            id="qqbot",
            name="QQ 机器人",
            group="QQ",
            command=(PY, "-m", "app.qq_bot"),
            order=7,
            cwd=str(ROOT),
            login_port=3000,
            pid_file=".qq_bot.pid",
            start_timeout=20,
        ),
    ]


def _optional_specs() -> list[ServiceSpec]:
    return [
        ServiceSpec(
            id="watchdog",
            name="看门狗",
            group="核心",
            command=(PY, "-m", "app.workers.watchdog"),
            order=8,
            cwd=str(ROOT),
            start_timeout=12,
            autostart=False,  # 旧看门狗仍会拉起一键启动全部.bat；先手动开
        ),
        ServiceSpec(
            id="comfyui",
            name="ComfyUI",
            group="可选",
            command=(COMSPEC, "/c", "启动ComfyUI.bat", "auto"),
            order=9,
            cwd=str(ROOT),
            port=8188,
            start_timeout=120,
            stop_timeout=30,
            autostart=False,  # 吃显存，按需手动开
        ),
    ]


def default_specs() -> list[ServiceSpec]:
    """九张卡：对应旧启动器里会开出来的九个窗口。"""
    return _core_specs() + _tunnel_specs() + _qq_specs() + _optional_specs()


# 状态 → 颜色。只允许三色，界面不靠颜色单独表达，旁边始终有文字。
_COLOR = {
    "running": "green",
    "starting": "yellow",
    "restarting": "yellow",
    "waiting": "yellow",
    "stopping": "yellow",
    "stopped": "red",
    "error": "red",
}

_LABEL = {
    "running": "运行中",
    "starting": "启动中",
    "restarting": "重启中",
    "waiting": "等待登录",
    "stopping": "停止中",
    "stopped": "已停止",
    "error": "异常",
}


def status_color(state: str) -> str:
    return _COLOR.get(state, "red")


def status_label(state: str) -> str:
    return _LABEL.get(state, "异常")

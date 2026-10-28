"""ComfyUI 的启动命令（唯一事实源）。

## 为什么要有这一份

拉起 ComfyUI 的地方有两处，命令必须**完全一致**：

- `watchdog._trigger_comfy_restart()`：进程真的没了（崩溃 / 被系统杀）时拉起；
- `image_jobs._force_restart_comfy()`：端口还在、进程假死（能连上不回包），
  只能按端口强杀再拉起。

历史上这两处各写各的：看门狗用 `COMFYUI_PYTHON + main.py`，强杀路径用
`启动ComfyUI.bat`——而那个 bat 里写死 `D:\\AI\\ComfyUI_v037` 和
`--vram-headroom 1 --force-fp16`。2026-10-28 主实例换到 nightly 之后，这条
路径会**拉起一个错误的实例**：它既没有 `--disable-comfy-compiler`
（nightly 不加直接 `aimdo memory compile error` 起不来）和 `--enable-triton-backend`，
又会去抢 8188。真出事时「自动重启」变成「自动换成一个跑不动的旧版本」，比不重启更糟。

现在两边都从这里取命令，改一份就够。

## 参数从哪来

`config.COMFYUI_LAUNCH_ARGS`（可用 `.env` 覆盖）。端口以 `COMFYUI_URL`
为准——那里是提交端真正连的地址，命令里的 `--port` 必须跟它一致，否则
重启出来的实例在另一个端口上，探活永远等不到。

`PYTORCH_CUDA_ALLOC_CONF` 这类**环境变量**不能靠继承：机器人进程自己可能是被
计划任务 / 干净终端拉起来的，环境里没有这一项，而 12GB 卡缺了
`expandable_segments:True` 很容易碎片化 OOM。所以这里显式补一个默认值
（父进程已经有的话以父进程为准）。
"""

import logging
import os
import re
import subprocess

from app.config import (COMFYUI_LAUNCH_ARGS, COMFYUI_PYTHON,
                        COMFYUI_ROOT, COMFYUI_URL)

log = logging.getLogger("comfy_launch")

_PORT_IN_ARGS = re.compile(r"^--port$")


def _url_port(default=8188):
    """从 `COMFYUI_URL` 里取端口。"""
    m = re.search(r":(\d+)", COMFYUI_URL or "")
    if not m:
        return default
    try:
        return int(m.group(1))
    except ValueError:
        return default


def launch_args():
    """启动参数列表（端口已对齐 `COMFYUI_URL`）。"""
    args = (COMFYUI_LAUNCH_ARGS or "").split()
    port = _url_port()
    for i, tok in enumerate(args):
        if _PORT_IN_ARGS.match(tok) and i + 1 < len(args):
            args[i + 1] = str(port)
            break
    else:
        args += ["--port", str(port)]
    return args


def command():
    """完整的启动命令 `[python, main.py, *args]`；路径不全时返回 None。"""
    python = COMFYUI_PYTHON
    main_py = os.path.join(COMFYUI_ROOT or "", "main.py")
    if not python or not os.path.isfile(python):
        log.error("找不到 ComfyUI 的解释器：%s", python)
        return None
    if not os.path.isfile(main_py):
        log.error("找不到 ComfyUI 主程序：%s", main_py)
        return None
    return [python, main_py] + launch_args()


def cwd():
    """启动时的工作目录（ComfyUI 以自己目录为根找 models/ user/）。"""
    return COMFYUI_ROOT


def env():
    """子进程环境：补上 12GB 卡需要的显存分配策略（父进程已有则不覆盖）。"""
    env = dict(os.environ)
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    return env


def log_stream():
    """子进程的 stdout/stderr 落 `COMFYUI_ROOT/run_8188.log`；开不了就丢空。

    自动重启拉起来的实例没有控制台窗口（`CREATE_NO_WINDOW`），崩了（OOM /
    aimdo 报错 / 模型文件缺失）用户那边**完全看不到原因**。留一份日志，出事
    能直接翻——2026-10-28 第一次实测就撞上了「重启成功 17 秒后进程自己没了，
    因为日志被丢掉，连为什么都不知道」。
    """
    path = os.path.join(COMFYUI_ROOT or "", "run_8188.log")
    try:
        return open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:                   # noqa: BLE001  日志开不了不该挡住启动
        log.warning("打不开 ComfyUI 日志 %s，输出丢弃", path, exc_info=True)
        return subprocess.DEVNULL

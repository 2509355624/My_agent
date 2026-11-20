# -*- coding: utf-8 -*-
"""漫画后台任务的落盘小账本。

为什么需要它
------------
漫画的批次任务（一键生成底图 / 一键加气泡）是 5174 进程里的一个
`threading.Thread`。线程句柄只存在 `routes._JOBS` 这个内存字典里，5174 一
重启（或线程被系统杀掉）就彻底消失；而 `state.json` 里还写着 pending /
rendered，前端看起来“有人会来跑”，实际永远没人来。用户就是这样在
ComfyUI 重启窗口里点了长任务、半小时后回来发现图没了。

这个模块只做一件事：把「哪个任务、哪个阶段、跑到哪、是不是还活着」写到
任务目录的 `job.json`。它不负责重跑，也不改 state.json——恢复/报错由
`routes` 决定，避免两处状态机。

文件放在任务目录里，和 script.md / state.json 同级：
    comics/<任务>/job.json

约定
----
- status: running / done / error / aborted / interrupted
- phase:  scene / bubble / ...（与 routes._spawn 的 phase 一致）
- pid:    写账本时的进程号；5174 重启后 pid 变了，就能认出“不是当前线程”
- meta:   可序列化的续跑参数（kind / only / redo / split / scene_skill ...）
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime

_FILE = "job.json"
_LOCK = threading.Lock()


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _path(task_dir):
    return os.path.join(task_dir, _FILE)


def _write(task_dir, data):
    """原子替换：半截 JSON 比没有更糟，重启恢复时会被当成坏账本。"""
    dst = _path(task_dir)
    os.makedirs(task_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".job-", suffix=".tmp", dir=task_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dst)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def begin(task_dir, phase, meta=None):
    """线程起动前先落 running。失败只记日志，不能反过来把任务拦住。"""
    data = {
        "phase": phase,
        "status": "running",
        "pid": os.getpid(),
        "started": _now(),
        "updated": _now(),
        "message": "",
        "meta": dict(meta or {}),
    }
    try:
        with _LOCK:
            _write(task_dir, data)
    except Exception:
        # 账本是保险，不是业务前置条件；磁盘满/权限错不该让用户点不动按钮。
        pass
    return data


def update(task_dir, **fields):
    try:
        with _LOCK:
            data = read(task_dir) or {}
            if not data:
                return None
            data.update(fields)
            data["updated"] = _now()
            _write(task_dir, data)
            return data
    except Exception:
        return None


def finish(task_dir, status="done", message=""):
    return update(task_dir, status=status, message=str(message or ""),
                  finished=_now())


def fail(task_dir, message):
    return finish(task_dir, "error", message)


def abort(task_dir):
    return finish(task_dir, "aborted", "用户中断了整批渲染")


def read(task_dir):
    try:
        with open(_path(task_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mark_interrupted(task_dir, message="上次任务被服务重启中断，可以继续未完成的部分"):
    data = read(task_dir)
    if not data or data.get("status") != "running":
        return data
    return finish(task_dir, "interrupted", message)


def scan(root):
    """列出 root 下所有任务目录的账本；坏文件跳过。给启动恢复用。"""
    out = {}
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if name.startswith("."):
            continue
        task_dir = os.path.join(root, name)
        if not os.path.isdir(task_dir):
            continue
        data = read(task_dir)
        if data:
            out[task_dir] = data
    return out

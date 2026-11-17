#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""未投递完的生图任务账本（state/pending_jobs.json）。

为什么要有这份东西（2026-10-17 用户提出）：重启机器人时 **ComfyUI 是独立
进程、照常在跑**——已经提交进去的图一定会画完、落在 COMFYUI_OUTPUT_DIR 里。
但机器人一死就没人去 `/history/<prompt_id>` 收图了，图躺在磁盘上、用户永远
收不到，**而且连报错都没有**。这份账本把「已提交、未投递」的任务记下来，
重启后按 prompt_id 把图捞回来补发，一张都不白烧显卡。

三个事件（全在 app/image_jobs.py 里挂）：
  提交成功（拿到 prompt_id）  → `add()`      `process`
  任务终结（投递完/失败/超时）→ `remove()`   `_finish`（唯一收口，所有路径都走到）
  进程启动                    → `load()`     `resume_pending()` 补发

⚠️ 三类任务**故意不记**：
  - `quiet`（扮演态配图）：worker 只负责把图跑出来，投递权在调用方手上，
    上下文随进程一起消失，恢复出来也没人要；
  - NAI（云端）：同步 HTTP，重启后既没有任务可等，源图 base64 又有几 MB；
  - 没有 prompt_id 的（还躺在内存队列里排队）：ComfyUI 侧压根没这个任务，
    恢复只能重新提交，而重新提交必然和重启前已经排进去的那张撞车。

写入方式：整份重写 + 原子落盘（tmp + os.replace）。串行队列下同时存在的
任务最多几条，几十字节，代价可以忽略。**写失败只记日志**——账本是保险，
不能反过来把出图搞挂。
"""
import json
import logging
import os
import threading

from app.config import state_path

log = logging.getLogger("job_ledger")

# 测试进程恒落在临时目录（见 config.state_path），单个用例仍可显式 patch
# 这个常量指向自己的临时文件；生产恒为 state/pending_jobs.json。
PATH = state_path("pending_jobs.json")

_lock = threading.Lock()


def _read_disk_locked():
    """读文件；坏掉/不存在一律当空账。调用方必须已持 `_lock`。"""
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)}


def _write_disk_locked(jobs):
    """原子落盘。失败只记日志、不抛。调用方必须已持 `_lock`。"""
    try:
        d = os.path.dirname(PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"jobs": jobs}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PATH)
        return True
    except OSError:
        log.warning("任务账本写盘失败（不影响本次出图）", exc_info=True)
        return False


def add(rec):
    """记一条「已提交、还没投递完」的任务。

    返回是否落盘成功。rec 必须带 `tag`（图片编号，同时是主键）和 `prompt_id`
    —— 缺任何一个就没有恢复意义（既认不出是哪张图，也捞不回来）。
    """
    rec = dict(rec or {})
    tag = str(rec.get("tag") or "")
    if not tag or not rec.get("prompt_id"):
        log.warning("账本记录缺 tag / prompt_id，跳过：%r", rec)
        return False
    with _lock:
        jobs = _read_disk_locked()
        jobs[tag] = rec
        return _write_disk_locked(jobs)


def remove(tag):
    """任务终结后划掉那一行。幂等：本来就没有也算成功。"""
    tag = str(tag or "")
    if not tag:
        return False
    with _lock:
        jobs = _read_disk_locked()
        if tag not in jobs:
            return True
        jobs.pop(tag, None)
        return _write_disk_locked(jobs)


def load():
    """读出全部残留任务（`{tag: rec}` 的副本）。

    坏文件/不存在一律当空——账本坏了不该拦住机器人启动。
    """
    with _lock:
        return dict(_read_disk_locked())


def clear():
    """清空账本（恢复流程跑完 / 手工排障用）。"""
    with _lock:
        return _write_disk_locked({})

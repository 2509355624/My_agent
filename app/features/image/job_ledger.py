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

⚠️ 两类任务**故意不记**：
  - `quiet`（扮演态配图）：worker 只负责把图跑出来，投递权在调用方手上，
    上下文随进程一起消失，恢复出来也没人要；
  - NAI（云端）：同步 HTTP，重启后既没有任务可等，源图 base64 又有几 MB。

## 排队中的那几张（2026-11-26 补，用户报「重启完就没了，用户还在傻等」）

从前「还躺在内存队列里排队」的压根不记账，理由是恢复只能重新提交、必然和
重启前已经排进去的那张撞车。这个理由依然成立——**所以它们到现在也救不回来**。

问题是它们**消失得无声无息**：回执早就说过「正在画」，用户等半小时还在等。
现在改成入队就记一条 `state="queued"` 的轻记录（只有会话 + 渠道 + 提示词，
**不存 workflow、不存源图**），`_finish` 划掉。重启后：

  - 留下它的 pid 还活着 → 不动（见 `drop_queued`）
  - pid 已经没了 → 这几张确实丢了，发一句话告诉用户，然后清掉

一句话概括：**救不回来，但要让人知道自己丢了什么。**

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


# 记录状态（2026-11-26 加 `queued`，用户报「重启后还在排队的那几张无声无息
# 就没了，用户一直等」）：
#   submitted —— 已经交给 ComfyUI，有 prompt_id，重启后能去 /history 取图补发
#   queued    —— 只在内存队列里排着，ComfyUI 那边还没有它。**救不回来**，记它
#                唯一的目的就是重启后能告诉用户「这几张没画成」，而不是让他
#                对着一条「正在画」的回执等半个小时。
# 老记录没有 `state` 字段，一律按 `submitted` 读——升级前写下的那些本来就是
# 已经提交过的。
STATE_SUBMITTED = "submitted"
STATE_QUEUED = "queued"


def _state_of(rec):
    return str((rec or {}).get("state") or STATE_SUBMITTED)


def add(rec):
    """记一条任务（已提交的 / 还在排队的都能记）。

    返回是否落盘成功。rec 必须带 `tag`（图片编号，同时是主键）；
    `prompt_id` 只有 `state="queued"` 时可以缺——其余情况缺了就没有恢复意义
    （既认不出是哪张图，也捞不回来）。
    """
    rec = dict(rec or {})
    tag = str(rec.get("tag") or "")
    if not tag:
        log.warning("账本记录缺 tag，跳过：%r", rec)
        return False
    if _state_of(rec) != STATE_QUEUED and not rec.get("prompt_id"):
        log.warning("账本记录缺 prompt_id，跳过：%r", rec)
        return False
    rec.setdefault("state", STATE_SUBMITTED)
    with _lock:
        jobs = _read_disk_locked()
        jobs[tag] = rec
        return _write_disk_locked(jobs)


def drop_queued(keep):
    """清掉重启后确认属于**死进程**的排队记录，返回留下的那些。

    进程被强杀时 `_finish` 走不到，账本里会留下一堆 `queued`。新进程启动时
    自己手里的队列是空的，所以第一件事就是问「这些是谁留下的」：

    - 留下它的那个 pid 还活着（5174 起来了、QQ bot 还没重启）→ **不许动**，
      它自己跑完会划账；动了就是把人家的任务抹了。
    - 那个 pid 已经不在了 → 那几张确实没了，交给调用方去告诉用户。

    `keep` 是调用方判断「这个 pid 还活着」的函数。读不到账本/写不进去一律
    返回空 dict（当没有残留），不能拦住启动。
    """
    try:
        jobs = load()
    except Exception:
        log.warning("排队残留：读账本失败，跳过", exc_info=True)
        return {}
    live, dead = {}, {}
    for tag, rec in jobs.items():
        if _state_of(rec) != STATE_QUEUED:
            continue
        try:
            alive = bool(keep(rec.get("pid")))
        except Exception:
            alive = True                # 判不了就当它还活着，宁可不报也别乱报
        (live if alive else dead)[tag] = rec
    if dead:
        try:
            with _lock:
                raw = _read_disk_locked()
                for tag in dead:
                    raw.pop(tag, None)
                _write_disk_locked(raw)
        except Exception:
            log.warning("排队残留：清理失败（不影响出图）", exc_info=True)
    return dead


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

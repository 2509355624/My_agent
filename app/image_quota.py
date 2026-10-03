# -*- coding: utf-8 -*-
"""私聊每日生图额度：按人计数、跨天自动归零、失败退还。

## 为什么单独一个模块

这是**账本**——要落盘、要在重启后活下来，而且被两个线程同时碰（agent 线程在
接单时扣，worker 线程在失败时退）。`agents.py` 那边只管读 settings.json 决定
「额度是多少、谁免额」，具体记账放这儿，跟 `qq_names` / `recent` 一个路数。

## 存储形态

`state/image_quota.json`（`state/` 在 .gitignore 里，不进仓库）：

    {"date": "2026-09-30", "used": {"3985441738": 3}}

**日期直接存在文件里**，不靠文件 mtime、不靠定时任务：读的时候发现 date 不是
今天就当 0，写的时候顺手把 date 换掉。跨天归零于是**没有任何后台依赖**——
机器人半夜重启、机器休眠一整天，醒来第一次读就自动归零了。

## 只算私聊

群聊不走这里（用户 2026-09-30 明确只要私聊限流）。判定放在调用方
（`agents.image_quota_allowed` / `generate_image`），本模块不关心 target。

## 绝不抛错

它在生图热路径上，也在 worker 的 finally 里。磁盘满、JSON 被手改坏，都只能
让计数退回「进程内内存值」，**不能让出图挂掉**。
"""

import json
import logging
import os
import threading
import time

from app.config import state_path

log = logging.getLogger("image_quota")

# 测试进程恒落在临时目录（见 config.state_path）——额度是**真实用户看得见的
# 账**，拿夹具里的 "42" 往真实 state/ 里扣，会让别人今天的额度凭空少几张
# （2026-10-04 实测：真实账本里攒了 "42": 25，全是跑测试扣的）。
# 单个用例仍可显式 patch 这个常量指向自己的临时文件，显式优先。
# 生产恒为 state/image_quota.json。
PATH = state_path("image_quota.json")

# 内存镜像：磁盘写失败时的兜底，也是「本进程刚刚扣过多少」的唯一事实。
# 结构同文件 {"date": ..., "used": {...}}。
_state = {"date": "", "used": {}}
# 上次写盘失败了 —— 此时内存比磁盘新，_sync_locked 不许拿磁盘覆盖它。
_unsynced = False
_lock = threading.Lock()


def _today():
    """本地日期（Asia/Shanghai，跟机器人日志同一个时区）。

    刻意不用 UTC：额度是给「用户感觉的一天」用的。
    账本因此**在本地 00:00 翻页归零**——不是「20:00 重置」那种更讲究的排法
    （那样得另存时刻，这里没做）。给模型的那句话照这个事实写
    （见 agents.image_quota_line 的「明天 00:00」），别改口。
    """
    return time.strftime("%Y-%m-%d", time.localtime())


def _read_disk():
    """读文件；坏掉/不存在一律当空账。调用方必须已持 _lock。"""
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    used = data.get("used")
    if not isinstance(used, dict):
        used = {}
    # 键统一成字符串（手改 settings 时可能写成数字）
    return {"date": str(data.get("date") or ""),
            "used": {str(k): int(v) for k, v in used.items()
                     if isinstance(v, (int, float))}}


def _write_disk():
    """原子落盘（tmp + os.replace）。失败只记日志，不抛。调用方必须已持 _lock。"""
    global _unsynced
    try:
        d = os.path.dirname(PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, PATH)
        _unsynced = False
        return True
    except OSError:
        # 写不进去就认账：从此内存说了算，免得 _sync_locked 拿旧磁盘把刚扣的
        # 那张抹掉（那等于额度凭空多出来）。
        _unsynced = True
        log.warning("配额账本写盘失败（内存计数仍然生效）", exc_info=True)
        return False


def _sync_locked():
    """把 _state 对齐到「今天」。调用方必须已持 _lock。

    **优先信磁盘**：管理页（Flask 进程）只读不写，机器人（另一个进程）负责写，
    两边各有一份内存镜像。要是这边拿内存当权威，管理页就会一整天显示 0——
    因为它自己的内存从来没被谁扣过。所以每次读写前先拉一次磁盘。
    （上次写盘失败时例外，那时磁盘反而是旧的，见 _unsynced。）

    然后只要日期不是今天就整个清空。**跨天归零就靠这一步**，不需要任何定时器。
    """
    global _unsynced
    if not _unsynced:
        disk = _read_disk()
        if disk is not None:
            _state.update(disk)
    today = _today()
    if _state["date"] != today:
        _state["date"] = today
        _state["used"] = {}


def used(target_id):
    """某人今天已经用掉几张。"""
    key = str(target_id or "")
    if not key:
        return 0
    with _lock:
        _sync_locked()
        return int(_state["used"].get(key, 0))


def snapshot():
    """今天全体的用量 {QQ号: 张数}，管理页显示用。"""
    with _lock:
        _sync_locked()
        return dict(_state["used"])


def charge(target_id):
    """扣一张，返回扣完之后的今日总数。

    扣额是**接单时**发生的（用户 2026-09-30 选的方案）：工具一被调用就占名额，
    这样才拦得住「连点刷队列」。跑失败由 worker 调 refund 退回来。
    """
    key = str(target_id or "")
    if not key:
        return 0
    with _lock:
        _sync_locked()
        n = int(_state["used"].get(key, 0)) + 1
        _state["used"][key] = n
        _write_disk()
        return n


def refund(target_id):
    """退一张（失败退还），返回退完之后的今日总数。

    下限 0：worker 重试、或者 refund 被调两次（比如 process 抛异常又被
    _finish 兜一次），都不能把账做成负数——负数的账会让额度凭空变多。
    """
    key = str(target_id or "")
    if not key:
        return 0
    with _lock:
        _sync_locked()
        n = int(_state["used"].get(key, 0)) - 1
        if n <= 0:
            _state["used"].pop(key, None)
            n = 0
        else:
            _state["used"][key] = n
        _write_disk()
        return n


def reset():
    """清空内存镜像（测试用；不动磁盘）。"""
    global _unsynced
    with _lock:
        _state["date"] = ""
        _state["used"] = {}
        _unsynced = False

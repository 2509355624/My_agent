"""QQ bot 实时状态快照：落盘给状态后台（Flask）读。

## 为什么写成文件，而不是开个接口
qq_bot.py 是**独立进程**，跟 Flask（app/main.py）不共享内存。SessionRunner
的排队现场（谁卡在 `async with self.bot.sem` 那一行）和 image_jobs 的队列
都在它自己的内存里，Flask 那个进程根本读不到。

所以让 qq_bot 自己把状态写成 JSON，Flask 去读文件。

顺带一个便宜的好处：**文件本身就是心跳**。mtime 停更 = bot 进程卡死或没了，
后台直接标红「bot 无心跳」——这正是「任务卡死时看不到后台状态」最需要的那
个信号，不用另做一套探活。

## 要点
- 原子写（临时文件 + os.replace）：读的一方永远看不到半截 JSON。
- 只写不读；读在 Flask 侧（read()）。
- NapCat 探活在这里做一次就够，别让 Flask 再去戳 3000 口。
- 时长一律用 monotonic 算（SessionRunner.state_since 也是 monotonic），
  千万别拿 time.time() 去减——两个时钟基准不同，减出来是垃圾。
"""

import json
import logging
import os
import threading
import time

from app.config import BASE_DIR

log = logging.getLogger("qq_status")

STATE_DIR = os.path.join(BASE_DIR, "state")
STATE_PATH = os.path.join(STATE_DIR, "qq_status.json")

# 写盘间隔。状态后台 2 秒刷一次，这里 1 秒写一次足够跟得上；再密没意义
# （人眼看不出），再疏会让人看到「假卡死」。
WRITE_INTERVAL = 1.0

# 读的一方认为「超过这么久没更新 = bot 死了」。
STALE_SECONDS = 10.0

# 探活 NapCat 的超时：状态后台每 2 秒要拿到结果，不能让它卡住。
_ALIVE_TIMEOUT = 3

# 阶段 -> 中文。后台直接显示，不用前端再翻一次。
STATE_LABEL = {
    "idle": "空闲",
    "debouncing": "攒消息中",
    "waiting_slot": "排队等并发槽",
    "running": "跑模型中",
}

# 排序权重：等并发槽的排最前（那才是「以为卡了」的人），空闲的沉底。
_ORDER = {"waiting_slot": 0, "running": 1, "debouncing": 2, "idle": 3}


def _alive():
    """探 NapCat 协议端。返回 (online, detail)。"""
    try:
        from app import qq_api
        uid, nick = qq_api.check_alive(timeout=_ALIVE_TIMEOUT)
        if uid:
            return True, (nick or str(uid))
        return False, "探活返回空 uid"
    except Exception as exc:
        return False, repr(exc)[:120]


def snapshot(bot=None):
    """收集一份完整状态。bot 为 None 时只出「进程级」字段（测试用）。"""
    now = time.time()          # 墙钟：给「距今多久」用
    mono = time.monotonic()    # 单调钟：给 SessionRunner 的时长用

    # ── 会话轮次（谁在排队）────────────────────────
    sessions = []
    runners = getattr(bot, "runners", None) or {}
    for key, r in runners.items():
        st = getattr(r, "state", "idle") or "idle"
        since = getattr(r, "state_since", mono)
        sessions.append({
            "key": key,
            "target": getattr(r, "target", "") or "",
            "target_id": getattr(r, "target_id", "") or "",
            "state": st,
            "state_label": STATE_LABEL.get(st, st),
            # 单调钟相减才是对的（state_since 来自 time.monotonic）
            "for": round(mono - since, 1),
            "pending": len(getattr(r, "_pending", None) or []),
            "preview": getattr(r, "turn_preview", "") or "",
            "senders": [s for s in (getattr(r, "batch_senders", None) or []) if s],
        })
    sessions.sort(key=lambda s: (_ORDER.get(s["state"], 9), -s["for"]))

    # ── 生图队列 ───────────────────────────────────
    try:
        from app import image_jobs
        jobs = image_jobs.snapshot()
    except Exception as exc:
        log.warning("读生图队列失败：%s", exc)
        jobs = {"running": None, "queued": [], "depth": 0}

    # ── NapCat ────────────────────────────────────
    online, detail = _alive()

    # ── 最后一次收发（墙钟）────────────────────────
    try:
        from app import notify
        idle_for = round(now - notify.last_activity(), 1)
    except Exception:
        idle_for = None

    # ── 群名/私聊昵称（顺手把刷新驱动一下）────────
    # refresh_lists 内部限速 10 分钟一次，状态后台每 2 秒调它不会刷爆 NapCat。
    try:
        from app import qq_names
        qq_names.refresh_lists()
        # 给会话/任务打名字（找不到返回 None → 前端回退到 ID）
        for s in sessions:
            s["name"] = qq_names.name_for(s.get("target"), s.get("target_id")) or ""
        for j in [jobs.get("running")] + (jobs.get("queued") or []):
            if j:
                j["name"] = qq_names.name_for(
                    j.get("target"), j.get("target_id")) or ""
    except Exception as exc:
        log.warning("名字解析失败：%s", exc)

    return {
        "ts": now,
        "bot_alive": True,
        "sessions": sessions,
        "jobs": jobs,
        "napcat": {"online": online, "detail": detail},
        "last_activity_ago": idle_for,
    }


# os.replace 在 Windows 上会被「读者正好开着文件」挡回来。Flask 每 2 秒
# open() 读一次快照（app/main.py 的 /api/status 走 qq_status.read），而 Python
# 的 open 不带 FILE_SHARE_DELETE——撞上那个微秒窗口，MoveFileEx 就返回
# 「拒绝访问」（2026-09-29 实测两天撞了 84 次，全是 WinError 5）。
# 对方的句柄只活到 json.load 读完，所以退让几毫秒重试就够，不必改写入策略
# （改成非原子写反而会让读者看到半截 JSON，那个代价更大）。
_REPLACE_ATTEMPTS = 4
_REPLACE_DELAY = 0.01


def _replace_with_retry(tmp, path, attempts=_REPLACE_ATTEMPTS,
                        delay=_REPLACE_DELAY):
    """重试几次 os.replace；仍然失败就把最后一次的异常抛给调用方去记日志。"""
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def write_snapshot(bot=None, path=STATE_PATH):
    """原子写一份快照。失败只记日志——状态后台不是主业，不能拖垮 bot。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    try:
        data = snapshot(bot)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        _replace_with_retry(tmp, path)
        return True
    except Exception as exc:
        log.warning("写状态快照失败：%s", exc)
        return False


def _write_loop(bot, path, interval):
    while True:
        try:
            # 顺便把名字缓存刷一下——refresh_lists 内部限速 10 分钟一次，
            # 起动时 last_refresh=0 必刷，之后每次写盘顺带检查但不真拉。
            # 这样起动后第一秒就能看到名字，也不会把 NapCat 打爆。
            try:
                from app import qq_names
                qq_names.refresh_lists()
            except Exception:
                pass
            write_snapshot(bot, path)
        except Exception as exc:     # 守护线程不能因为一次异常就静默死掉
            log.warning("状态快照循环异常：%s", exc)
        time.sleep(interval)


def start(bot, path=STATE_PATH, interval=WRITE_INTERVAL):
    """起守护线程持续写快照。重复调用只起一次。"""
    if getattr(start, "_started", False):
        return
    start._started = True
    threading.Thread(target=_write_loop, args=(bot, path, interval),
                     daemon=True, name="qq-status").start()
    log.info("状态快照已启用：%s（每 %.1f 秒）", path, interval)


def read(path=STATE_PATH, stale=STALE_SECONDS):
    """Flask 侧读。返回 (data, is_stale)。

    is_stale=True 表示文件太久没更新 —— bot 进程卡死或压根没起。这时 data
    里的内容是**过期的**，只用来显示「最后看到的样子」，不能当实时状态信。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, True
    return data, (time.time() - mtime > stale)

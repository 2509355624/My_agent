"""扮演态状态文件的持久化：原子写、备份、跨进程合并。

这里只管「按给定路径读写一份 dict」，不认识会话键、角色卡和迁移规则。
`service._load_state` / `_save_state` 负责把路径和迁移接进来。
"""

import copy
import functools
import glob
import json
import logging
import os
import shutil
import threading
import time

log = logging.getLogger("persona.state")

# 同一进程内的读写互斥（可重入）：主线程落盘和小记压缩线程不能交叉。
STATE_LOCK = threading.RLock()


def state_guard(fn):
    """把状态文件的读写串行化（`load` 里会再调 `save`）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with STATE_LOCK:
            return fn(*args, **kwargs)
    return wrapper


@state_guard
def read_raw(path):
    """直接读盘，不迁移、不落盘。返回 None 表示「文件在、但读不了」。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else {}


@state_guard
def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        # 文件真坏了：挪去旁边留尸，再从空库起，绝不静默覆盖。
        bad = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        log.exception("角色扮演状态文件坏了，已挪到 %s", bad)
        try:
            os.replace(path, bad)
        except OSError:
            log.exception("坏状态文件挪不动，继续用空库（写盘会再次被拒）")
        return {}
    except OSError:
        # 读到一半权限/占用失败时不能拿空库覆盖，宁可这一轮报错。
        log.exception("读角色扮演状态失败（拒绝用空库覆盖）：%s", path)
        raise
    return d if isinstance(d, dict) else {}


@state_guard
def save(path, state, deleted=None, read_raw_fn=None):
    """整份落盘，但先把盘上别的线合并回来。

    `read_raw_fn` 仅供兼容门面传入（测试会 patch `service._read_state_raw`）；
    不传就直接读 `path`。

    合并只补「盘上有、这次没带」的键；真要删线必须显式传 `deleted`，
    不然删档会被合并动作复活。同一把键两边都有时，游戏日只许前进。
    """
    cur = read_raw_fn() if read_raw_fn is not None else read_raw(path)
    if cur is None:
        if os.path.exists(path):
            log.error("状态文件读不出来，拒绝整份覆盖：%s", path)
            raise RuntimeError("persona state unreadable; refuse to overwrite")
        cur = {}
    drop = {str(k) for k in (deleted or ())}
    for k, v in cur.items():
        if k in drop:
            continue
        if k not in state:
            state[k] = v
            continue
        mine, disk = state.get(k), v
        if not isinstance(mine, dict) or not isinstance(disk, dict):
            continue
        if mine.pop("_day_back_ok", False) or disk.pop("_day_back_ok", False):
            continue
        try:
            md = int((mine.get("gameday") or {}).get("day") or 0)
            dd = int((disk.get("gameday") or {}).get("day") or 0)
        except (TypeError, ValueError):
            md = dd = 0
        if dd > md:
            mine["gameday"] = copy.deepcopy(disk.get("gameday") or {})
            mine.pop("gameday_prev", None)
        try:
            mt = int(mine.get("turn_total") or -1)
            dt = int(disk.get("turn_total") or -1)
        except (TypeError, ValueError):
            mt = dt = -1
        if dt > mt:
            mine["turn_total"] = dt

    from app.core.agents import _atomic_write_text
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 写前备份：上一代 + 每小时一份，留 12 份。失败不影响主流程。
    try:
        if os.path.exists(path):
            shutil.copy2(path, path + ".prev")
            hourly = path + ".hourly-" + time.strftime("%Y%m%d-%H")
            if not os.path.exists(hourly):
                shutil.copy2(path, hourly)
                olds = sorted(glob.glob(path + ".hourly-*"))
                for old in olds[:-12]:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
    except (OSError, NameError):
        log.warning("写前备份失败（不影响落盘）：%s", path)
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))

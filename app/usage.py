"""每日 token 用量统计：按「天 + 会话」聚合，落盘 JSON。

为什么需要它：MiMo 后台只有总量，对不上「哪个群烧的钱」。这里在 llm 层
（_record_usage，流式/非流式共用口径）挂一个采集点，把每次调用的
命中/未命中/输出 token 归到当前**归属**名下——归属就是会话 key
（group_xxx / private_xxx），隐形调用（识图/接话判断/压缩摘要/长期记忆）
各有自己的类别名，这样账才能和 LLM 后台对上。

归属通过线程本地变量传递：qq_bot 在轮次外层 scope(session_key)，内层的
识图、摘要等调用会被各自的 scope 覆盖成更具体的类别。落盘节流（默认
10 秒），进程重启最多丢最后一笔的落盘延迟，内存数据不丢的代价不值得付。

文件放 BASE_DIR/usage/（gitignore 内），一天一个 JSON：

    {
      "date": "2026-09-27",
      "sessions": {
        "group_1041079621": {"calls": 12, "hit": 34000, "miss": 4000,
                             "output": 1500},
        "vision": {...}
      }
    }
"""

import json
import logging
import os
import threading
import time

from app.config import BASE_DIR

log = logging.getLogger("usage")

# 落盘节流：两次写盘的最小间隔（秒）。统计场景不在乎这 10 秒的窗口。
_FLUSH_INTERVAL = 10.0

_local = threading.local()
_lock = threading.Lock()

# {date: {tag: {"calls": n, "hit": n, "miss": n, "output": n}}}
_daily = {}
_dirty = set()          # 有未落盘改动的日期
_last_flush = 0.0


def _today():
    return time.strftime("%Y-%m-%d")


def _usage_dir():
    return os.path.join(BASE_DIR, "usage")


def _usage_path(date):
    return os.path.join(_usage_dir(), "%s.json" % date)


def current_tag():
    """当前线程的归属名；没设过就是 other（不该出现，兜底可见）。"""
    return getattr(_local, "tag", None) or "other"


def set_tail(n):
    """记下本轮「尾巴」的估算 token 数（状态栏 + extra_context）。

    尾巴挂在消息数组末尾、每轮都变，所以每轮必 miss——它的大小直接决定
    命中率天花板（≈ 1 − 尾巴/prompt）。只用于日志展示，不参与计费聚合。
    """
    _local.tail = int(n or 0)


def current_tail():
    """当前线程记下的尾巴 token 数；没设过是 0。"""
    return int(getattr(_local, "tail", 0) or 0)


class scope:
    """上下文管理器：把当前线程的用量归属设为 tag（嵌套时内层生效）。"""

    def __init__(self, tag):
        self.tag = tag
        self._prev = None

    def __enter__(self):
        self._prev = getattr(_local, "tag", None)
        _local.tag = self.tag
        return self

    def __exit__(self, *exc):
        _local.tag = self._prev
        return False


def record(hit, miss, output=0, provider="", model=""):
    """记一笔。provider/model 只是留档方便对账，聚合口径里不参与分组。"""
    global _last_flush
    date = _today()
    with _lock:
        day = _daily.setdefault(date, {})
        slot = day.setdefault(current_tag(), {})
        slot["calls"] = slot.get("calls", 0) + 1
        slot["hit"] = slot.get("hit", 0) + int(hit or 0)
        slot["miss"] = slot.get("miss", 0) + int(miss or 0)
        slot["output"] = slot.get("output", 0) + int(output or 0)
        _dirty.add(date)
        due = time.time() - _last_flush >= _FLUSH_INTERVAL
    if due:
        flush()


def flush():
    """把内存里攒的用量写盘（原子写：临时文件 + replace）。"""
    global _last_flush
    with _lock:
        dates = list(_dirty)
        _dirty.clear()
        _last_flush = time.time()
        os.makedirs(_usage_dir(), exist_ok=True)
        for date in dates:
            data = _daily.get(date) or {}
            path = _usage_path(date)
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"date": date, "sessions": data},
                              f, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
            except OSError as e:
                log.warning("用量落盘失败 %s：%s", path, e)
                _dirty.add(date)             # 失败的下轮再试


def daily(date=None):
    """读一天的聚合结果（内存优先，落盘文件兜底——供跨进程/重启后查看）。"""
    date = date or _today()
    with _lock:
        data = _daily.get(date)
        if data is not None:
            return {"date": date, "sessions": data}
    try:
        with open(_usage_path(date), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"date": date, "sessions": {}}


def load_all_dates():
    """已有落盘的日期列表（管理页做日期选择用）。"""
    try:
        return sorted(f[:-5] for f in os.listdir(_usage_dir())
                      if f.endswith(".json"))
    except OSError:
        return []


def _load_from_disk_into_memory():
    """进程启动时把「今天可能已有的半份账」并回内存，避免同日重启把账劈两半。"""
    date = _today()
    saved = daily(date).get("sessions") or {}
    if not saved:
        return
    with _lock:
        day = _daily.setdefault(date, {})
        for tag, slot in saved.items():
            cur = day.setdefault(tag, {})
            for k in ("calls", "hit", "miss", "output"):
                cur[k] = cur.get(k, 0) + int(slot.get(k, 0) or 0)


_load_from_disk_into_memory()

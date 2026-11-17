"""扮演态两条历史线的读写：滑动窗口 + 只增归档。

这里只接收已经算好的文件路径，不解析会话键；路径规则留在 service。
"""

import json
import logging
import os

log = logging.getLogger("persona.history")


def archive_path(hist_path, session_key):
    """由窗口文件路径推出归档路径；窗口不合法（None）时返回 None。"""
    if not hist_path:
        return None
    d, _n = os.path.split(hist_path)
    return os.path.join(d, "persona_archive_" + session_key + ".jsonl")


def append(path, msgs, seed=None):
    """往归档里追几条；`seed` 非空且文件不存在时先整份铺底。"""
    if not path or (not msgs and not seed):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fresh = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            rows = seed if (fresh and seed) else msgs
            for m in rows:
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("归档写不进去：%s", path)


def _parse_lines(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue            # 单行坏了不该让整段历史读不出来
    except OSError:
        log.exception("历史读不出来：%s", path)
    return rows


def read_rows(path):
    """只读归档文件本身；缺失 / 读不了返回 []。"""
    if not (path and os.path.exists(path)):
        return []
    return [m for m in _parse_lines(path)
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]


def read_window(path):
    """读滑动窗口原文；缺失 / 读不了返回 []。"""
    if not (path and os.path.exists(path)):
        return []
    return _parse_lines(path)


def write_window(path, msgs):
    """整份重写滑动窗口（原子写）。"""
    if not path:
        return
    from app.core.agents import _atomic_write_text
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(path,
                       "".join(json.dumps(m, ensure_ascii=False) + "\n"
                               for m in msgs))

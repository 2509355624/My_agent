"""moss 小漫画 · 回执账本（`message_id → 哪一系列第几页`）。

## 为什么要它

2026-10-31 用户把指令砍成四个词（`moss漫画` / `moss续集` / `moss重画` /
`moss合集`，**必须带 moss 前缀**），前提是**必须引用那一页的回执**。
引用段里带回来的 `reply.id` 就是发图时协议端给的 `message_id`——这里把它
记下来，引用就等于精确定位到「哪一系列第几页」，用户一个字都不用打。

照抄 `card_bot` 的做法（`card_msgs.json` 那套）：造卡引用重画踩过的坑
一模一样，没必要换一种写法。

## 一条兜底：转发之后 id 就变了

`message_id` 只对**原消息**有效。用户把图转发到别的群、或者截图再发，
引用回来的 `id` 是新的，账本里查不到——而「把回执分享给朋友一起玩」
恰恰是用户要的场景。

所以回执卡上印了看得见的编号 `S7K3-P05`，认不出 id 时就从
**被引消息的正文**里正则抠它（`moss_series.parse_code`）。引用段带的是
被引那条消息的全文，抠得出来。

两道保险的先后：**先查 id（准），再抠编号（兜底），都不行就拒绝**。
"""

import json
import logging
import os
import threading
import time

from . import moss_series as series_mod

log = logging.getLogger("moss_msgs")

LEDGER_FILE = os.path.join(series_mod.STATE_ROOT, "_msgs.json")
KEEP = 800

# 群聊里两个系列同时出图会同时回写，不加锁就是互相覆盖。
_LOCK = threading.RLock()


def _load():
    if not os.path.isfile(LEDGER_FILE):
        return {}
    try:
        with open(LEDGER_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:                           # noqa: BLE001
        # 手改坏 / 写一半断电：引用定位失效，但不该影响出图本身。
        log.warning("moss 回执账本读不出来，按空处理")
        return {}


def remember(message_id, series_id, page_id, n):
    """记一笔 `message_id → 系列/页`。拿不到 id 只打日志，不抛。

    拿不到 id 的那张图之后引用认不出来——但编号还印在卡片上，
    用户手打 `moss续集 S7K3-P05` 仍然能接上，所以不是死路。
    """
    mid = str(message_id or "").strip()
    if not mid:
        log.warning("moss 回执没拿到 message_id（%s 第 %s 页），"
                    "这张之后只能靠卡片上的编号续", series_id, n)
        return ""
    with _LOCK:
        data = _load()
        data[mid] = {"series": series_id, "page": page_id, "n": int(n),
                     "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        if len(data) > KEEP:
            for k in sorted(data, key=lambda x: data[x].get("ts") or "")[:-KEEP]:
                data.pop(k, None)
        try:
            os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
            tmp = LEDGER_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, LEDGER_FILE)
        except OSError as exc:
            log.warning("moss 回执账本写不进去（%s），这张只能靠编号续", exc)
    return mid


def recall(message_id):
    with _LOCK:
        return _load().get(str(message_id or "").strip())


def locate(quoted_id="", quoted_text="", owner=""):
    """引用一段 → `(series_id, 页节点, 命中方式)`，认不出返回 `("", None, "")`。

    `quoted_text` 是被引消息的正文（引用段里除图之外的全部文字），
    用来在上面没有 id 时抠 `S7K3-P05`。

    `owner` 是**发指令那个会话**的键（`moss_series.session_key`）。同一条线上
    群聊和私聊可能都有「第 6 页」，手打编号时必须优先给他自己那个，
    否则会把群友送到别人的分支上（2026-10-31 用户要的会话隔离）。
    引用命中 `id` 时不看 owner——那是精确指向某一条消息，本来就不该改。
    """
    rec = recall(quoted_id) if quoted_id else None
    if rec:
        s = series_mod.load(rec.get("series") or "")
        if s:
            pg = series_mod.by_id(s, rec.get("page") or "")
            if pg:
                return s["id"], pg, "id"
    sid, n = series_mod.parse_code(quoted_text)
    if sid and n:
        s = series_mod.load(sid)
        if s:
            pg = series_mod.by_n(s, n, owner=owner)
            if pg:
                return sid, pg, "code"
    return "", None, ""

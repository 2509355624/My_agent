"""评分与意图判断里的纯字段处理。"""

from app.features.persona.prompt_facts import _parse_facts

NOTE_MAX_CHARS = 1000


def _director_fields(d):
    """从第一段的 JSON 里取**事实**字段（2026-11-09 收窄）。

    这里只剩事实：这一轮已经摆出来的东西（facts）、他这句是不是对着她说
    （addressed）、他有没有藏着话（secret）、有没有内射（finish）、道具进出
    （items）。

    分段（说·做·想）、潜台词、她察觉到的、这一拍的态度、主动的勾子全部
    不再由编剧产出——那五样都是「这一拍该怎么写」的判断，编剧只该记数值
    和已经发生的事，写法交给写作层。
    """
    if not isinstance(d, dict):
        return {"facts": [], "addressed": True, "secret": False,
                "finish": "unknown", "items": None}
    addressed = d.get("addressed", True)
    if isinstance(addressed, str):
        addressed = addressed.strip().lower() not in ("false", "0", "no", "否", "不")
    else:
        addressed = bool(addressed)
    secret = d.get("secret")
    if isinstance(secret, str):
        secret = secret.strip().lower() in ("true", "1", "yes", "是")
    return {
        "facts": _parse_facts(d.get("facts")),
        "addressed": addressed,
        # 他说没说出口，是事实；内容不抄给演员（他看得见的是行为，不是心声）。
        "secret": bool(secret),
        # 怀孕（2026-11-05）：编剧判这一轮有没有内射。代码只在跨天时用。
        "finish": _norm_finish(d.get("finish")),
        # 道具（2026-11-08）：v1 / 自由模式也走这条路，透传出去。
        "items": d.get("items") if isinstance(d.get("items"), dict) else None,
    }


def _norm_finish(value):
    v = str(value or "").strip().lower()
    return v if v in ("inside", "outside", "none", "unknown") else "unknown"


def _judge_ledger(note):
    """给第一段看的长期账本。空账本就写「还没有」，别塞一个空标题。"""
    text = str(note or "").strip()
    return text[:NOTE_MAX_CHARS] if text else "（还没有）"



def _item_keys_text():
    """道具清单的 key 列表，给编剧挑 use 用。**只给 key，不给 prompt**。

    写在提示词里的是「有哪些可选」，具体怎么写由 `app/items.py` 在
    用到那一条时注入——这样十几样道具都进清单也只占一行。
    """
    try:
        from app.features.persona import items as _items
        rows = []
        for key, it in (_items.all_items() or {}).items():
            if isinstance(it, dict):
                rows.append("%s=%s" % (key, it.get("name") or key))
        return "、".join(rows) if rows else "（无）"
    except Exception:                                     # noqa: BLE001
        return "（无）"

"""评分与意图判断里的纯字段处理。"""

import re

from app.features.persona.prompt_facts import _parse_facts

NOTE_MAX_CHARS = 1000

_RAG_DETAIL_RE = re.compile(
    r"第一次|初次|初夜|初吻|初口|初后|破处|原话|"
    r"当时.{0,8}(?:说|讲)|具体.{0,6}(?:哪|什么|怎么|几)|"
    r"在哪|什么时候|几点|哪天")
_RAG_PAST_RE = re.compile(
    r"上次|上回|之前|以前|当时|那时候|那天|昨天|前天|"
    r"还记得|记得|你说过|我说过|我们说过|约好|答应过|曾经|那次")


def _norm_rag(value):
    """编剧给的 RAG 路由；不认识的值当作没给。"""
    v = str(value or "").strip().lower()
    return v if v in ("none", "summary", "detail") else None


def rag_fallback(text):
    """编剧没给/解析失败时的保底路由；只认明确的过去指向。

    这是**只保底、不抢编剧判断**：编剧给了合法值就以编剧为准，
    这里只负责把漏掉的显式过去问法补回来。
    """
    t = str(text or "").strip()
    if not t:
        return None
    if _RAG_DETAIL_RE.search(t):
        return "detail"
    if _RAG_PAST_RE.search(t):
        return "summary"
    return None


def resolve_rag(value, text):
    """合并编剧判断与正则兜底，返回最终档位。

    规则（2026-11-18）：
      · 编剧给合法值 → 以编剧为准；
      · 编剧漏了/判挂了 → 用正则兜底，没过去指向就 none；
      · 显式过去问法只能把档位往上抬（none→summary / →detail），
        不能把编剧已经判的 detail 降下来。
    """
    mode = _norm_rag(value)
    fb = rag_fallback(text)
    if mode not in ("none", "summary", "detail"):
        return fb or "none"
    if fb == "detail" and mode != "detail":
        return "detail"
    if fb == "summary" and mode == "none":
        return "summary"
    return mode


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

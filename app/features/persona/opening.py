"""开场导读：新线第一次进网页时的一段场景说明。"""

import datetime
import logging

OPENING_MAX = 260
_DAYPARTS = ((5, "深夜"), (8, "早上"), (11, "上午"), (13, "中午"),
             (17, "下午"), (19, "傍晚"), (23, "夜里"), (24, "深夜"))
log = logging.getLogger("persona")


def _daypart():
    """现在大概是一天里的什么时候（只给个粗粒度，写进导读就够用）。"""
    from app.features.persona import service as svc

    h = datetime.datetime.now().hour
    for edge, name in _DAYPARTS:
        if h < edge:
            return name
    return "深夜"


def opening(session_key, force=False):
    """这条线的开场导读；生成过一次就缓存。

    没有角色时返回空串（网页不画那块灰条）。生成失败也返回空串——开场导读
    是个锦上添花的东西，不能因为它挂掉整页打不开。
    """
    from app.features.persona import service as svc

    card = svc.active_card(session_key)
    if card is None:
        return ""
    ent = svc._entry(session_key) or {}
    cur = str(ent.get("intro") or "").strip()
    if cur and not force:
        return cur
    # 卡级默认开场（2026-11-07）：卡里写了就直接用，**不再调模型**。
    # 优先级：session 的 `intro` > 卡的 `opening` > 现生成。
    # 为什么 session 优先：玩家已经聊过的线不该被卡里的开场覆盖——
    # 那等于把开场白换成另一段，历史还在，两边的场景对不上。
    card_open = str(card.get("opening") or "").strip()
    if card_open and not force:
        return card_open

    rel = ""
    try:
        st = svc.affection.status(session_key) or {}          # 顺手建好感记录
        bits = [str(st.get("levelName") or ""), str(st.get("tierName") or "")]
        rel = "、".join([b for b in bits if b]) or "还不熟"
    except Exception:                                     # noqa: BLE001
        rel = "还不熟"

    who = "，".join([b for b in (
        svc.char_name(card, "", key=session_key),
        str(card.get("description") or ""),
        str(card.get("personality") or "")) if b])
    sysp = str(card.get("systemPrompt") or "").strip()
    if sysp:
        who += "\n" + sysp[:260]
    prompt = svc._OPENING_PROMPT % (OPENING_MAX, who, rel, _daypart())
    try:
        text = str(svc._llm_call([{"role": "user", "content": prompt}],
                             max_tokens=700) or "").strip()
    except Exception:                                     # noqa: BLE001
        svc.log.exception("开场导读生成失败 %s", session_key)
        return ""
    if not text:
        return ""
    text = svc._cut_sentence(text, OPENING_MAX)
    state = svc._load_state()
    st = state.get(session_key)
    if isinstance(st, dict):
        st["intro"] = text
        svc._save_state(state)
    svc.log.info("开场导读已生成 %s（%d 字）", session_key, len(text))
    return text

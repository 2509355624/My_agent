"""会话线的键派生：群聊按人隔离、私聊按角色分线、每卡三个存档槽。

纯字符串运算，不读盘；`resolve_line` 这类需要状态的逻辑留在 service。
"""

# 每张卡最多几个存档（2026-11-08 用户拍板：3 个）。
# slot 0 是**老存档**，key 不带后缀——加这一列之前的所有线都在 slot 0。
SLOT_MAX = 3


def _persona_key(session_key, sender_uin=""):
    """扮演态的状态键：群里按人隔离，私聊不隔离。"""
    key = str(session_key or "")
    uid = str(sender_uin or "").strip()
    if not uid or not key.startswith("group_"):
        return key
    return key + "_" + uid


def _slot_suffix(slot):
    """slot 0 → 空串（老 key 原样）；1/2 → `_s1` / `_s2`。"""
    try:
        n = int(slot or 0)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(SLOT_MAX - 1, n))
    return "" if n == 0 else "_s%d" % n


def _card_key_slot(session_key, card_id, slot=0):
    key = str(session_key or "")
    cid = str(card_id or "").strip()
    if not cid or not key:
        return key
    sfx = _slot_suffix(slot)
    # 已经带过了就不要再叠（幂等）；后缀也要一起比。
    if key.endswith("__" + cid + sfx):
        return key
    return key + "__" + cid + sfx


def card_key(session_key, card_id=""):
    """「这个人 × 这个角色」的会话键；card_id 为空时原样返回。"""
    return _card_key_slot(session_key, card_id, 0)


def card_slot_of(key, card_id=""):
    """反查一个线键是这张卡的第几个存档；读不出来当 0（老档）。"""
    key = str(key or "")
    cid = str(card_id or "").strip()
    if not cid or not key:
        return 0
    for n in range(SLOT_MAX - 1, 0, -1):
        if key.endswith("__" + cid + "_s%d" % n):
            return n
    return 0

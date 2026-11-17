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


def resolve_line(session_key):
    """base 键 → 此刻该用的**线键**（QQ 私聊分线，2026-11-03 方案 B）。

    网页端调用方（persona_api）传进来的已经是线键（带 `__`），原样返回；
    QQ 端传的是 base（`private_<qq>`），按 base 上记的 `card` 派生出
    `private_<qq>__<角色>`。这样：

      · 切角色 → 各聊各的线，历史/小记/好感互不污染（和网页一致）
      · 切回来 → 接着原来那条线聊
    群聊不分线（已停演），没在演时返回 base 本身。
    """
    from app.features.persona import service as svc

    key = str(session_key or "")
    if "__" in key or not key.startswith("private_"):
        return key
    ent = svc._entry(key) or {}
    cid = str(ent.get("card") or "").strip()
    if not cid:
        return key
    # 存档槽（2026-11-08）：base 上记着 slot 就派生出带后缀的线键。
    # 没有这个字段的老档按 slot 0 走，行为完全不变。
    try:
        slot = int(ent.get("slot") or 0)
    except (TypeError, ValueError):
        slot = 0
    return _card_key_slot(key, cid, slot)


def status_brief(session_key):
    """每轮跟在角色回复后面的「此刻」窄条（2026-11-05 用户要求）。

    和手动「状态」的分工：那边是**完整阶梯表**；这边只报箭头 ▶ 指着的那
    一格，免得每轮都把表格铺一遍。没在演（取不到线）返回 ""，调用方别拼。
    """
    from app.features.persona import service as svc

    try:
        return svc.affection.panel_brief(svc.resolve_line(session_key))
    except Exception:                            # noqa: BLE001
        svc.log.warning("生成「此刻」简报失败 %s", session_key, exc_info=True)
        return ""


def switch_line(base, card, slot=0):
    """切角色 / 切存档（2026-11-08 加 slot）：**两条都要写**——

      · 线键 `base__<card_id>`：这条角色线的真实状态（历史/小记/好感都挂它）
      · base：记「此刻在演谁」的副本（`resolve_line` 靠它找线）

    切回一条演过的线时，`svc.switch()` 会保留它的小记/轮次——真正「接着聊」。
    网页端 svc.switch 端点与本函数是同一套语义（2026-11-03 起共用这里）。
    """
    from app.features.persona import service as svc

    ckey = _card_key_slot(base, card["id"], slot)
    svc.switch(ckey, card)
    st = svc._load_state()
    st[base] = dict(st.get(ckey) or {})
    # base 上记「此刻是这张卡的第几个存档」——`_active_key` / `resolve_line`
    # 靠它派生出线键。slot 0 不写（老档没有这个字段，行为不变）。
    try:
        n = int(slot or 0)
    except (TypeError, ValueError):
        n = 0
    if n:
        st[base]["slot"] = n
    else:
        st[base].pop("slot", None)
    svc._save_state(st)
    return ckey


def switch(session_key, card):
    """进扮演态（`session_key` 应传**线键**，见 `switch_line`/`resolve_line`）。

    分线后的语义（2026-11-03 方案 B）：
      - **新线**（这个角色在这条会话没演过）→ 小记/轮次/心情全空白；
      - **切回旧线**（条目已存在且 card 相同）→ **原样保留**小记/轮次/心情
        ——「切回来接着原来那条线聊」，和网页的承诺一致。
        （旧版这里无条件清零，网页切回来小记也被洗掉，是同一处疏漏。）

    「上一个角色的记忆不该带进来」由**分线本身**保证：每条角色线有自己的
    小记和历史，换人 = 换线，天然互不污染。
    """
    from app.features.persona import service as svc

    state = svc._load_state()
    old = state.get(session_key)
    if isinstance(old, dict) and str(old.get("card") or "") == card["id"]:
        svc.log.info("扮演态回到旧线：%s（保留小记 %d 字 / %s 轮）",
                 session_key, len(str(old.get("note") or "")),
                 old.get("turns"))
        return
    state[session_key] = {"card": card["id"], "note": "", "turns": 0,
                          "mood": "", "intensity": 0, "state": ""}
    svc._save_state(state)
    svc.log.info("扮演态开启：%s → %s（%s，新线）",
             session_key, card["name"], card["id"])


def _match_card_in_text(text):
    """在一段描述里找卡名，返回命中的第一张卡（找不到返回 None）。

    场景：「角色扮演，你是我的白色小兔妹妹，我们相依为命……」——用户没写
    精确卡名，而是用一段话描述他要谁。这里按**卡名出现位置**找，
    **长得越长的名字优先**（「妹妹闺蜜」要压过「妹妹」）。
    """
    from app.features.persona import service as svc

    t = str(text or "")
    if not t:
        return None
    hits = []
    for c in svc.load_cards():
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        pos = t.find(name)
        if pos >= 0:
            hits.append((pos, -len(name), c))
    if not hits:
        return None
    hits.sort(key=lambda x: (x[0], x[1]))
    return hits[0][2]


def _group_redirect(card):
    """群里不让开演，回一段说明 + 去处。

    2026-11-03 用户反馈「群里其他人能看到别人的聊天内容」——状态隔离没坏，
    坏的是**群聊内容本来就公开**。所以群里不再实际扮演。
    """
    from app.features.persona import service as svc

    lines = [
        "这里人多，**不方便演**——我说的话一屋子人都看得到。",
        "",
        "想跟她单独聊，两个地方：",
        "",
        "1. **私聊我**，发「%s」就能开始" % card["name"],
    ]
    try:
        from app.features.qq import friend_site
        site = friend_site.url()
    except Exception:                              # noqa: BLE001
        site = ""
    if site:
        lines.append("2. **网页上玩**（能点按钮出图、看她的状态）：")
        lines.append("   %s/persona" % site.rstrip("/"))
    else:
        lines.append("2. **网页上玩**：找我主人要个地址")
    lines.append("")
    lines.append("（群里发「#角色扮演」还是能看到完整规则的。）")
    return "\n".join(lines)

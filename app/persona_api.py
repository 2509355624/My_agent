"""网页端角色扮演的 HTTP 接口（2026-11-03）。

## 为什么要有它

`persona.py` 那套引擎本来就是平台无关的——`run_turn()` 只管「一句话进、
一段叙述出」，QQ 只是其中一个调用方。用户反馈「QQ 单纯的指令很多人根本
不会玩，网页端点按钮更直观」，所以补一层网页入口。

**引擎一行都不改**：这里只做「解析请求 → 调 persona → 包成 JSON」。
会话键用 `web_<qq>`，和 QQ 的 `private_<qq>` 完全隔离——同一个人在两边
各聊各的，互不串味。

## 身份

沿用画廊那套：请求带 `qq` 参数，用 `friend_auth.QQ_RE` 校验。
没有登录态也能跑单人本地的场景（不传 qq 时退回 `web_local`）。
"""
import json
import logging
import os
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from app import chat_bg, friend_auth, llm, persona, usage

log = logging.getLogger("persona_api")

bp = Blueprint("persona_api", __name__)

# 游客身份 cookie（2026-11-03）。和登录 cookie 分开：游客换浏览器会重来，
# 这是已知且接受的——体验版是为了让人尝到甜头，不是防盗版。
GUEST_COOKIE = "gs_id"
GUEST_COOKIE_TTL = 30 * 24 * 3600      # cookie 活 30 天（对话数据只保 24 小时）

# 游客固定演这一张卡（2026-11-05 用户拍板：**只给一个角色**，不再允许换人）。
# `junior` = 学妹（谦逊、崇拜学长、略带羞涩）。
GUEST_CARD_ID = "junior"


def _guest_free(ckey):
    """游客一律自由模式（2026-11-05 用户拍板）。

    体验版没有攻略面板、没有模式开关，所以不该被档位/结局/戒备锁住。
    必须**显式**写一条 free 记录：`affection._ensure` 给没有记录的线默认建的
    是 raid（2026-11-04「一进来就是攻略模式」留下的行为），不写就会跑成攻略。
    已经有 free 记录就直接返回——不每轮都碰状态文件。
    """
    ent = persona._entry(ckey) or {}                      # noqa: SLF001
    aff = ent.get("affection") if isinstance(ent, dict) else None
    if isinstance(aff, dict) and str(aff.get("mode") or "") == "free":
        return
    try:
        from app import affection
        affection.set_mode(ckey, False)
    except Exception:                                     # noqa: BLE001
        log.warning("游客线转自由模式失败 %s", ckey, exc_info=True)


# ── 「回复 ↔ 漫画页」映射（2026-11-03）────────────────────
#
# 出图成功后存一份「哪条回复 → 漫画第几页」到**服务器**。以前这层关联只活
# 在浏览器 localStorage（picStore）里，换设备/手机重登/清缓存就全丢——历史
# 气泡下面空空如也，但图明明在服务器磁盘上。
# 结构：{会话线 key: {"ai3": {"sid": "9J74", "page": 5}}}
_PIC_LOCK = threading.Lock()


def _pic_map_path():
    from app.config import BASE_DIR
    return os.path.join(str(BASE_DIR), "state", "persona_pic_map.json")


def _pic_map_load():
    try:
        with open(_pic_map_path(), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _pic_map_set(key, ai_key, val):
    with _PIC_LOCK:
        d = _pic_map_load()
        d.setdefault(key, {})[ai_key] = val
        tmp = _pic_map_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, _pic_map_path())


def _pic_map_clear(key):
    """清空存档时连图映射一起抹（2026-11-04）。

    不清的话：清空后重开同一条线，归档行号从 0 重新数，旧的 `n5` 会挂到
    新对话的第 6 条上 —— 图又「自己跑到最新消息下面」了。
    """
    with _PIC_LOCK:
        d = _pic_map_load()
        if key not in d:
            return
        d.pop(key, None)
        tmp = _pic_map_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False)
            os.replace(tmp, _pic_map_path())
        except OSError:
            log.exception("图片映射清理失败 %s", key)


def _pic_map_drop_tail(key, dropped, total):
    """回退后抹掉被删那几条的图映射（2026-11-04）。

    归档行号是**复用**的：退掉最后一轮再聊，新一轮还是占 `n<total-2>`/
    `n<total-1>` 这两个号——旧映射不删，上一次那张图会「鬼影」到新气泡上
    （和 `_pic_map_clear` 注释里是同一类坑，只是范围小）。
    `total` 是回退**前**的归档长度，被删的是最后 `dropped` 行。
    """
    if dropped <= 0 or total <= 0:
        return
    slots = ["n%d" % i for i in range(max(0, total - dropped), total)]
    with _PIC_LOCK:
        d = _pic_map_load()
        ent = d.get(key)
        if not ent:
            return
        for s in slots:
            ent.pop(s, None)
        tmp = _pic_map_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False)
            os.replace(tmp, _pic_map_path())
        except OSError:
            log.exception("回退图片映射清理失败 %s", key)


def _qq():
    """取当前用户。跟 `gallery_api._qq()` 同一套口径：query 或 body 里带。

    2026-11-03：加了 cookie 兜底——5175 网关会把登录态 cookie 一起转过来，
    不用每次都在 URL 上挂 `?qq=`。
    """
    qq = str(request.args.get("qq") or "").strip()
    if not qq:
        body = request.get_json(silent=True) or {}
        qq = str(body.get("qq") or "").strip()
    if qq and friend_auth.QQ_RE.match(qq):
        return qq
    # 登录 cookie（5175 网关转发时带来的）
    try:
        ck = friend_auth.check_cookie(
            request.cookies.get(friend_auth.COOKIE_NAME))
    except Exception:                              # noqa: BLE001
        ck = None
    return ck or ""


def _guest_id():
    """取游客 id；没有就当场签一个。返回 (裸 uuid, 需要下发的 cookie 值)。"""
    from app import guest
    raw = request.cookies.get(GUEST_COOKIE) or ""
    uid = guest.check_id(raw)
    if uid:
        return uid, ""
    token = guest.issue_id()
    return guest.check_id(token), token


def _who():
    """这一轮是谁：`("member", qq)` 或 `("guest", uuid)`。

    登录优先。游客是兜底——用户 2026-11-03 定：登录能无限聊，游客体验 15 轮。
    """
    qq = _qq()
    if qq:
        return "member", qq, ""
    uid, token = _guest_id()
    return "guest", uid, token


def _attach_guest_cookie(resp, token):
    """给游客下发身份 cookie（只在第一次需要签发时调）。"""
    if token:
        resp.set_cookie(GUEST_COOKIE, token, max_age=GUEST_COOKIE_TTL,
                        httponly=True, samesite="Lax", path="/")


def _key(qq):
    """网页会话的基础键。**和 QQ 的 `private_<qq>` 分开**，两边各聊各的。

    不隔离的话，网页上说一句、QQ 上接着说，会变成同一个上下文，
    用户会看到「她怎么突然记得我在另一头说过的话」。
    """
    return "web_" + (qq or "local")


def _active_key(base):
    """「这个人此刻在跟谁演」→ 真正要用的会话键。

    2026-11-03 用户要求：「选择其他的角色就是另一个会话了」。

    所以一个 base（`web_<qq>`）下可能同时挂着多个角色的活跃态：

        web_2509355624__izumi_sagiri   演纱雾时的历史
        web_2509355624__rem            演雷姆时的历史

    怎么知道「此刻」是哪个？看 base 上记的 `card`——`switch()` 会写进去。
    base 上没有记录时（还没选过角色）就返回 base 本身，保持老行为。

    这样：
      · 切角色 → 切到另一条独立的线，历史不混
      · 切回来 → 接着原来那条线聊
      · 不传角色 → 用最近演的那个
    """
    try:
        ent = persona._entry(base) or {}                  # noqa: SLF001
    except Exception:                                     # noqa: BLE001
        return base
    cid = str(ent.get("card") or "").strip()
    if not cid:
        return base
    return persona.card_key(base, cid)


def _card_json(card):
    if not card:
        return None
    return {
        "id": card.get("id") or "",
        "name": card.get("name") or "",
        "description": card.get("description") or "",
        "personality": card.get("personality") or "",
        "avatar": card.get("avatar") or "",
    }


@bp.route("/api/persona/cards")
def list_cards():
    """可选角色列表：网页左栏用。hidden 的卡是暂下架，不进这个列表
    （卡池页 /api/characters 仍能看到全部，那边的开关管上下架）。"""
    out = []
    for c in persona.load_cards():
        if c.get("hidden"):
            continue
        out.append(_card_json(c))
    return jsonify({"ok": True, "cards": out})


def _card_by_id(cid):
    """按 id 或名字找卡。

    2026-11-03：`persona.find_card()` 先查卡列表缓存，测试里换过
    `AGENTS_DIR` 之后那份缓存可能还指着旧目录。这里直接遍历
    `load_cards()` 的结果——它自己会处理缓存，不做二次 clear。
    """
    cid = str(cid or "").strip()
    if not cid:
        return None
    low = cid.lower()
    for c in persona.load_cards():
        if str(c.get("id") or "").lower() == low:
            return c
        if str(c.get("name") or "").lower() == low:
            return c
    return None


def _msgs_from(rows, start):
    """归档切片 → 前端要的消息数组。

    `n` = 归档行号（`start + i`）。前端拿它当气泡 id、图片锚点和翻页游标——
    它是稳定的：归档只增不删，而窗口序号每聊一轮就整体前移一位。
    """
    out = []
    for i, m in enumerate(rows):
        n = start + i
        if m.get("role") == "user":
            out.append({"n": n, "role": "user", "text": m.get("content") or ""})
            continue
        reply, scene, mood, intensity, state = persona.split_turn(
            m.get("content") or "")
        if reply.strip():
            out.append({"n": n, "role": "assistant", "text": reply,
                        "scene": scene, "mood": mood,
                        "intensity": intensity, "state": state})
    return out


@bp.route("/api/persona/session")
def get_session():
    """这条会话现在是什么状态：演的谁、聊了几轮、她此刻的样子。

    2026-11-04：消息从**归档**取最后 `ARCHIVE_PAGE` 条（以前是 12 条滑动窗口，
    聊过 6 轮就翻不到更早的）。更早的由 `/api/persona/history` 分页往回翻。
    """
    role, who, token = _who()

    if role == "guest":
        return _guest_session(who, token)

    key = _active_key(_key(who))
    ent = persona._entry(key) or {}                       # noqa: SLF001
    card = _card_by_id(ent.get("card"))

    start, rows, total = persona.history_page(key)
    msgs = _msgs_from(rows, start)

    aff = _aff_status(key)
    # 状态行跟着「她说的最后一句话」走（2026-11-04）：刷新后历史里没有
    # 每轮的 aff 快照，手机端就整条状态凭空消失。只把**当前**状态挂到
    # 最后一条她的回复上——那条之后没再发生过任何事，所以它就是此刻。
    if aff and str(aff.get("mode") or "") == "raid":
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                m["aff"] = aff
                break

    return jsonify({
        "ok": True,
        "card": _card_json(card),
        "note": str(ent.get("note") or ""),
        "intro": str(ent.get("intro") or ""),
        "state": str(ent.get("state") or ""),
        "mood": str(ent.get("mood") or ""),
        "intensity": int(ent.get("intensity") or 0),
        "turns": persona.turn_total(key),
        "messages": msgs,
        "total": total,                 # 归档总条数（前端翻页的游标上限）
        "more": start > 0,              # 还有更早的可以往回翻
        "affection": aff,
        "day": persona.game_day(key),
        "phase": persona.game_phase(key),
        "preg": persona.pregnancy_status(key),
        "role": "member",
        "rolled_back": bool(ent.get("rollback_done")),
        # 还没回答的「射在哪里」（2026-11-06）：刷新/断线后前端靠它重弹，
        # 不然高潮值都扣了、正文却永远缺一块。
        "pending": persona.pending_finish(key),
    })


@bp.route("/api/persona/opening")
def persona_opening():
    """开场导读：她此刻在哪、在干什么、你们什么关系、从哪儿开口。

    2026-11-04 用户要的：「不然我就莫名其妙地开始了，都不知道怎么聊」。
    没有历史的新线上，网页先画一块灰条说明背景；生成一次就缓存进状态，
    清空存档后重铺。生成失败回空串，前端就不画那块（不该挡住整页）。
    """
    role, who, _token = _who()
    if role == "guest":
        # 游客攻略化（2026-11-05）：游客也有自己的线，同样给开场导读——
        # 「莫名其妙地开始，不知道怎么聊」对游客更是劝退。
        ckey = _guest_line(who)
        if ckey is None:
            return jsonify({"ok": True, "intro": ""})
        return jsonify({"ok": True, "intro": persona.opening(ckey)})
    key = _active_key(_key(who))
    return jsonify({"ok": True, "intro": persona.opening(key)})


@bp.route("/api/persona/history")
def history():
    """往回翻更早的聊天记录（2026-11-04）。

    以前网页只喂 12 条滑动窗口，聊过 6 轮就看不到更早的——数据其实一直在
    `persona_archive_*.jsonl` 里，只是接口没给。现在首屏给最后一页，往上滚
    一页要一页。

    query:
      - `before`：归档行号（不含），不传 = 最后一页
      - `limit`：一页多少条，默认 `persona.ARCHIVE_PAGE`

    游客攻略化（2026-11-05）后也有归档，同一套分页照给——30 轮也够翻几页的。
    """
    role, who, _token = _who()
    if role == "guest":
        ckey = _guest_line(who)
        if ckey is None:
            return jsonify({"ok": True, "messages": [], "total": 0,
                            "more": False})
        key = ckey
    else:
        key = _active_key(_key(who))
    start, rows, total = persona.history_page(
        key, request.args.get("before"), request.args.get("limit"))
    return jsonify({
        "ok": True,
        "messages": _msgs_from(rows, start),
        "total": total,
        "more": start > 0,
    })


def _aff_status(key):
    """这条线的好感状态（给网页状态条）；没开过攻略返回 None。"""
    try:
        from app import affection
        return affection.status(key)
    except Exception:                                     # noqa: BLE001
        return None


def _aff_panel(key):
    """整张状态面板（2026-11-04 深夜）：网页右栏的竖排表直接画这个。

    和 `status` 分开是因为两者给的东西不一样：`status` 是「此刻那几个词」，
    面板是**全部档位 + 当前卡在第几个**。前端要画「一列一列看数值」，
    只有 status 是不够的——它拿不到「下一级叫什么、还差多少」之外的行。
    """
    try:
        from app import affection
        return affection.panel(key)
    except Exception:                                     # noqa: BLE001
        return None


def _guest_line(uid):
    """游客的 persona 线键：**固定学妹，一条线，不能换人**。

    2026-11-05 用户拍板回到单角色体验：游客只演 `GUEST_CARD_ID`（学妹），
    不提供切角色——既然只有一个角色，也就没有「演谁」要记，没有多线。
    每条游客线都强制自由模式（见 `_guest_free`），体验版没有攻略模式。
    """
    card = _card_by_id(GUEST_CARD_ID)
    if card is None:
        cards = persona.load_cards()
        card = cards[0] if cards else None
    if card is None:
        return None
    ckey = persona.card_key("guest_" + str(uid or ""), str(card.get("id") or ""))
    if persona._entry(ckey) is None:                      # noqa: SLF001
        persona.switch(ckey, card)
        log.info("游客新线：%s（%s）", ckey, card["name"])
    _guest_free(ckey)
    return ckey


def _guest_sweep():
    """清掉超过 `guest.GUEST_TTL` 没动的游客线（2026-11-05）。

    游客改走 persona 存储后，历史/归档/好感都挂在 `guest_*` 线上——
    老 guest 目录那套「只点名不删」的约定不再够用，状态文件会一直涨。
    这里按**窗口文件的 mtime** 判过期，整线擦掉（历史+好感+条目一起）。
    只在游客聊天入口调用：读 state 的 key 列表 + 每个 key 一次 getmtime，
    量级很小。
    """
    import os
    import time
    from app import guest as G
    now = time.time()
    state = persona._load_state()                         # noqa: SLF001
    stale = []
    for key in list(state):
        if not key.startswith("guest_"):
            continue
        try:
            age = now - os.path.getmtime(persona._hist_path(key))  # noqa: SLF001
        except OSError:
            continue                       # 从没聊过 / 已清：不动
        if age > G.GUEST_TTL:
            stale.append((key, age))
    for key, age in stale:
        try:
            persona.wipe(key)
            log.info("游客线过期清理：%s（%.1f 小时没动）", key, age / 3600.0)
        except Exception:                                 # noqa: BLE001
            log.exception("游客线清理失败 %s", key)


def _guest_turns(ckey):
    """游客已用轮数 = 归档里的 assistant 条数。

    ⚠️ 不能拿 persona 线的 `turns` 字段——那是**小记压缩计数**，每满
    `NOTE_EVERY_TURNS` 轮被 `compress_note` 清零，拿来当额度永远到不了 30。
    归档只增不删、每轮恰好 +2 行，数 assistant 就是真实轮数。
    """
    try:
        rows = persona.load_archive(ckey)
    except Exception:                                     # noqa: BLE001
        return 0
    return sum(1 for r in rows if (r or {}).get("role") == "assistant")


def _guest_session(uid, token):
    """游客的会话状态。

    2026-11-05：和登录用户同一个会话形状（归档分页 / 开场导读 / 状态都给），
    只多 `remaining` / `limit` 两个字段——30 轮上限保留（用户拍板），
    到顶后前端锁输入框引导登录。固定学妹、默认自由模式。
    """
    from app import guest
    ckey = _guest_line(uid)
    if ckey is None:
        return jsonify({"ok": False, "error": "没有可用角色"}), 500

    ent = persona._entry(ckey) or {}                      # noqa: SLF001
    card = _card_by_id(ent.get("card"))
    start, rows, total = persona.history_page(ckey)
    msgs = _msgs_from(rows, start)

    aff = _aff_status(ckey)
    # 和成员端同一条规则：当前状态挂到最后一她的回复上，刷新不丢状态行。
    if aff and str(aff.get("mode") or "") == "raid":
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                m["aff"] = aff
                break

    turns = _guest_turns(ckey)
    resp = jsonify({
        "ok": True,
        "card": _card_json(card),
        "note": str(ent.get("note") or ""),
        "intro": str(ent.get("intro") or ""),
        "state": str(ent.get("state") or ""),
        "mood": str(ent.get("mood") or ""),
        "intensity": int(ent.get("intensity") or 0),
        "turns": turns,
        "messages": msgs,
        "total": total,
        "more": start > 0,
        "affection": aff,
        "day": persona.game_day(ckey),
        "phase": persona.game_phase(ckey),
        "preg": persona.pregnancy_status(ckey),
        "role": "guest",
        "remaining": max(0, guest.GUEST_TURNS - turns),
        "limit": guest.GUEST_TURNS,
        "rolled_back": bool(ent.get("rollback_done")),
    })
    _attach_guest_cookie(resp, token)
    return resp


@bp.route("/api/persona/switch", methods=["POST"])
def switch():
    """切角色。body: {name 或 id}"""
    body = request.get_json(silent=True) or {}
    want = str(body.get("id") or body.get("name") or "").strip()
    if not want:
        return jsonify({"ok": False, "error": "没说演谁"}), 400

    role, who, token = _who()
    if role == "guest":
        # 游客固定学妹（`GUEST_CARD_ID`），不提供换人（2026-11-05 用户拍板）：
        # 体验版只给一个角色，换人等于把体验版变成免费全集，也没必要。
        resp = jsonify({
            "ok": False,
            "error": "体验版只能玩学妹。登录后可以随便换。",
            "limit": True,
        })
        resp.status_code = 403
        _attach_guest_cookie(resp, token)
        return resp

    base = _key(who)
    card = persona.find_card(want)
    if card is None:
        return jsonify({"ok": False, "error": "没有这个角色：%s" % want}), 404

    # 两条线都要写：
    #   base      记「此刻在演谁」（`_active_key` 靠它找）
    #   ckey      角色专属线，历史/小记/状态都挂这儿
    # 这样切角色 = 换一条独立的线，切回来还能接上。
    # （2026-11-03 起这段逻辑收进 persona.switch_line——QQ 私聊分线用的是
    #   同一套，切回旧线时小记/轮次保留的行为两边自然一致。）
    ckey = persona.switch_line(base, card)                # noqa: SLF001
    # 默认攻略模式（2026-11-04）：切过来就把好感记录建好（按这张卡的起点），
    # 右栏一打开就有「关系 · 攻略模式」，不用等玩家先发一句话。
    try:
        from app import affection
        affection.status(ckey)
    except Exception:                                     # noqa: BLE001
        pass
    log.info("网页切角色：%s → %s（线 %s）", base, card["name"], ckey)
    return jsonify({"ok": True, "card": _card_json(card),
                    "reply": "从现在起我是%s。" % card["name"]})


@bp.route("/api/persona/leave", methods=["POST"])
def leave():
    """「出戏」：退出扮演，保留历史。"""
    role, who, token = _who()
    if role == "guest":
        resp = jsonify({"ok": True, "reply": "体验版不用出戏，关掉页面就行。"})
        _attach_guest_cookie(resp, token)
        return resp
    key = _active_key(_key(who))
    if not persona.active_card(key):
        return jsonify({"ok": True, "reply": "本来就没在演。"})
    persona.leave(key)
    return jsonify({"ok": True, "reply": "出戏了。"})


@bp.route("/api/persona/clear", methods=["POST"])
def clear():
    """清空记忆，保留角色。游客没有「保留记忆」的概念，直接回一句话即可。"""
    role, who, token = _who()
    if role == "guest":
        resp = jsonify({"ok": True,
                        "reply": "体验版不能清空。登录后可以随时重来。"})
        _attach_guest_cookie(resp, token)
        return resp
    key = _active_key(_key(who))
    reply = persona.clear(key)
    _pic_map_clear(key)          # 归档一起没了，图映射留着只会挂错位置
    return jsonify({"ok": True, "reply": reply})


@bp.route("/api/persona/finish", methods=["POST"])
def finish_api():
    """她高潮满了，玩家在弹窗里选「射在哪里 / 自己写做法」（2026-11-05）。

    body: `{spot?, text?, custom?}`

    2026-11-05 晚改：这里**直接生成**那一拍，不再只存选择。用户说
    「选完还要等玩家再发一句话」很别扭——他得猜什么时候该写射精。
    现在选完就出内容，玩家只负责推动剧情。

    `spot` 是预设位置（mouth/body/pussy/anus/none），`text` 是玩家自己
    写的做法；两个都给时以 `text` 为准。
    """
    body = request.get_json(silent=True) or {}
    spot = str(body.get("spot") or "").strip().lower()
    text = str(body.get("text") or "").strip()[:120]
    llm.set_custom_override(body.get("custom"))
    try:
        role, who, token = _who()
        if role == "guest":
            return jsonify({"ok": False, "error": "体验版没有这个"}), 403
        key = _active_key(_key(who))
        if persona.active_card(key) is None:
            return jsonify({"ok": False, "error": "还没选角色"}), 400
        # 什么都没给 = 玩家放弃这一下，不算错误（2026-11-06 挪到调用**之前**：
        # 以前和「生成失败」共用同一个 skipped 回包，前端分不清，失败了也当
        # 没事发生——用户就这么丢过一段射精文本）。
        if not spot and not text:
            persona._set_pending_finish(key, None)     # noqa: SLF001
            return jsonify({"ok": True, "skipped": True, "reply": ""})
        with usage.scope(key):
            turn = persona.finish_action(key, spot=spot, text=text)
        if turn is None or not turn.reply.strip():
            # 给了指令却没写出来 = 真失败。回 502，前端据此**保留弹窗**让玩家重试。
            return jsonify({"ok": False, "error": "这一下没写成，再试一次"}), 502
        out = {"ok": True, "reply": turn.reply, "scene": turn.scene,
               "mood": turn.mood, "intensity": turn.intensity,
               "state": turn.state, "role": "member"}
        total = persona.archive_len(key)
        out["n"] = {"user": max(0, total - 2), "assistant": max(0, total - 1)}
        out["preg"] = persona.pregnancy_status(key)
        return jsonify(out)
    finally:
        llm.set_custom_override(None)


@bp.route("/api/persona/finish_night", methods=["POST"])
def finish_night_api():
    """第 5 次之后：结束 / 再来最后一次（2026-11-05）。

    body: `{last, custom?}`
    · last=true  —— 写这一夜的最后一下（**不跨天**）；前端接着调
                    `/api/persona/next_morning` 写第二天，中间插「第二天」标识。
    · last=false —— 收场，留在当晚。
    """
    body = request.get_json(silent=True) or {}
    last = bool(body.get("last"))
    llm.set_custom_override(body.get("custom"))
    try:
        role, who, token = _who()
        if role == "guest":
            return jsonify({"ok": False, "error": "体验版没有这个"}), 403
        key = _active_key(_key(who))
        if persona.active_card(key) is None:
            return jsonify({"ok": False, "error": "还没选角色"}), 400
        with usage.scope(key):
            turn = persona.finish_night(key, last=last)
        if turn is None or not turn.reply.strip():
            return jsonify({"ok": False, "error": "没写出来，再试一次"}), 502
        return jsonify(_turn_out(key, turn, next_stage="morning" if last else ""))
    finally:
        llm.set_custom_override(None)


@bp.route("/api/persona/next_morning", methods=["POST"])
def next_morning_api():
    """跨到第二天早晨（2026-11-05）：一次单独请求。

    单独一个接口是为了让前端在两次请求之间浮出「第二天」那个标识——
    生成期间锁输入，生成完才放开。
    """
    body = request.get_json(silent=True) or {}
    llm.set_custom_override(body.get("custom"))
    try:
        role, who, token = _who()
        if role == "guest":
            return jsonify({"ok": False, "error": "体验版没有这个"}), 403
        key = _active_key(_key(who))
        if persona.active_card(key) is None:
            return jsonify({"ok": False, "error": "还没选角色"}), 400
        with usage.scope(key):
            turn = persona.next_morning(key)
        if turn is None or not turn.reply.strip():
            return jsonify({"ok": False, "error": "没写出来，再试一次"}), 502
        return jsonify(_turn_out(key, turn))
    finally:
        llm.set_custom_override(None)


def _turn_out(key, turn, next_stage=""):
    """三段请求共用的回包形状（和 `/chat` 对齐，前端不用分支）。"""
    out = {"ok": True, "reply": turn.reply, "scene": turn.scene,
           "mood": turn.mood, "intensity": turn.intensity,
           "state": turn.state, "role": "member"}
    total = persona.archive_len(key)
    out["n"] = {"user": max(0, total - 2), "assistant": max(0, total - 1)}
    out["day"] = persona.game_day(key)
    out["phase"] = persona.game_phase(key)
    out["preg"] = persona.pregnancy_status(key)
    out["turns"] = persona.turn_total(key)   # 累计交流轮次（不是压缩节拍）
    # 三段请求里 `finish_night` / `next_morning` 会调用 `affection.sleep`
    # 收场，冲动/高潮/场景都变了。不把最新结果带回去，右栏要等玩家手动刷新
    # 才会更新（2026-11-06 群友反馈「切到第二天状态栏不刷新」）。
    aff_now = _aff_status(key)
    if aff_now:
        out["affection"] = aff_now
    if next_stage:
        out["next"] = next_stage
    return out


@bp.route("/api/persona/chat", methods=["POST"])
def chat():
    """一轮对话。body: {text, custom?}

    登录用户 → 无限；游客 → 30 轮（按 AI 回复次数算），超了硬拒绝。
    `custom`（2026-11-05）：{base_url, model, api_key}——用户自带的
    OpenAI 兼容模型，配置只存在浏览器 localStorage，随请求带给后端**临时
    用一轮**（主回复、好感打分、小记压缩全走它），请求结束立刻摘掉，
    后端不落盘。缺 custom 或字段不全 = 用默认模型，老前端不受影响。
    """
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    # 精确输入（2026-11-06）：主输入框可以空，只要有一个分栏有内容就能发。
    manual = persona._manual_norm(body.get("fields"))       # noqa: SLF001
    if not text and not manual:
        return jsonify({"ok": False, "error": "说点什么"}), 400

    llm.set_custom_override(body.get("custom"))
    try:
        return _chat_turn(text, manual)
    finally:
        llm.set_custom_override(None)


def _chat_turn(text, manual=None):
    role, who, token = _who()

    if role == "guest":
        return _guest_chat(text, who, token, manual)

    key = _active_key(_key(who))
    card = persona.active_card(key)
    if card is None:
        return jsonify({"ok": False, "error": "还没选角色"}), 400

    # 用量归属（2026-11-03）：网页端的扮演调用以前没打标签，全落进 usage 的
    # "other" 兜底，和跑图台、管理页的调用混在一起——「网页端扮演烧了多少
    # token」永远统计不出来。QQ 侧是在 qq_bot 的轮次外层包的，这里补同一层，
    # 标签用活动线键（web_<qq>__<card>），能精确到「哪个角色烧了多少」。
    with usage.scope(key):
        turn = persona.run_turn(key, text, "private", "", manual=manual)
    if not turn.reply.strip():
        return jsonify({"ok": False, "error": "她这轮没说话"}), 502
    out = {
        "ok": True,
        "reply": turn.reply,
        "scene": turn.scene,
        "mood": turn.mood,
        "intensity": turn.intensity,
        "state": turn.state,
        "role": "member",
    }
    # 这一轮落在归档的第几行（2026-11-04）：前端拿它当气泡 id 和图片锚点。
    # 归档是「首次铺底 + 每轮追加两条」，所以最后两条就是这一轮。
    total = persona.archive_len(key)
    out["n"] = {"user": max(0, total - 2), "assistant": max(0, total - 1)}
    out["day"] = persona.game_day(key)      # 游戏内第几天（2026-11-04）
    out["phase"] = persona.game_phase(key)
    out["finish"] = str(getattr(turn, "finish", "") or "")   # 这一轮有没有内射
    out["preg"] = persona.pregnancy_status(key)
    out["turns"] = persona.turn_total(key)  # 累计交流轮次（2026-11-06）
    # 攻略模式（2026-11-03）：把这一轮的结算带给前端——AI 回复下方的
    # 状态条直接用它渲染。自由模式 turn.aff 是 None，不带这个字段。
    if getattr(turn, "aff", None):
        out["affection"] = turn.aff
    return jsonify(out)


def _guest_chat(text, uid, token, manual=None):
    """游客的一轮。

    直接走 `persona.run_turn` 主引擎，和登录用户同一套打分/注入/归档；
    差别只剩三样：**一律自由模式**、固定学妹、30 轮上限（不能出图/删档）。
    """
    from app import guest

    _guest_sweep()                          # 顺手清过期线，量级很小
    ckey = _guest_line(uid)
    if ckey is None:
        return jsonify({"ok": False, "error": "没有可用角色"}), 500

    used = _guest_turns(ckey)
    if used >= guest.GUEST_TURNS:
        resp = jsonify({
            "ok": False,
            "error": "体验版玩完啦。登录后可以无限对话，还能让她出图。",
            "limit": True,
            "remaining": 0,
        })
        resp.status_code = 403
        _attach_guest_cookie(resp, token)
        return resp

    # 游客的用量单独归一类（guest_<uid前8>），和登录用户的 web_ 分开——
    # 体验版是「拉新成本」，得看清楚它值不值。run_turn 内部的两次调用
    # （打分 + 正文）都在这个 scope 里。
    with usage.scope("guest_" + str(uid or "anon")[:8]):
        turn = persona.run_turn(ckey, text, "private", "", manual=manual)
    if not turn.reply.strip():
        return jsonify({"ok": False, "error": "她这会儿有点卡"}), 502

    out = {
        "ok": True, "reply": turn.reply, "scene": turn.scene,
        "mood": turn.mood, "intensity": turn.intensity, "state": turn.state,
        "role": "guest",
        "remaining": max(0, guest.GUEST_TURNS - used - 1),
        "limit": guest.GUEST_TURNS,
    }
    total = persona.archive_len(ckey)
    out["n"] = {"user": max(0, total - 2), "assistant": max(0, total - 1)}
    out["day"] = persona.game_day(ckey)
    out["phase"] = persona.game_phase(ckey)
    out["finish"] = str(getattr(turn, "finish", "") or "")
    out["preg"] = persona.pregnancy_status(ckey)
    out["turns"] = _guest_turns(ckey)       # 累计交流轮次（2026-11-06）
    if getattr(turn, "aff", None):
        out["affection"] = turn.aff
    resp = jsonify(out)
    _attach_guest_cookie(resp, token)
    return resp


@bp.route("/api/persona/skip_day", methods=["POST"])
def skip_day_api():
    """时间跳跃（2026-11-04；2026-11-05 支持多天）：推进 days 天，**由她先开口**。

    body 可带 custom（自定义模型）——开场那一次 LLM 调用同样走它。
    """
    body = request.get_json(silent=True) or {}
    llm.set_custom_override(body.get("custom"))
    try:
        days = max(1, min(365, int(body.get("days") or 1)))
    except (TypeError, ValueError):
        days = 1
    try:
        role, who, token = _who()
        if role == "guest":
            from app import guest

            _guest_sweep()
            ckey = _guest_line(who)
            if ckey is None:
                return jsonify({"ok": False, "error": "没有可用角色"}), 500
            used = _guest_turns(ckey)
            if used >= guest.GUEST_TURNS:
                resp = jsonify({"ok": False, "limit": True, "remaining": 0,
                                "error": "体验版玩完啦"})
                resp.status_code = 403
                _attach_guest_cookie(resp, token)
                return resp
            with usage.scope("guest_" + str(who or "anon")[:8]):
                turn = persona.skip_day(ckey, "", days=days)
            if turn is None or not turn.reply.strip():
                return jsonify({"ok": False, "error": "没跳成，再试一次"}), 502
            out = {
                "ok": True, "reply": turn.reply, "scene": turn.scene,
                "mood": turn.mood, "intensity": turn.intensity,
                "state": turn.state, "role": "guest",
                "remaining": max(0, guest.GUEST_TURNS - used - 1),
                "limit": guest.GUEST_TURNS,
            }
            total = persona.archive_len(ckey)
            out["n"] = {"user": max(0, total - 2), "assistant": max(0, total - 1)}
            out["day"] = persona.game_day(ckey)
            out["phase"] = persona.game_phase(ckey)
            out["preg"] = persona.pregnancy_status(ckey)
            out["turns"] = _guest_turns(ckey)
            # 跨天会 `affection.sleep` 收场，把最新状态一起带回去。
            aff_now = _aff_status(ckey)
            if aff_now:
                out["affection"] = aff_now
            resp = jsonify(out)
            _attach_guest_cookie(resp, token)
            return resp

        key = _active_key(_key(who))
        card = persona.active_card(key)
        if card is None:
            return jsonify({"ok": False, "error": "还没选角色"}), 400
        with usage.scope(key):
            turn = persona.skip_day(key, "", days=days)
        if turn is None or not turn.reply.strip():
            return jsonify({"ok": False, "error": "没跳成，再试一次"}), 502
        out = {
            "ok": True, "reply": turn.reply, "scene": turn.scene,
            "mood": turn.mood, "intensity": turn.intensity,
            "state": turn.state, "role": "member",
        }
        total = persona.archive_len(key)
        out["n"] = {"user": max(0, total - 2), "assistant": max(0, total - 1)}
        if getattr(turn, "aff", None):
            out["affection"] = turn.aff
        out["day"] = persona.game_day(key)
        out["phase"] = persona.game_phase(key)
        out["preg"] = persona.pregnancy_status(key)
        out["turns"] = persona.turn_total(key)
        # 跨天会 `affection.sleep` 收场，把最新状态一起带回去。
        aff_now = _aff_status(key)
        if aff_now:
            out["affection"] = aff_now
        return jsonify(out)
    finally:
        llm.set_custom_override(None)


@bp.route("/api/persona/set_phase", methods=["POST"])
def set_phase_api():
    """切今天时段（2026-11-05）：早晨 / 中午 / 下午 / 晚上。

    不只是改状态：会让她先开口——把上一段收尾（亲密/正在做的事结束），
    再自然进入新时段。不推进天数、不掷事件、不结算怀孕。
    """
    body = request.get_json(silent=True) or {}
    llm.set_custom_override(body.get("custom"))
    phase = str(body.get("phase") or "").strip()
    try:
        if phase not in persona.PHASES:
            return jsonify({"ok": False,
                            "error": "时段只能是早晨/中午/下午/晚上"}), 400
        role, who, token = _who()
        if role == "guest":
            from app import guest

            _guest_sweep()
            ckey = _guest_line(who)
            if ckey is None:
                return jsonify({"ok": False, "error": "没有可用角色"}), 500
            used = _guest_turns(ckey)
            if used >= guest.GUEST_TURNS:
                resp = jsonify({"ok": False, "limit": True, "remaining": 0,
                                "error": "体验版玩完啦"})
                resp.status_code = 403
                _attach_guest_cookie(resp, token)
                return resp
            with usage.scope("guest_" + str(who or "anon")[:8]):
                turn = persona.jump_phase(ckey, phase, "")
            if turn is None or not turn.reply.strip():
                return jsonify({"ok": False, "error": "没切成，再试一次"}), 502
            out = {
                "ok": True, "reply": turn.reply, "scene": turn.scene,
                "mood": turn.mood, "intensity": turn.intensity,
                "state": turn.state, "role": "guest",
                "remaining": max(0, guest.GUEST_TURNS - used - 1),
                "limit": guest.GUEST_TURNS,
            }
            total = persona.archive_len(ckey)
            out["n"] = {"user": max(0, total - 2),
                        "assistant": max(0, total - 1)}
            out["day"] = persona.game_day(ckey)
            out["phase"] = persona.game_phase(ckey)
            out["preg"] = persona.pregnancy_status(ckey)
            out["turns"] = _guest_turns(ckey)
            resp = jsonify(out)
            _attach_guest_cookie(resp, token)
            return resp

        key = _active_key(_key(who))
        card = persona.active_card(key)
        if card is None:
            return jsonify({"ok": False, "error": "还没选角色"}), 400
        with usage.scope(key):
            turn = persona.jump_phase(key, phase, "")
        if turn is None or not turn.reply.strip():
            return jsonify({"ok": False, "error": "没切成，再试一次"}), 502
        out = {
            "ok": True, "reply": turn.reply, "scene": turn.scene,
            "mood": turn.mood, "intensity": turn.intensity,
            "state": turn.state, "role": "member",
        }
        total = persona.archive_len(key)
        out["n"] = {"user": max(0, total - 2), "assistant": max(0, total - 1)}
        if getattr(turn, "aff", None):
            out["affection"] = turn.aff
        # 时段跳转已经用 `affection.sleep` 把当前场景收掉；把最新的封存
        # 状态带回去，右栏才不会还挂着「进行中 / 高潮值」。
        aff_now = _aff_status(key)
        if aff_now:
            out["affection"] = aff_now
        out["day"] = persona.game_day(key)
        out["phase"] = persona.game_phase(key)
        out["preg"] = persona.pregnancy_status(key)
        out["turns"] = persona.turn_total(key)
        return jsonify(out)
    finally:
        llm.set_custom_override(None)


@bp.route("/api/persona/rollback", methods=["POST"])
def rollback_api():
    """回退上一轮（2026-11-04）：删掉她最后一条回复和你对应的那句话，
    好感/游戏日/心情退回这轮之前。只支持一级，没有参数。

    游客也放行（体验版发错消息同样要救），但**额度不退**——轮数已经
    烧掉了，回退不白嫖。前端拿到 ok 后整页重拉 session 刷新气泡和状态条。
    """
    role, who, token = _who()
    if role == "guest":
        from app import guest

        _guest_sweep()
        ckey = _guest_line(who)
        if ckey is None:
            return jsonify({"ok": False, "error": "没有可用角色"}), 500
        res = persona.rollback_last_turn(ckey)
        resp = jsonify(res)
        resp.status_code = 200 if res.get("ok") else 400
        _attach_guest_cookie(resp, token)
        return resp

    key = _active_key(_key(who))
    card = persona.active_card(key)
    if card is None:
        return jsonify({"ok": False, "error": "还没选角色"}), 400
    total = persona.archive_len(key)        # 回退前的行数：图映射按它算尾号
    res = persona.rollback_last_turn(key)
    if not res.get("ok"):
        return jsonify(res), 400
    _pic_map_drop_tail(key, int(res.get("dropped") or 0), total)
    # 前端成功后整页重拉 session（气泡、状态条、天数一次刷齐），
    # 这里的字段只是顺手带上：aff 是还原后的好感面板，day 是还原后的天数。
    return jsonify(res)


@bp.route("/api/persona/custom_test", methods=["POST"])
def custom_test():
    """「测试链接」（2026-11-05）：拿用户填的模型配置发一条 ping。

    body: {base_url, model, api_key}——就是弹窗里那三个框的现值，**不读
    localStorage 也不落盘**，测的是草稿。成功回 {ok, elapsed, info}（模型
    的回话），失败回 {ok:false, error}（错误原文，含 HTTP 状态和 detail）。
    """
    body = request.get_json(silent=True) or {}
    base_url = str(body.get("base_url") or "").strip()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not (base_url.startswith(("http://", "https://"))
            and model and api_key):
        return jsonify({"ok": False,
                        "error": "接口地址（http/https）、模型名、API Key 都要填"}), 400
    ok, elapsed, info = llm.test_custom(base_url, model, api_key)
    if not ok:
        return jsonify({"ok": False, "elapsed": elapsed, "error": info})
    return jsonify({"ok": True, "elapsed": elapsed, "info": info})


@bp.route("/api/persona/affection", methods=["POST"])
def affection_mode():
    """攻略模式开关。body: {mode: "raid" | "free"}

    同一条线在 QQ 端发「攻略模式 / 自由模式」是同一套状态（affection 模块），
    两边切来切去互不冲突。没在演谁时返回 400。
    """
    from app import affection
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in ("raid", "free"):
        return jsonify({"ok": False, "error": "mode 要是 raid 或 free"}), 400

    role, who, token = _who()
    if role == "guest":
        resp = jsonify({"ok": False, "error": "体验版没有攻略模式。"})
        resp.status_code = 403
        _attach_guest_cookie(resp, token)
        return resp

    key = _active_key(_key(who))
    reply = affection.set_mode(key, raid=(mode == "raid"))
    if reply is None:
        return jsonify({"ok": False, "error": "先选一个角色再开攻略模式"}), 400
    return jsonify({"ok": True, "reply": reply,
                    "affection": affection.status(key)})


@bp.route("/api/persona/affection/panel")
def affection_panel_api():
    """状态面板（2026-11-04 深夜）：网页右栏的整张竖排表。

    一次给全：身体进度七级、感情七档、冲动五档、高潮五档，
    每行带 `gate`（门槛）和 `state`（reached / current / locked）。
    前端不重算任何东西——档位表都在 config 里，前端算一遍就会两边不一致。

    游客攻略化（2026-11-05）：游客也有面板——体验版要展示的就是攻略玩法
    本身，藏着等于白改。
    """
    role, who, token = _who()
    if role == "guest":
        ckey = _guest_line(who)
        if ckey is None:
            resp = jsonify({"ok": False, "error": "没有可用角色"})
            resp.status_code = 500
            _attach_guest_cookie(resp, token)
            return resp
        p = _aff_panel(ckey)
        resp = jsonify({"ok": True, "panel": p})
        _attach_guest_cookie(resp, token)
        return resp
    key = _active_key(_key(who))
    p = _aff_panel(key)
    return jsonify({"ok": True, "panel": p})


@bp.route("/api/persona/affection/wipe", methods=["POST"])
@bp.route("/api/persona/affection/restart", methods=["POST"])
def affection_wipe():
    """「清空存档」：真删 —— 聊天记录 + 好感 + 成就 + 结局，角色卡保留。

    2026-11-04 用户定：这是不可逆操作，前端弹输入框要玩家手打
    「我同意清空存档」。后端照样校验一遍 —— 前端那道是防手滑，这道是
    防有人直接打接口绕过前端。

    老的 `/restart` 路径保留：玩家可能开着旧页面没刷新，点下去不该报 404。
    两条路径行为一致，都是真删。
    """
    role, who, token = _who()
    if role == "guest":
        resp = jsonify({"ok": False, "error": "体验版没有攻略模式。"})
        resp.status_code = 403
        _attach_guest_cookie(resp, token)
        return resp
    key = _active_key(_key(who))
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm") or "").strip() != persona.WIPE_CONFIRM:
        return jsonify({"ok": False, "error": "确认句不对，存档没有删。"}), 400
    reply = persona.wipe(key)
    if reply is None:
        return jsonify({"ok": False, "error": "现在没有在演谁，没什么可删的。"}), 400
    _pic_map_clear(key)          # 归档和窗口都删了，映射不删就会挂到新对话上
    return jsonify({"ok": True, "reply": reply})


@bp.route("/api/persona/affection/mercy", methods=["POST"])
def affection_mercy():
    """「再给我一次机会」（2026-11-04）：结局解除、好感往线内挪一格，每人 3 次。

    效果和「切自由模式再切回攻略」一样，只是它留在攻略模式里、并且扣次数。

    游客攻略化（2026-11-05）：游客也能用——进局子是攻略玩法的一部分，
    把出口对游客关上等于让人卡死在结局页面。它只是一次数值搬移，没有
    LLM 成本。
    """
    from app import affection
    role, who, token = _who()
    if role == "guest":
        ckey = _guest_line(who)
        if ckey is None:
            resp = jsonify({"ok": False, "error": "没有可用角色"})
            resp.status_code = 500
            _attach_guest_cookie(resp, token)
            return resp
        reply = affection.mercy(ckey)
        if reply is None:
            resp = jsonify({"ok": False, "error": "先选一个角色再开攻略模式"})
            resp.status_code = 400
            _attach_guest_cookie(resp, token)
            return resp
        resp = jsonify({"ok": True, "reply": reply,
                        "affection": affection.status(ckey)})
        _attach_guest_cookie(resp, token)
        return resp
    key = _active_key(_key(who))
    reply = affection.mercy(key)
    if reply is None:
        return jsonify({"ok": False, "error": "先选一个角色再开攻略模式"}), 400
    return jsonify({"ok": True, "reply": reply,
                    "affection": affection.status(key)})


def _img_rel(f):
    """磁盘相对名 → URL 里能用的相对路径（统一正斜杠）。

    2026-11-04 用户报「网页端出图没有显示成果」：图其实出好了，17:30 那次
    `image_jobs` 交回的是 `web\\生图页\\anima-line/Anima_00049_.png`（Windows
    反斜杠和正斜杠混着），拼成 `/api/image/web\\生图页\\...` 塞给 `<img src>`，
    浏览器/5175 网关那一段不认，一个取图请求都没发出来。
    落盘和读盘两头都过一遍，老映射里的反斜杠也能被纠正。
    """
    return "/".join(x for x in str(f or "").replace("\\", "/").split("/") if x)


@bp.route("/api/persona/shot", methods=["POST"])
def shot():
    """「出图」→ anima-line 单张横屏 CG（2026-11-04 换线，原 moss 五格漫画）。

    仍走 `image_jobs` 那条全局队列（`persona.draw_shot` → `image_jobs.enqueue`），
    不绕过队列的串行/水位控制。**后台线程**出图，这个接口立刻返回；完成情况
    写进 pic_map 的 `n<归档行号>` 条目：
    - 排队中：`{"pending": true}`
    - 成功：`{"file": 文件名}`
    - 失败：`{"error": 原因}`
    前端轮询 `/api/persona/shot_status?n=<行号>` 拿结果。

    body: {msgIndex?}
      - `msgIndex` 指定画哪一条回复：**归档行号**（每条回复下面的按钮传它）
      - 不传 = 画最后一轮
    """
    body = request.get_json(silent=True) or {}
    idx = body.get("msgIndex")
    if idx is not None:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "msgIndex 不是数字"}), 400

    role, who, _token = _who()
    if role == "guest":
        # 用户 2026-11-03 定：游客不能出图。出图占 GPU，是最贵的那一环。
        return jsonify({
            "ok": False,
            "error": "体验版不能出图。登录后可以让她画出来。",
            "limit": True,
        }), 403

    key = _active_key(_key(who))
    card = persona.active_card(key)
    if card is None:
        return jsonify({"ok": False, "error": "还没选角色"}), 400

    if idx is None:
        idx = max(0, persona.archive_len(key) - 1)

    # 先验画面：没有「画面：」行就直接拒绝（用户拍板不做 LLM 兜底翻译），
    # 别让后台线程白排一趟队。
    if not persona.shot_scene(key, idx):
        return jsonify({"ok": False,
                        "error": "这一轮没有可画的画面"}), 400

    slot = "n%d" % idx
    _pic_map_set(key, slot, {"pending": True})

    def _work():
        try:
            name, note = persona.draw_shot(card, key, msg_index=idx)
        except Exception:                          # noqa: BLE001
            log.exception("网页出图异常 %s", key)
            name, note = "", "出图这步出了岔子，再试一次"
        _pic_map_set(key, slot,
                     {"file": _img_rel(name)} if name
                     else {"error": note or "出图失败"})

    threading.Thread(target=_work, daemon=True, name="persona-shot").start()
    return jsonify({"ok": True, "msgIndex": idx,
                    "reply": "在画了，横屏单图，几十秒。"})


@bp.route("/api/persona/shot_status")
def shot_status():
    """轮询单张 CG 出完没有。query: n（归档行号）。

    status: none（没这张的记录）/ running / done（带 url）/ failed。
    """
    role, who, _token = _who()
    if role == "guest":
        return jsonify({"ok": True, "status": "none"})
    try:
        n = int(request.args.get("n", -1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "要数字 n"}), 400
    key = _active_key(_key(who))
    ent = (_pic_map_load().get(key) or {}).get("n%d" % max(0, n))
    if not ent:
        return jsonify({"ok": True, "status": "none"})
    if ent.get("pending"):
        return jsonify({"ok": True, "status": "running"})
    f = _img_rel(ent.get("file"))
    if f:
        return jsonify({"ok": True, "status": "done",
                        "url": "/api/image/" + f})
    return jsonify({"ok": True, "status": "failed",
                    "error": str(ent.get("error") or "出图失败")})



@bp.route("/api/persona/comics")
def comic_map():
    """当前会话线的「第几条回复 → 漫画第几页」映射。

    前端登录后拉一次，按映射逐条调 `/api/persona/comic` 恢复图。
    这是**服务器上的权威关联**——换设备、手机重登、清 localStorage 都在。
    """
    role, who, _token = _who()
    if role == "guest":
        # 游客不能出图，自然没有映射
        return jsonify({"ok": True, "pics": {}})
    key = _active_key(_key(who))
    # 只回 `n<行号>` 这一套（2026-11-04）。老的 `ai<窗口序号>` 已经对不上任何
    # 消息了，回给前端只会挂到错误的气泡上——用户定：老图直接作废。
    # 两种条目都回：老 moss 漫画 `{sid,page}`（前端走 /api/persona/comic 查）、
    # 新单张 CG `{file}`（前端直接用）。pending / error 不回——恢复时没意义。
    raw = (_pic_map_load().get(key) or {})
    pics = {k: v for k, v in raw.items()
            if k.startswith("n") and isinstance(v, dict)
            and (v.get("file") or (v.get("sid") and v.get("page")))}
    # 老映射里的 `file` 可能是 Windows 反斜杠（见 `_img_rel`），恢复时也纠正，
    # 否则刷新页面后图又「凭空消失」。
    for v in pics.values():
        if v.get("file"):
            v["file"] = _img_rel(v["file"])
    return jsonify({"ok": True, "pics": pics})


@bp.route("/api/persona/comic")
def comic_status():
    """查某一页出完没有。前端轮询这个，出完把图挂到对应回复**气泡的下方**。

    query: sid, page
    返回 status: pending / done / failed，done 时带 url。

    图片地址不在系列档案里，在**任务目录的 `state.json`**：
    系列页记 `task`（绝对路径），那下面每页有 `{status, file, error}`。
    见 `moss_api._load_state` 的写法。
    """
    import json
    import os
    from app import moss_api, moss_series
    sid = str(request.args.get("sid") or "").strip()
    page = str(request.args.get("page") or "").strip()
    if not sid or not page.isdigit():
        return jsonify({"ok": False, "error": "要 sid 和数字 page"}), 400

    series = moss_series.load(sid)
    if not series:
        return jsonify({"ok": False, "error": "这条线不在了"}), 404
    # owner 要和 `shot` 里建档时一致（`web_<qq>`）。传空串在私聊线上能查到，
    # 但网页系列的页 owner 是 `web_xxx`，用 "" 查会漏。
    owner = "web_" + (_qq() or "local")
    pg = (moss_series.by_n(series, int(page), owner=owner)
          or moss_series.by_n(series, int(page), owner=""))
    if not pg:
        return jsonify({"ok": False, "error": "没有这一页"}), 404

    task = str(pg.get("task") or "")
    if not task:
        return jsonify({"ok": True, "status": "pending", "sid": sid,
                        "page": int(page)})
    try:
        with open(os.path.join(task, "state.json"), encoding="utf-8") as fh:
            st = json.load(fh) or {}
    except Exception:                              # noqa: BLE001
        # 还没落盘 = 正在画
        return jsonify({"ok": True, "status": "pending", "sid": sid,
                        "page": int(page)})

    # ⚠️ 2026-11-03：这里**不能用系列页号**去查 `state.json`。
    #
    # 每个系列页有自己的任务目录（`moss_comic/20261103_1459_xxx/`），
    # 而每次渲染只出一页，所以那个目录的 `state.json` 里页号**永远是 "1"**。
    # 拿系列第 2 页的 2 去查，永远查不到 → 前端一直认为 pending
    # （用户报「网页端出图有问题」就是这个）。
    #
    # 正确做法：任务目录里只有一页，直接取它的唯一记录。
    pages_map = st.get("pages") or {}
    rec = pages_map.get("1") or {}
    if not rec and pages_map:
        # 兜底：万一哪天一目录多页，取最后一条
        rec = pages_map[sorted(pages_map.keys())[-1]]
    status = rec.get("status") or "pending"

    # 僵尸 running（2026-11-03）：进程被重启后，`state.json` 会永远停在
    # `running`——出图线程死了，没人回写结果。前端会一直轮询到超时。
    #
    # 判据：状态是 running，但 `updated` 已经超过 12 分钟。单张图实测
    # 100~180 秒，12 分钟还没动静一定是死了。
    if status == "running":
        try:
            upd = str(st.get("updated") or "")
            if upd:
                import time as _t
                ts = _t.mktime(_t.strptime(upd, "%Y-%m-%d %H:%M:%S"))
                if _t.time() - ts > 12 * 60:
                    log.warning("[web] 出图卡死超 12 分钟，判失败：%s", task)
                    status = "failed"
                    rec = dict(rec)
                    rec["error"] = "出图中途断了（进程重启），重出一次"
        except Exception:                          # noqa: BLE001
            pass

    out = {"ok": True, "status": status, "sid": sid, "page": int(page)}
    if status == "done":
        rel = str(rec.get("file") or "")
        out["file"] = rel
        # 走已有的出图接口取文件（它只认 state.json 里记过的相对路径）。
        # `n` 也要传 "1"——那是任务目录内的页号，不是系列页号。
        out["url"] = ("/api/moss/img?task=%s&n=1"
                      % (request.args.get("task") or _rel_task(task)))
    elif status == "failed":
        out["error"] = rec.get("error") or "出图失败"
    return jsonify(out)


def _rel_task(task):
    """任务绝对路径 → `/api/moss/img` 认的那个相对值。

    `moss_api._task_dir()` 只认 `state/moss_comic` 下的目录名，所以这里取
    末段目录名。取不出来就回空串（前端会显示「查不到」而不是炸掉）。
    """
    import os
    return os.path.basename(str(task or "").rstrip("\\/"))


# ─── 聊天背景（2026-11-05）─────────────────────────────────
#
# 三种来源的优先级在 `chat_bg.resolve()` 里：账号 > 这张卡 > 全局默认 >
# 内置图。游客不参与账号层——体验版没有稳定身份，存了也会随浏览器丢；
# 所以游客只看卡/全局默认，上传接口直接 403。

def _bg_card_id(role, who):
    """当前这条会话演的是哪张卡；没有就返回空。"""
    if role == "guest":
        ckey = _guest_line(who)
        ent = persona._entry(ckey) if ckey else {}
    else:
        key = _active_key(_key(who))
        ent = persona._entry(key) or {}
    if not isinstance(ent, dict):
        return GUEST_CARD_ID if role == "guest" else ""
    cid = str(ent.get("card") or "").strip()
    return cid or (GUEST_CARD_ID if role == "guest" else "")


@bp.route("/api/persona/background")
def get_background():
    """当前账号应该用哪张背景：账号自己的 > 卡的默认 > 全局默认。"""
    role, who, token = _who()
    guest = role == "guest"
    bg = chat_bg.resolve("" if guest else who, _bg_card_id(role, who),
                         allow_user=not guest)
    resp = jsonify({
        "ok": True,
        "role": role,
        "editable": not guest,
        **bg,
    })
    if guest:
        _attach_guest_cookie(resp, token)
    return resp


@bp.route("/api/persona/background", methods=["POST"])
def upload_background():
    """账号换一张自己的背景。游客 403。"""
    role, who, token = _who()
    if role == "guest":
        resp = jsonify({"ok": False, "limit": True,
                        "error": "体验版不能换背景，登录后可以。"})
        resp.status_code = 403
        _attach_guest_cookie(resp, token)
        return resp
    try:
        bg = chat_bg.save(request.files.get("file"), "user", who)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "source": "user", **bg})


@bp.route("/api/persona/background/reset", methods=["POST"])
def reset_background():
    """删掉账号自己的背景，回落到这张卡/全局默认。"""
    role, who, token = _who()
    if role == "guest":
        resp = jsonify({"ok": False, "limit": True,
                        "error": "体验版不能换背景，登录后可以。"})
        resp.status_code = 403
        _attach_guest_cookie(resp, token)
        return resp
    chat_bg.delete("user", who)
    bg = chat_bg.resolve(who, _bg_card_id(role, who), allow_user=True)
    return jsonify({"ok": True, **bg})


@bp.route("/api/persona/background/file")
def background_file():
    """背景图本身。账号层只认当前身份，不能拿别人的 hash 来读。"""
    kind = str(request.args.get("kind") or "default").strip().lower()
    ident = str(request.args.get("id") or "").strip()
    thumb = str(request.args.get("thumb") or "") in ("1", "true", "yes")
    if kind == "user":
        role, who, token = _who()
        if role == "guest":
            resp = jsonify({"ok": False, "error": "没有背景"})
            resp.status_code = 403
            _attach_guest_cookie(resp, token)
            return resp
        ident = who
    p = chat_bg.path_for(kind, ident, thumb=thumb)
    if not p or not os.path.isfile(p):
        return jsonify({"ok": False, "error": "没有背景"}), 404
    return send_file(p, conditional=True, max_age=3600)


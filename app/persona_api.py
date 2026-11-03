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

from flask import Blueprint, jsonify, request

from app import friend_auth, persona, usage

log = logging.getLogger("persona_api")

bp = Blueprint("persona_api", __name__)

# 游客身份 cookie（2026-11-03）。和登录 cookie 分开：游客换浏览器会重来，
# 这是已知且接受的——体验版是为了让人尝到甜头，不是防盗版。
GUEST_COOKIE = "gs_id"
GUEST_COOKIE_TTL = 30 * 24 * 3600      # cookie 活 30 天（对话数据只保 24 小时）

# 游客进来先演谁。游客不选角色（省一步），直接给这张卡。
# 想换就改这一个字符串（卡的 id）。
GUEST_CARD_ID = "izumi_sagiri"

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
    """可选角色列表：网页左栏用。"""
    out = []
    for c in persona.load_cards():
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


@bp.route("/api/persona/session")
def get_session():
    """这条会话现在是什么状态：演的谁、聊了几轮、她此刻的样子。"""
    role, who, token = _who()

    if role == "guest":
        return _guest_session(who, token)

    key = _active_key(_key(who))
    ent = persona._entry(key) or {}                       # noqa: SLF001
    card = _card_by_id(ent.get("card"))
    hist = persona.trim(persona._load_hist(key))          # noqa: SLF001

    # 按原顺序回填，助手那条要拆出情绪/状态
    msgs = []
    for m in hist:
        if m.get("role") == "user":
            msgs.append({"role": "user", "text": m.get("content") or ""})
        else:
            reply, scene, mood, intensity, state = persona.split_turn(
                m.get("content") or "")
            if reply.strip():
                msgs.append({"role": "assistant", "text": reply,
                             "scene": scene, "mood": mood,
                             "intensity": intensity, "state": state})
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
        "state": str(ent.get("state") or ""),
        "mood": str(ent.get("mood") or ""),
        "intensity": int(ent.get("intensity") or 0),
        "turns": int(ent.get("turns") or 0),
        "messages": msgs,
        "affection": aff,
        "role": "member",
    })


def _aff_status(key):
    """这条线的好感状态（给网页状态条）；没开过攻略返回 None。"""
    try:
        from app import affection
        return affection.status(key)
    except Exception:                                     # noqa: BLE001
        return None


def _guest_session(uid, token):
    """游客的会话状态：固定一个角色，历史从游客目录读。"""
    from app import guest
    card = _card_by_id(GUEST_CARD_ID)
    if card is None:
        cards = persona.load_cards()
        card = cards[0] if cards else None

    msgs = []
    for m in guest.load_hist(uid):
        if m.get("role") == "user":
            msgs.append({"role": "user", "text": m.get("content") or ""})
        else:
            reply, scene, mood, intensity, state = persona.split_turn(
                m.get("content") or "")
            if reply.strip():
                msgs.append({"role": "assistant", "text": reply,
                             "mood": mood, "intensity": intensity,
                             "state": state})
    resp = jsonify({
        "ok": True,
        "card": _card_json(card),
        "note": "",
        "state": "",
        "mood": "",
        "intensity": 0,
        "turns": guest.turns_used(uid),
        "messages": msgs,
        "role": "guest",
        "remaining": guest.remaining(uid),
        "limit": guest.GUEST_TURNS,
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
        # 游客固定演一张卡（`GUEST_CARD_ID`）。让他换人等于把体验版
        # 变成免费全集，而且换人后历史还得跟着分桶，没必要。
        resp = jsonify({
            "ok": False,
            "error": "体验版只能玩这一个角色。登录后可以随便换。",
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
    return jsonify({"ok": True, "reply": persona.clear(key)})


@bp.route("/api/persona/chat", methods=["POST"])
def chat():
    """一轮对话。body: {text}

    登录用户 → 无限；游客 → 15 轮（按 AI 回复次数算），超了硬拒绝。
    """
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "说点什么"}), 400

    role, who, token = _who()

    if role == "guest":
        return _guest_chat(text, who, token)

    key = _active_key(_key(who))
    card = persona.active_card(key)
    if card is None:
        return jsonify({"ok": False, "error": "还没选角色"}), 400

    # 用量归属（2026-11-03）：网页端的扮演调用以前没打标签，全落进 usage 的
    # "other" 兜底，和跑图台、管理页的调用混在一起——「网页端扮演烧了多少
    # token」永远统计不出来。QQ 侧是在 qq_bot 的轮次外层包的，这里补同一层，
    # 标签用活动线键（web_<qq>__<card>），能精确到「哪个角色烧了多少」。
    with usage.scope(key):
        turn = persona.run_turn(key, text, "private", "")
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
    # 攻略模式（2026-11-03）：把这一轮的结算带给前端——AI 回复下方的
    # 状态条直接用它渲染。自由模式 turn.aff 是 None，不带这个字段。
    if getattr(turn, "aff", None):
        out["affection"] = turn.aff
    return jsonify(out)


def _guest_chat(text, uid, token):
    """游客的一轮。用 `persona` 的引擎，但历史存到游客自己的目录。"""
    from app import guest
    from app import persona as P

    used = guest.turns_used(uid)
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

    card = P.find_card(GUEST_CARD_ID)
    if card is None:
        # 找不到默认卡就退回第一张
        cards = P.load_cards()
        card = cards[0] if cards else None
    if card is None:
        return jsonify({"ok": False, "error": "没有可用角色"}), 500

    # 用游客自己的历史（不走 persona 那套存储）
    hist = guest.load_hist(uid)[-P.HISTORY_LIMIT:]
    head = P.build_head(card, "guest", "", "", "", intimate=True)
    msgs = ([{"role": "system", "content": head}] + hist
            + [{"role": "user", "content": text}])
    try:
        # 游客的用量单独归一类（guest_<uid前8>），和登录用户的 web_ 分开——
        # 体验版是「拉新成本」，得看清楚它值不值。
        with usage.scope("guest_" + str(uid or "anon")[:8]):
            raw = P.llm.call_llm(msgs)
    except Exception:                              # noqa: BLE001
        log.exception("游客对话调用失败 %s", uid[:8])
        return jsonify({"ok": False, "error": "她这会儿有点卡"}), 502

    reply, scene, mood, intensity, state = P.split_turn(raw or "")
    if not reply.strip():
        return jsonify({"ok": False, "error": "她这轮没说话"}), 502

    guest.append_hist(uid, [
        {"role": "user", "content": text},
        {"role": "assistant",
         "content": P._ensure_meta(raw, scene, mood, intensity, state)},
    ])

    resp = jsonify({
        "ok": True, "reply": reply, "scene": scene,
        "mood": mood, "intensity": intensity, "state": state,
        "role": "guest",
        "remaining": max(0, guest.GUEST_TURNS - used - 1),
    })
    _attach_guest_cookie(resp, token)
    return resp


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


@bp.route("/api/persona/affection/wipe", methods=["POST"])
@bp.route("/api/persona/affection/restart", methods=["POST"])
def affection_wipe():
    """「清空存档」：真删 —— 聊天记录 + 好感 + 成就 + 结局，角色卡保留。

    2026-11-04 用户定：这是不可逆操作，前端弹输入框要玩家手打
    「我同意删除该存档」。后端照样校验一遍 —— 前端那道是防手滑，这道是
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
    return jsonify({"ok": True, "reply": reply})


@bp.route("/api/persona/affection/mercy", methods=["POST"])
def affection_mercy():
    """「再给我一次机会」（2026-11-04）：结局解除、好感往线内挪一格，每人 3 次。

    效果和「切自由模式再切回攻略」一样，只是它留在攻略模式里、并且扣次数。
    """
    from app import affection
    role, who, token = _who()
    if role == "guest":
        resp = jsonify({"ok": False, "error": "体验版没有攻略模式。"})
        resp.status_code = 403
        _attach_guest_cookie(resp, token)
        return resp
    key = _active_key(_key(who))
    reply = affection.mercy(key)
    if reply is None:
        return jsonify({"ok": False, "error": "先选一个角色再开攻略模式"}), 400
    return jsonify({"ok": True, "reply": reply,
                    "affection": affection.status(key)})


@bp.route("/api/persona/shot", methods=["POST"])
def shot():
    """「出图」→ 交给 moss 漫画出五格。

    **走的是 `image_jobs` 那条全局队列**（`moss_bot.start` → `moss_api._draw`
    → `image_jobs.enqueue`），和 QQ 端完全同源。网页不能自己开一条出图路径，
    否则会绕过队列的串行/水位控制，把 ComfyUI 打爆（用户点名要求）。

    body: {msgIndex?, extra?}
      - `msgIndex` 指定画第几条 assistant 回复（网页每条回复下面的按钮传它）
      - 不传 = 画最后一轮（QQ 端「出图」的行为）
      - `extra` 额外剧情，可选
    """
    from app import moss_bot, moss_series
    body = request.get_json(silent=True) or {}
    extra = str(body.get("extra") or "").strip()
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

    brief = persona.comic_brief(card, key, extra=extra, msg_index=idx)

    # 网页会话在 moss 里的身份：`("private", "web_<qq>)"`。
    #
    # 不能用字面量 `"web"`（第一版就是这么写的，后果见下）：
    #   1. `_say()` 会拿它当 QQ 号去发消息 → 每次出图都刷「moss 回话失败」
    #   2. `series_mod.current("private", "web")` 是**全局共享**的，所有用户
    #      的图会串进同一条线
    web_tid = "web_" + (_qq() or "local")

    # 起跑前记下这条线的最新页号，等一下要等「**比它大的**页号出现」。
    # ⚠️ 2026-11-03：旧版等的是「系列有页」——老系列秒返回旧页号，前端把
    # 旧图挂在新气泡下，新画的那页永远不显示（17:35 实测：雷姆线连点两次
    # 出图，都返回第 4 页，显示的是 16:28 的旧图）。
    pre = 0
    pre_sid = moss_series.current("private", web_tid)
    if pre_sid:
        pre = int((moss_series.latest(moss_series.load(pre_sid) or {}, "")
                   or {}).get("n") or 0)

    try:
        # 网页模式：出图照跑（走同一条 `image_jobs` 队列），但不往 QQ 发任何
        # 东西。回执由这个 HTTP 响应自己带。
        with moss_bot.web_mode():
            took, note = moss_bot.start("private", web_tid, "append",
                                        brief, sender_uin=_qq())
    except Exception as exc:                       # noqa: BLE001
        log.exception("网页出图失败 %s", key)
        return jsonify({"ok": False, "error": str(exc)}), 500
    if not took:
        return jsonify({"ok": False, "error": note or "排队中"}), 429

    # 等新页建档（编剧 2~6 秒 LLM 调用后才 add_page，渲染要更久不用等）。
    sid, page = pre_sid or "", 0
    for _ in range(180):
        time.sleep(0.25)
        sid = moss_series.current("private", web_tid)
        series = moss_series.load(sid) if sid else None
        page = int((moss_series.latest(series or {}, "") or {}).get("n") or 0)
        if sid and page > pre:
            log.info("[web] 新页落盘：%s 第 %s 页（起跑前 %s）", sid, page, pre)
            break
    else:
        # 两种可能：编剧太慢 / 这条线上一张还在画（_do_append 里 _take
        # 失败，线程静默退出——网页模式没处说话）。都按失败回，前端弹提示。
        log.warning("[web] 等新页落盘超时（45s）：%s（前值 %s）", web_tid, pre)
        return jsonify({
            "ok": False,
            "error": "上一张可能还在画，等它出来再点一次出图",
        }), 504

    # 存「回复 ↔ 页」映射，重登/换设备按这个恢复（见 /api/persona/comics）。
    if idx is not None:
        _pic_map_set(key, "ai%d" % idx, {"sid": sid, "page": page})
    return jsonify({"ok": True, "sid": sid, "page": page,
                    "msgIndex": idx,
                    "reply": "在画了，一页五格，大概一两分钟。"})


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
    return jsonify({"ok": True, "pics": _pic_map_load().get(key) or {}})


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

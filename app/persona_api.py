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
import logging

from flask import Blueprint, jsonify, request

from app import friend_auth, persona

log = logging.getLogger("persona_api")

bp = Blueprint("persona_api", __name__)


def _qq():
    """取当前用户。跟 `gallery_api._qq()` 同一套口径：query 或 body 里带。"""
    qq = str(request.args.get("qq") or "").strip()
    if not qq:
        body = request.get_json(silent=True) or {}
        qq = str(body.get("qq") or "").strip()
    if qq and friend_auth.QQ_RE.match(qq):
        return qq
    return ""


def _key(qq):
    """网页会话键。**和 QQ 的 `private_<qq>` 分开**，两边各聊各的。

    不隔离的话，网页上说一句、QQ 上接着说，会变成同一个上下文，
    用户会看到「她怎么突然记得我在另一头说过的话」。
    """
    return "web_" + (qq or "local")


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
    key = _key(_qq())
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
    return jsonify({
        "ok": True,
        "card": _card_json(card),
        "note": str(ent.get("note") or ""),
        "state": str(ent.get("state") or ""),
        "mood": str(ent.get("mood") or ""),
        "intensity": int(ent.get("intensity") or 0),
        "turns": int(ent.get("turns") or 0),
        "messages": msgs,
    })


@bp.route("/api/persona/switch", methods=["POST"])
def switch():
    """切角色。body: {name 或 id}"""
    body = request.get_json(silent=True) or {}
    key = _key(_qq())
    want = str(body.get("id") or body.get("name") or "").strip()
    if not want:
        return jsonify({"ok": False, "error": "没说演谁"}), 400
    card = persona.find_card(want)
    if card is None:
        return jsonify({"ok": False, "error": "没有这个角色：%s" % want}), 404
    persona.switch(key, card)
    return jsonify({"ok": True, "card": _card_json(card),
                    "reply": "从现在起我是%s。" % card["name"]})


@bp.route("/api/persona/leave", methods=["POST"])
def leave():
    """「出戏」：退出扮演，保留历史。"""
    key = _key(_qq())
    if not persona.active_card(key):
        return jsonify({"ok": True, "reply": "本来就没在演。"})
    persona.leave(key)
    return jsonify({"ok": True, "reply": "出戏了。"})


@bp.route("/api/persona/clear", methods=["POST"])
def clear():
    """清空记忆，保留角色。"""
    key = _key(_qq())
    return jsonify({"ok": True, "reply": persona.clear(key)})


@bp.route("/api/persona/chat", methods=["POST"])
def chat():
    """一轮对话。body: {text}"""
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "说点什么"}), 400
    key = _key(_qq())
    card = persona.active_card(key)
    if card is None:
        return jsonify({"ok": False, "error": "还没选角色"}), 400

    turn = persona.run_turn(key, text, "private", "")
    if not turn.reply.strip():
        return jsonify({"ok": False, "error": "她这轮没说话"}), 502
    return jsonify({
        "ok": True,
        "reply": turn.reply,
        "scene": turn.scene,
        "mood": turn.mood,
        "intensity": turn.intensity,
        "state": turn.state,
    })


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

    key = _key(_qq())
    card = persona.active_card(key)
    if card is None:
        return jsonify({"ok": False, "error": "还没选角色"}), 400

    brief = persona.comic_brief(card, key, extra=extra, msg_index=idx)
    try:
        took, note = moss_bot.start("private", _qq() or "web", "append",
                                    brief, sender_uin=_qq())
    except Exception as exc:                       # noqa: BLE001
        log.exception("网页出图失败 %s", key)
        return jsonify({"ok": False, "error": str(exc)}), 500
    if not took:
        return jsonify({"ok": False, "error": note or "排队中"}), 429

    sid = moss_series.current("private", _qq() or "web")
    series = moss_series.load(sid) if sid else None
    newest = moss_series.latest(series or {}, "") or {}
    return jsonify({"ok": True, "sid": sid, "page": newest.get("n") or 0,
                    "msgIndex": idx,
                    "reply": "在画了，一页五格，大概一两分钟。"})


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
    pg = moss_series.by_n(series, int(page), owner="")
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

    rec = (st.get("pages") or {}).get(str(page)) or {}
    status = rec.get("status") or "pending"
    out = {"ok": True, "status": status, "sid": sid, "page": int(page)}
    if status == "done":
        rel = str(rec.get("file") or "")
        out["file"] = rel
        # 走已有的出图接口取文件（它只认 state.json 里记过的相对路径）
        out["url"] = ("/api/moss/img?task=%s&n=%s"
                      % (request.args.get("task") or _rel_task(task), page))
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

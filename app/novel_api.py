"""小说模式的 HTTP 接口（2026-11-11）。

只挂 5174，站主自用；`_public_source_guard` / `_tunnel_guard` 会挡住公网来源。
这一层只做「解析请求 → 调 novel → 包成 JSON」，世界书与裁定全在 `app/novel.py`。

三种错误分开说：
- 参数不合法 → 400，说清楚缺什么；
- 模型没交作业（空返回 / JSON 解析失败）→ 502，提示再点一次；
- 链路不通 → 502，提示等一下再试，不叫玩家重写一遍。
"""
import logging

from flask import Blueprint, jsonify, request

from app import novel

log = logging.getLogger("novel_api")

bp = Blueprint("novel_api", __name__)


def _fail(e):
    if isinstance(e, novel.NoHomework):
        return jsonify({"ok": False, "error": "这一轮 AI 没交作业，再点一次",
                        "detail": str(e)}), 502
    if isinstance(e, ValueError):
        return jsonify({"ok": False, "error": str(e)}), 400
    log.exception("[novel] 调用失败")
    return jsonify({"ok": False, "error": "模型链路暂时不通，等一下再试",
                    "detail": str(e)[:200]}), 502


@bp.route("/api/novel/plays")
def plays():
    """玩法列表。第一版只有「丧尸末日」，以后加玩法就是多放一包预设。"""
    return jsonify({"ok": True, "plays": novel.list_plays()})


@bp.route("/api/novel/worlds")
def worlds():
    """已有的世界（一个世界一条线）。"""
    return jsonify({"ok": True, "worlds": novel.list_worlds()})


@bp.route("/api/novel/state")
def state():
    wid = str(request.args.get("id") or "").strip()
    w = novel.get_world(wid)
    if not w:
        return jsonify({"ok": False, "error": "没有这个世界"}), 404
    return jsonify({"ok": True, "state": novel.state_card(w)})


@bp.route("/api/novel/open", methods=["POST"])
def open_world():
    """开场封样。body: {play, name, identity}"""
    body = request.get_json(silent=True) or {}
    try:
        out = novel.open_world(body.get("play"), body.get("name"),
                               body.get("identity"))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)
    return jsonify(out)


@bp.route("/api/novel/turn", methods=["POST"])
def turn():
    """一轮。body: {id, text} —— text 是玩家的意图，不是既成事实。"""
    body = request.get_json(silent=True) or {}
    try:
        out = novel.run_turn(body.get("id"), body.get("text"))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)
    return jsonify(out)


@bp.route("/api/novel/restart", methods=["POST"])
def restart():
    """死亡重开。body: {id, name?, identity?}，不传就沿用原来的名字和身份。"""
    body = request.get_json(silent=True) or {}
    try:
        out = novel.restart(body.get("id"), body.get("name"),
                            body.get("identity"))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)
    return jsonify(out)

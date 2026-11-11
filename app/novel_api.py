"""小说模式的 HTTP 接口（2026-11-11，2026-11-12 补自定义模型与二次确认）。

只挂 5174，站主自用；`_public_source_guard` / `_tunnel_guard` 会挡住公网来源。
这一层只做「解析请求 → 调 novel → 包成 JSON」，世界书与裁定全在 `app/novel.py`。

三种错误分开说：
- 参数不合法 → 400，说清楚缺什么；
- 模型没交作业（空返回 / JSON 解析失败）→ 502，提示再点一次；
- 链路不通 → 502，提示等一下再试，不叫玩家重写一遍。

## 自定义模型为什么只在「真的配了」时挂覆盖（2026-11-12）

`llm.set_custom_override([])` 会把 `persona_chain_active()` 打开，`candidates()`
随即把链尾换成「玩家自带模型 + 代充付费」，**不再接 `LLM_FALLBACK_CHAIN`**。
站主没配自定义模型时链尾就是空的，中转站一 402 就无处可降——正好把
`c728d4a`「跟 1v1 一致，放开降级链」修好的问题又弄回去。所以：
配了才挂，没配就让 `novel._call(strict=False)` 照旧顺全局链降级。
"""
import json
import logging

from flask import Blueprint, g, jsonify, request

from app import llm, novel
from app.config import PERSONA_MODEL, PERSONA_PROVIDER

log = logging.getLogger("novel_api")

bp = Blueprint("novel_api", __name__)

# 「重开一本」的手打确认句。前端弹窗照这句让玩家原样打一遍，后端再校验一次
# （防直接打接口绕过）。和 persona.WIPE_CONFIRM 同一套做法，不共用常量：
# 小说模式删的是一整本周目进度，文案得说清楚是哪件事。
RESTART_CONFIRM = "我同意重开这一本"


def _novel_customs(body):
    """玩家自带模型列表，跟 persona_api._persona_customs 同款。

    新前端传 `custom_models`（数组顺序 = 降级顺序）；老前端传 `custom`
    （单条 dict）。没带字段返回 `[]`。
    """
    raw = body.get("custom_models")
    if isinstance(raw, list):
        return raw
    one = body.get("custom")
    return [one] if isinstance(one, dict) and one else []


@bp.before_request
def _begin_custom_override():
    """自定义模型挂在请求线程上，整本小说这一轮所有 LLM 调用都吃得到。"""
    customs = _novel_customs(request.get_json(silent=True) or {})
    g.novel_has_custom = bool(customs)
    if customs:
        try:
            llm.set_custom_override(customs)
        except Exception:                                   # noqa: BLE001
            log.warning("[novel] 自定义模型覆盖挂载失败", exc_info=True)
            g.novel_has_custom = False


@bp.teardown_request
def _end_custom_override(exc):
    try:
        llm.set_custom_override(None)
    except Exception:                                       # noqa: BLE001
        pass


@bp.after_request
def _attach_model_used(resp):
    """所有回包带上「这一轮到底是哪个模型回的话」，供页面那行显示。

    挂了覆盖时读 `llm.persona_last_model()`（精确）；没挂时不能读——那个值是
    线程本地的，会残留上一个请求的。此时只报链头这个**事实**，并写明失败会
    顺链降级，不假装知道最终落在谁身上。
    """
    used = (llm.persona_last_model()
            if getattr(g, "novel_has_custom", False) else None)
    if used is None:
        used = {"kind": "chain_head",
                "name": PERSONA_MODEL or "默认链头",
                "label": "链头（失败会顺链降级）"}
    if resp.is_json:
        try:
            d = resp.get_json() or {}
            if isinstance(d, dict):
                d["model_used"] = used
                d["restart_confirm"] = RESTART_CONFIRM
                resp.set_data(json.dumps(d, ensure_ascii=False))
        except Exception:                                   # noqa: BLE001
            pass                                    # 只是展示字段，坏了别拦回包
    return resp


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
    return jsonify({"ok": True, "plays": novel.list_plays(),
                    "chain_head": {"provider": PERSONA_PROVIDER or "",
                                   "model": PERSONA_MODEL or ""}})


@bp.route("/api/novel/worlds")
def worlds():
    """已有的世界（一个世界一条线）。"""
    return jsonify({"ok": True, "worlds": novel.list_worlds()})


@bp.route("/api/novel/state")
def state():
    """打开一本世界。老世界顺手把 history 排进 RAG 回填，接着写就能检索到。"""
    wid = str(request.args.get("id") or "").strip()
    w = novel.get_world(wid)
    if not w:
        return jsonify({"ok": False, "error": "没有这个世界"}), 404
    novel.warm_world(w)
    return jsonify({"ok": True, "state": novel.state_card(w)})


@bp.route("/api/novel/open", methods=["POST"])
def open_world():
    """开场封样。body: {play, name, identity, world_id?}

    带 world_id 时走 novel.open_world 的幂等分支：已经封过样就直接返回，
    不再跑一次 LLM，身份也不会被改掉。
    """
    body = request.get_json(silent=True) or {}
    try:
        out = novel.open_world(body.get("play"), body.get("name"),
                               body.get("identity"),
                               body.get("world_id") or body.get("id"))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)
    return jsonify(out)


@bp.route("/api/novel/rename", methods=["POST"])
def rename():
    """主角名字随时可改；身份不在这里动（要改身份只能重开一本）。"""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(novel.rename(body.get("id") or body.get("world_id"),
                                    body.get("name")))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)


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
    """死亡重开。body: {id, confirm, name?, identity?}

    confirm 必须是玩家手打的那句，前后端各校验一次；不传就沿用原名和身份。
    """
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm") or "").strip() != RESTART_CONFIRM:
        return jsonify({"ok": False,
                        "error": "重开要先把确认句原样打一遍"}), 400
    try:
        out = novel.restart(body.get("id"), body.get("name"),
                            body.get("identity"))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)
    return jsonify(out)


@bp.route("/api/novel/quota")
def quota():
    """查额度：只报当前事实，不编一个不存在的次数账本。

    小说模式现在只在 5174 站主自用，走站主自己的链头，没有「站长给的次数」
    这回事。等搬去 5175 群友态，唯一要改的就是这条路由：认登录 QQ、读对应的
    群友额度账本返回 `limited / remaining`，前端那颗按钮不用动。
    """
    return jsonify({"ok": True, "mode": "owner", "limited": False,
                    "remaining": None,
                    "text": "站主自用 · 走自己的链头，没有次数额度"})


@bp.route("/api/novel/custom_test", methods=["POST"])
def custom_test():
    """「测试连接」：拿弹窗草稿发一条 ping，不读 localStorage、不落盘。"""
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

"""小说模式的 HTTP 接口（2026-11-11 建，2026-11-12 上架 5175）。

这一层只做「认身份 → 定链策略 → 调 novel → 包成 JSON」，世界书与裁定全在
`app/novel.py`。服务层不 import flask、不认 QQ，只吃 `owner` / `opts` 两个参数。

三种错误分开说：
- 参数不合法 / 额度不足 → 400，说清楚缺什么；
- 模型没交作业（空返回 / JSON 解析失败）→ 502，提示再点一次；
- 链路不通 → 502，提示等一下再试，不叫玩家重写一遍。

## 群友身份（2026-11-12）

5175 的 `_forward()` 带 `X-Friend-Token` 并把登录 Cookie 解出的 QQ 写死塞进
请求，客户端自己伪造没用。照 `comic_quota.friend_qq()` 那套认人：
认到 QQ = 群友，世界书严格按 owner 隔离；站主直连 5174 认不到 = owner ""。
**`main._tunnel_guard` 的白名单故意不加 `/api/novel/`**：万一隧道错指到
5174，带 CF 头的请求在那儿会被 404 挡住，不会绕开 5175 的 QQ 注入变成站主态。

## 群友计费（照漫画台，用户 2026-11-12 拍板）

免费层 = 公用中转站（PERSONA_PROVIDER/MODEL），`strict=True` 只打这一家，
成功就免费不限次、不扣任何额度；**绝不静默接全局降级链**——链尾第一项就是
付费官方，一接上等于绕过次数闸门白烧站主余额（comic_api 里的原话）。
中转站挂了才扣 1 次 `script` 代充次数、改走站主官方。一轮两次 LLM（编剧 +
写作）只算一次作业，跟「一次剧本生成 = 1 次」同一个口径。

## 自定义模型为什么只在「真的配了」时挂覆盖（2026-11-12）

`llm.set_custom_override([])` 会把 `persona_chain_active()` 打开，`candidates()`
随即把链尾换成「玩家自带模型 + 代充付费」，**不再接 `LLM_FALLBACK_CHAIN`**。
站主没配自定义模型时链尾就是空的，中转站一 402 就无处可降——正好把
`c728d4a`「跟 1v1 一致，放开降级链」修好的问题又弄回去。所以：
配了才挂，没配就让 `novel._call(strict=False)` 照旧顺全局链降级。
群友态一律不挂：自带 API Key 不该明文经过站主服务器，也不该绕开计费。
"""
import json
import logging

from flask import Blueprint, g, jsonify, request

from app import comic_quota, config as _cfg, llm, novel
from app.config import PERSONA_MODEL, PERSONA_PROVIDER

log = logging.getLogger("novel_api")

bp = Blueprint("novel_api", __name__)

# 「重开一本」的手打确认句。前端弹窗照这句让玩家原样打一遍，后端再校验一次
# （防直接打接口绕过）。和 persona.WIPE_CONFIRM 同一套做法，不共用常量：
# 小说模式删的是一整本周目进度，文案得说清楚是哪件事。
RESTART_CONFIRM = "我同意重开这一本"

# 代充层：provider/model 都空 → llm.candidates() 从全局降级链头开始。
# 免费层的参数是每次请求现算的（见 _relay_opts），别在 import 时定死，
# 否则测试 patch config 换链头不生效。
_PAID_OPTS = {"provider": None, "model": None, "strict": False}


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


def _qq():
    """当前请求的群友 QQ；站主直连返回 ""。"""
    try:
        return getattr(g, "novel_qq", "") or ""
    except RuntimeError:                    # 没有请求上下文（CLI / 后台线程）
        return ""


@bp.before_request
def _begin_request():
    """认身份 + 挂自定义模型覆盖，两件事放一起，不依赖 before_request 顺序。"""
    try:
        g.novel_qq = comic_quota.friend_qq()
    except Exception:                                       # noqa: BLE001
        log.warning("[novel] 群友身份识别失败，按站主处理", exc_info=True)
        g.novel_qq = ""
    g.novel_paid = False
    # 群友一律不用自定义模型：Key 不能过站主服务器，也不能绕开计费。
    customs = [] if g.novel_qq else _novel_customs(
        request.get_json(silent=True) or {})
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

    站主挂了覆盖时读 `llm.persona_last_model()`（精确）；没挂时不能读——那个
    值是线程本地的，会残留上一个请求的，此时只报链头这个**事实**。
    群友态不挂覆盖，但可以准确报「免费层 / 代充」，因为链是接口层定的。
    """
    used = (llm.persona_last_model()
            if getattr(g, "novel_has_custom", False) else None)
    if used is None:
        qq = _qq()
        if qq and getattr(g, "novel_paid", False):
            used = {"kind": "paid", "name": "站主代充余额",
                    "label": "中转站没跑成，这一轮用站主额度"}
        elif qq:
            used = {"kind": "public", "name": "公用中转站（免费）",
                    "label": "公用 V4 Pro · 免费层"}
        else:
            used = {"kind": "chain_head",
                    "name": PERSONA_MODEL or "默认链头",
                    "label": "链头（失败会顺链降级）"}
    if resp.is_json:
        try:
            d = resp.get_json() or {}
            if isinstance(d, dict):
                d["model_used"] = used
                d["restart_confirm"] = RESTART_CONFIRM
                d["mode"] = "friend" if _qq() else "owner"
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


def _relay_opts():
    """免费层：只打公用中转站，失败抛出、绝不顺链降级。"""
    return {"provider": (_cfg.PERSONA_PROVIDER or "").strip() or None,
            "model": (_cfg.PERSONA_MODEL or "").strip() or None,
            "strict": True}


def _friend_run(qq, run):
    """群友整轮作业：`run(opts)` → 结果。

    先免费层跑整轮；链路异常才扣 1 次代充、换全局链整轮重跑。整轮重跑是
    安全的：`novel.run_turn` 在写作 AI 抛异常时还没落盘（`_apply` 只改内存
    副本），同一个 `turn_token` 也不会被判成重复。
    「模型没交作业」「参数不合法」不是链路故障，原样抛、不白扣次数。
    """
    if not qq:
        return run(None)                    # 站主：链策略不变
    free = _relay_opts()
    if free["provider"]:
        try:
            return run(free)
        except (novel.NoHomework, ValueError):
            raise
        except Exception as e:                              # noqa: BLE001
            log.warning("[novel] 群友免费层没跑成，试代充：%s", e)
    if comic_quota.remaining(qq, "script") <= 0:
        raise ValueError("中转站暂时跑不了，免费的这一轮用不了；"
                         "找站长加次数，就能用站主额度继续写")
    if not comic_quota.consume(qq, "script"):
        raise ValueError("代充次数刚好用完，找站长加次数吧")
    g.novel_paid = True
    try:
        return run(_PAID_OPTS)
    except Exception:
        comic_quota.refund(qq, "script")
        raise


@bp.route("/api/novel/plays")
def plays():
    """玩法列表。第一版只有「丧尸末日」，以后加玩法就是多放一包预设。"""
    return jsonify({"ok": True, "plays": novel.list_plays(),
                    "chain_head": {"provider": PERSONA_PROVIDER or "",
                                   "model": PERSONA_MODEL or ""}})


@bp.route("/api/novel/worlds")
def worlds():
    """已有的世界（一个世界一条线）。只列当前身份自己的。"""
    return jsonify({"ok": True, "worlds": novel.list_worlds(_qq())})


@bp.route("/api/novel/state")
def state():
    """打开一本世界。老世界顺手把 history 排进 RAG 回填，接着写就能检索到。"""
    wid = str(request.args.get("id") or "").strip()
    w = novel.get_world(wid, _qq())
    if not w:
        return jsonify({"ok": False, "error": "没有这个世界"}), 404
    novel.warm_world(w)
    return jsonify({"ok": True, "state": novel.state_card(w)})


@bp.route("/api/novel/book")
def book():
    """世界书全貌，只读。世界一旦生成就不再改设定，这里只是看。"""
    wid = str(request.args.get("id") or "").strip()
    w = novel.get_world(wid, _qq())
    if not w:
        return jsonify({"ok": False, "error": "没有这个世界"}), 404
    return jsonify({"ok": True, "book": novel.world_book(w)})


@bp.route("/api/novel/open", methods=["POST"])
def open_world():
    """开场封样。body: {play, name, identity, world_id?, table?}

    带 world_id 时走 novel.open_world 的幂等分支：已经封过样就直接返回，
    不再跑一次 LLM，身份也不会被改掉。
    `table` 非空 = 自建玩法：玩家填的那张表，AI 现生成一份世界观内嵌进
    这本世界书，不进 novels/ 目录（那里是全局的，不认 owner）。
    """
    body = request.get_json(silent=True) or {}
    qq = _qq()
    table = body.get("table")
    try:
        return jsonify(_friend_run(qq, lambda o: novel.open_world(
            body.get("play"), body.get("name"), body.get("identity"),
            body.get("world_id") or body.get("id"), owner=qq, opts=o,
            table=table if isinstance(table, dict) else None)))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)


@bp.route("/api/novel/rename", methods=["POST"])
def rename():
    """主角名字随时可改；身份不在这里动（要改身份只能重开一本）。"""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(novel.rename(body.get("id") or body.get("world_id"),
                                    body.get("name"), owner=_qq()))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)


@bp.route("/api/novel/turn", methods=["POST"])
def turn():
    """一轮。body: {id, text, turn_token?} —— text 是玩家的意图，不是既成事实。

    `turn_token` 由前端每轮生成一个，网关超时后重发同一个 token 会拿到上一轮
    结果，不重跑模型、不重复扣次数。
    """
    body = request.get_json(silent=True) or {}
    qq = _qq()
    try:
        return jsonify(_friend_run(qq, lambda o: novel.run_turn(
            body.get("id"), body.get("text"), owner=qq, opts=o,
            turn_token=body.get("turn_token"))))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)


@bp.route("/api/novel/rollback", methods=["POST"])
def rollback():
    """回退最近一轮。body: {id}

    不跑模型、不扣次数（没有 LLM 调用），所以不走 `_friend_run`；只校验归属。
    只给一次：回退后世界书的回退点被清空，前端那颗按钮随即变灰，要再回退
    必须先往下写一轮。
    """
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(novel.rollback(body.get("id") or body.get("world_id"),
                                      owner=_qq()))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)


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
        return jsonify(novel.restart(body.get("id"), body.get("name"),
                                    body.get("identity"), owner=_qq()))
    except Exception as e:                              # noqa: BLE001
        return _fail(e)


@bp.route("/api/novel/quota")
def quota():
    """查额度。站主自用没有次数账本；群友读漫画台那份账号额度。

    前端那颗按钮和文案在两种模式下都不用改：这里把 `limited / remaining /
    text` 按事实给全就行。免费层成功不扣次数，所以「剩 0 次」不等于不能写，
    文案必须说清楚「只有中转站挂了才用次数」。
    """
    qq = _qq()
    if not qq:
        return jsonify({"ok": True, "mode": "owner", "limited": False,
                        "remaining": None,
                        "text": "站主自用 · 走自己的链头，没有次数额度"})
    left = comic_quota.snapshot(qq)["left"].get("script", 0)
    return jsonify({
        "ok": True, "mode": "friend", "limited": left <= 0,
        "remaining": left,
        "text": ("公用中转站在线时免费随便写；挂了才用站长给的次数"
                 "（代充剩 %d 次）" % left),
    })


@bp.route("/api/novel/custom_test", methods=["POST"])
def custom_test():
    """「测试连接」：拿弹窗草稿发一条 ping，不读 localStorage、不落盘。

    群友态关掉：自定义 API Key 不该明文经过站主服务器，也不该绕开计费；
    前端在群友态会把这颗入口整个藏起来，这里是服务端那一半。
    """
    if _qq():
        return jsonify({"ok": False, "error": "自定义模型是站主自用功能"}), 403
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

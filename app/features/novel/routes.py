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

## 站主的链：跟 1v1 一模一样，**永不接全局降级链**（2026-11-15 拍板）

`llm.set_custom_override([])` 会把 `persona_chain_active()` 打开，`candidates()`
随即把链尾换成「玩家自带模型 + 代充付费」，**不再接 `LLM_FALLBACK_CHAIN`**。

2026-11-12 曾经反着写过：只有真配了自定义模型才挂覆盖，没配就顺全局链降级，
理由是「站主没配时链尾空，中转站一 402 无处可降」（`c728d4a`）。那条路的代价
是**站主一跑小说就烧官方付费**——全局链第一项就是 `deepseek:deepseek-flash`。
2026-11-15 用户重新拍板：小说模式要和 1v1 完全一致，中转站挂了就只剩
「自己配的自定义模型 / 代充付费段」，**站主不替它掏官方那份钱**。
⇒ 站主态**无条件**挂覆盖（空列表也挂），链尾爱空就空，报错交给前端引导。

2026-11-28 群友态也挂上了（用户拍板放开）：Key 仍然只存在玩家自己的浏览器，
随请求临时进来、`teardown_request` 立刻摘掉、不落盘不记日志；计费闸门不动——
自带模型那一层不扣次数，只有落到代充才扣（见 `_friend_run` 的三段）。
"""
import json
import logging
import re

from flask import Blueprint, g, jsonify, request
from urllib.parse import urlsplit

from app.features.comic import quota as comic_quota
from app.integrations import llm
from app.features.novel import novel
from app import config as _cfg
from app.config import PERSONA_MODEL, PERSONA_PROVIDER

log = logging.getLogger("novel_api")

bp = Blueprint("novel_api", __name__)

# 「重开一本」的手打确认句。前端弹窗照这句让玩家原样打一遍，后端再校验一次
# （防直接打接口绕过）。和 persona.WIPE_CONFIRM 同一套做法，不共用常量：
# 小说模式删的是一整本周目进度，文案得说清楚是哪件事。
RESTART_CONFIRM = "我同意重开这一本"

# 「删掉这本书」的手打确认句。粒度就是整本：设定、主角、正文、状态一起没，
# 没有「只删一部分」的选项，也没有回收站。比重开更狠，所以文案和按钮都单独
# 一套（红色 danger），不跟重开共用。
DELETE_CONFIRM = "我确认删掉这本书"

# 代充层：provider/model 都空 → llm.candidates() 从全局降级链头开始。
# 免费层的参数是每次请求现算的（见 _relay_opts），别在 import 时定死，
# 否则测试 patch config 换链头不生效。
_PAID_OPTS = {"provider": None, "model": None, "strict": False}

# 「测试连接」的内网闸（2026-11-28）：放开给群友后，这个接口等于让服务器
# 代发一次 POST，不挡内网就是个现成的 SSRF 探针。
_PRIVATE_HOST = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|10\."
    r"|192\.168\.|169\.254\."
    r"|172\.(1[6-9]|2\d|3[01])\."
    r"|::1|\[::1\])", re.I)


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
    # 自定义模型**谁都能挂**（2026-11-28 用户拍板放开群友态）。
    # 旧口径是「群友一律不用」：Key 不能过站主服务器、也不能绕开计费。放开
    # 的理由是页面那半边——5175 的 /novel 强制登录，群友态把整颗「自定义模型」
    # 按钮 display:none 掉，群友打开小说页只看得到「查询额度」，以为自己没得选。
    # Key 的处理方式不变：只存在玩家自己的浏览器 localStorage，每轮随请求临时
    # 进来，`teardown_request` 立刻摘掉，不落盘、不写日志；站主进程只在内存里
    # 转一手就还给他。计费闸门也不动，见 `_friend_run`：自带模型不扣次数。
    customs = _novel_customs(request.get_json(silent=True) or {})
    g.novel_has_custom = bool(customs)
    # **无条件**挂覆盖，空列表也挂（2026-11-15 用户拍板）。
    # 只有挂了 `persona_chain_active()` 才会是 True，candidates() 的链尾
    # 才会换成「自定义模型 + 代充付费段」；不挂就退回 LLM_FALLBACK_CHAIN，
    # 那条链第一项过后就是付费官方 —— 小说不该烧那个钱。
    # 链尾可能因此是空的（没配自定义、也没代充），中转站一挂直接报错，
    # 由 `_fail` 给「去配自定义模型 / 找站长加次数」的引导。
    # paid_after=True：小说里玩家自己掏钱的模型永远排在付费段之前先试。
    try:
        llm.set_custom_override(customs, paid_after=True)
    except Exception:                                       # noqa: BLE001
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
                    "label": "公用中转站（本站免费层）"}
    if resp.is_json:
        try:
            d = resp.get_json() or {}
            if isinstance(d, dict):
                d["model_used"] = used
                d["restart_confirm"] = RESTART_CONFIRM
                d["delete_confirm"] = DELETE_CONFIRM
                d["mode"] = "friend" if _qq() else "owner"
                resp.set_data(json.dumps(d, ensure_ascii=False))
        except Exception:                                   # noqa: BLE001
            pass                                    # 只是展示字段，坏了别拦回包
    return resp


def _fail(e):
    if isinstance(e, novel.NoHomework):
        # 2026-11-12 真机事故：这一支原来一行日志都不打。用户连撞三次 502，
        # 只能靠「有没有 [cache] 用量行」反推是「上游 200 但正文为空」还是
        # 「JSON 被砍断」——两者在日志里长得一模一样，白白丢掉现场。
        # detail 本身已带原文长度与结尾几个字（见 novel._parse_json），记下来
        # 下次就能直接分辨，不用再猜。
        try:
            where = request.path
        except Exception:                           # noqa: BLE001
            where = "?"                             # 单测里没有请求上下文
        log.warning("[novel] 没交作业 %s：%s", where, e)
        return jsonify({"ok": False, "error": "这一轮 AI 没交作业，再点一次",
                        "detail": str(e)}), 502
    if isinstance(e, ValueError):
        return jsonify({"ok": False, "error": str(e)}), 400
    if isinstance(e, llm.PersonaChainExhausted):
        # 站主态才会走到这里：链尾被 `_begin_request` 换成了「自定义模型 +
        # 代充付费段」，中转站一挂就是真的没路了。以前这里会悄悄顺全局链
        # 落到付费官方，2026-11-15 起不再这样做，所以提示要给出**下一步动作**，
        # 不能只说「等一下再试」——等多久都不会自己好。
        log.warning("[novel] 链路走空（站主态，未接全局链）：%s", e)
        return jsonify({"ok": False,
                        "error": "公用中转站连不上了，也没有配自定义模型。"
                                 "在「自定义模型」里填一个你自己的 API 就能继续，"
                                 "或者找站长加代充次数",
                        "detail": str(e)[:200]}), 502
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

    三段（2026-11-28 加了中间那段「自带模型」）：

    1. 免费层：只打公用中转站，成功不扣次数；
    2. 自带模型：群友自己填的那几条（Key 在**他自己浏览器**里，随请求临时
       进来）。链尾已经在 `_begin_request` 挂好，这里把链头让开（provider=None）
       就只剩他的模型——成功即返回，**不扣次数**、不碰站主余额；这一层是
       「中转站挂了也能自己接着写」，不是代充的替代品，所以排在免费层之后。
    3. 代充：扣 1 次 `script`，走站主官方那条全局链。**必须先把覆盖摘掉**：
       挂着覆盖时 `candidates()` 只会给出群友自己的模型，代充会变成空跑。

    整轮重跑是安全的：`novel.run_turn` 在写作 AI 抛异常时还没落盘（`_apply`
    只改内存副本），同一个 `turn_token` 也不会被判成重复。
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
            log.warning("[novel] 群友免费层没跑成：%s", e)
    # 第二段：群友自带模型（不扣次数）
    if getattr(g, "novel_has_custom", False):
        try:
            return run({"provider": None, "model": None, "strict": False})
        except (novel.NoHomework, ValueError):
            raise
        except Exception as e:                              # noqa: BLE001
            log.warning("[novel] 群友自带模型也没跑成，试代充：%s", e)
    if comic_quota.remaining(qq, "script") <= 0:
        raise ValueError("中转站和你的自定义模型都没跑成，这一轮用不了；"
                         "填一个你自己的 API（「自定义模型」里），"
                         "或者找站长加次数")
    if not comic_quota.consume(qq, "script"):
        raise ValueError("代充次数刚好用完，找站长加次数吧")
    g.novel_paid = True
    # 代充走全局链：先摘覆盖，跑完在 teardown 里不会再挂回来。
    llm.set_custom_override(None)
    try:
        return run(_PAID_OPTS)
    except Exception:
        comic_quota.refund(qq, "script")
        raise


@bp.route("/api/novel/plays")
def plays():
    """玩法列表：官方（磁盘预设）+ 这个人自己的（从自己的书里反推）。

    自建玩法不落磁盘，所以左栏那两组数据都是这一次现算的：别人的书不会
    出现在你的「我的玩法」里，删掉最后一本，那个玩法条目也就没了。
    """
    return jsonify({"ok": True, "plays": novel.list_plays(_qq()),
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


@bp.route("/api/novel/delete", methods=["POST"])
def delete():
    """删掉一整本。body: {id, confirm}

    粒度就是这本书：设定、主角、所有正文、人物名录、走过的路一起没，没有
    「只删设定」或「只删进度」的选项，也没有回收站。confirm 必须是把确认句
    原样打一遍，前后端各校验一次（防直接打接口绕过）——这是全项目唯一一个
    真正不可恢复的动作，比「重开」还狠。
    """
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm") or "").strip() != DELETE_CONFIRM:
        return jsonify({"ok": False,
                        "error": "删掉要把确认句原样打一遍"}), 400
    try:
        return jsonify(novel.delete_world(body.get("id") or body.get("world_id"),
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
    # 用量（2026-11-12）：原来站主态只回一句固定文案，什么都看不出来。
    # 现在把进程内的累计调用数和 token 估算一起带上。
    try:
        _u = novel.usage_snapshot()
    except Exception:                                     # noqa: BLE001
        _u = {"tokens": 0, "calls": 0}
    if not qq:
        return jsonify({"ok": True, "mode": "owner", "limited": False,
                        "remaining": None, "usage": _u,
                        # 用量交给 `usage` 字段渲染（2026-11-28 修）：这里再拼一遍
                        # `_usage_txt`，前端又 append 一次，页面上会重复出现
                        # 「本次运行还没写过」两遍。
                        "text": "站主自用 · 走自己的链头，没有次数额度"})
    left = comic_quota.snapshot(qq)["left"].get("script", 0)
    return jsonify({
        "ok": True, "mode": "friend", "limited": left <= 0,
        "remaining": left,
        "text": ("公用中转站在线时免费随便写；它挂了先用你自己配的模型"
                 "（「自定义模型」里填，不扣次数），都没跑成才用站长给的次数"
                 "（代充剩 %d 次）" % left),
    })


@bp.route("/api/novel/custom_test", methods=["POST"])
def custom_test():
    """「测试连接」：拿弹窗草稿发一条 ping，不读 localStorage、不落盘。

    2026-11-28 群友也能用（跟着自定义模型一起放开）：他自己填的 Key、他自己
    付钱，没理由不给测。加了一条内网闸——这个接口本质是「服务器替玩家发一次
    POST」，放开就等于送一个 SSRF 探针，所以 127./10./192.168./172.16-31./
    localhost 一律拒掉。
    """
    body = request.get_json(silent=True) or {}
    base_url = str(body.get("base_url") or "").strip()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not (base_url.startswith(("http://", "https://"))
            and model and api_key):
        return jsonify({"ok": False,
                        "error": "接口地址（http/https）、模型名、API Key 都要填"}), 400
    host = (urlsplit(base_url).hostname or "").strip().lower()
    if not host or _PRIVATE_HOST.match(host):
        return jsonify({"ok": False,
                        "error": "内网 / 本机地址不能测，填你模型的公网地址"}), 400
    ok, elapsed, info = llm.test_custom(base_url, model, api_key)
    if not ok:
        return jsonify({"ok": False, "elapsed": elapsed, "error": info})
    return jsonify({"ok": True, "elapsed": elapsed, "info": info})


# ─── 管理页（只读）──────────────────────────────────
#
# 5175 把 `/api/novel/<path>` 整个通配反代给了群友，所以这几个接口必须自己
# 挡一次身份，不能指望「管理页没挂到网关」——接口只要能被 POST 到就等于开着。

def _owner_only():
    """群友态一律 403；站主直连（qq 为空）才继续。"""
    if _qq():
        return jsonify({"ok": False, "error": "小说记录只有站主能看"}), 403
    return None


@bp.route("/api/novel/admin/list")
def admin_list():
    """所有世界书（不分 owner），给管理页列「谁写了什么」。"""
    blocked = _owner_only()
    if blocked is not None:
        return blocked
    return jsonify({"ok": True, "worlds": novel.all_worlds()})


@bp.route("/api/novel/admin/book")
def admin_book():
    """管理页读一本：正文按轮次摆好 + 世界书全貌。只读，不改任何东西。"""
    blocked = _owner_only()
    if blocked is not None:
        return blocked
    w = novel.admin_world(request.args.get("id"))
    if not w:
        return jsonify({"ok": False, "error": "没有这个世界"}), 404
    return jsonify({"ok": True, "story": novel.admin_story(w),
                    "book": novel.world_book(w)})

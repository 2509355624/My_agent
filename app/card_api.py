"""星陨AI 角色卡工作台：一句话 → 六项文案 + 封面（2026-10-19）。

    GET  /card                页面（web/card_studio.html）
    POST /api/card/draft      一句话描述 → 一张卡（同步，5 秒左右）
    POST /api/card/save       存卡（id 空 = 新建）
    POST /api/card/delete     删卡
    POST /api/card/cover      出一张封面（后台线程，2~4 分钟）
    GET  /api/card/status     轮询封面进度
    GET  /api/card/img        取封面图
    GET  /api/card/plain      导出纯文本

## 为什么出图走后台线程而不是 `image_jobs` 队列

`image_jobs` 是给 QQ / 生图页那条链路用的（它有 worker、有跨进程锁、要发回
会话、要过审核）。角色卡封面是**网页侧的一次性动作**，只需要「出一张、落盘、
前端能看见进度」，起个线程写状态最直接——和 `comic_api` 的 portrait 那条线
一个路子。真正的并发保护在 `comic._submit` 里（排队 + 跨进程锁），这里不管。
"""

import logging
import os
import threading
import urllib.parse

from flask import Blueprint, Response, jsonify, request, send_file, send_from_directory

from app import card_maker
from app.config import WEB_DIR

log = logging.getLogger("card_api")


# ─── 谁能创建/修改角色（2026-11-03 用户要求「游客无法创建角色」）─────
#
# 现状：角色卡的接口全在 5174，而公网只映射 5175 的网关，`/api/card/*`
# 根本没转发出去——游客天然碰不到。
#
# 但「天然没暴露」不等于「有保障」：万一以后有人图省事往 5175 加了转发，
# 游客就能建卡、甚至改内置卡。所以这里加一道**显式**判断：
#
#   · 本地直连 5174（没有 `X-Friend-Token`）→ 放行。那是用户的自己人场景，
#     管理页本来就不对外。
#   · 经 5175 网关转发（带 `X-Friend-Token`）→ 必须是站主本人。
#     游客没有 qq，登录的普通群友也不是站主，都会被挡住。
#
# 不引入新概念：判据就是网关那只"这个请求是从外网来的"的标记 + 站主 QQ。

def _via_gateway():
    """这个请求是不是从 5175 网关转发来的。"""
    return bool(request.headers.get("X-Friend-Token"))


def _is_owner():
    """站主本人？（`GALLERY_OWNER_QQ` 没配就等于没有站主）"""
    from app.config import GALLERY_OWNER_QQ
    if not GALLERY_OWNER_QQ:
        return False
    qq = str(request.args.get("qq") or "").strip()
    if not qq:
        body = request.get_json(silent=True) or {}
        qq = str(body.get("qq") or "").strip()
    return qq == str(GALLERY_OWNER_QQ)


def _may_write():
    """能不能建卡/改卡/删卡。返回 `(ok, 原因)`。"""
    if not _via_gateway():
        return True, ""
    if _is_owner():
        return True, ""
    return False, "游客和普通用户不能创建或修改角色。这是站主的权限。"


bp = Blueprint("card", __name__)

# 正在出封面的卡号。同一张卡连点两下只跑一份，别让两张图抢同一个落盘路径。
_RUNNING = set()
_LOCK = threading.Lock()


def _brief_limit():
    return 2000


@bp.route("/api/card/draft", methods=["POST"])
def api_card_draft():
    """一句话 → 六个模块的文案。慢在这里（一次 LLM 调用），所以同步等着。"""
    data = request.get_json(silent=True) or {}
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "先写一句角色描述"}), 400
    if len(brief) > _brief_limit():
        return jsonify({"ok": False,
                        "error": "描述太长了（%d 字），控制在 %d 字以内"
                                 % (len(brief), _brief_limit())}), 400
    try:
        card = card_maker.draft(brief,
                                provider=data.get("provider") or None,
                                model=data.get("model") or None)
    except ValueError as e:
        # 「模型返回空 / 不是 JSON」——它没交作业，让用户重跑，别猜别兜底
        return jsonify({"ok": False, "error": str(e)}), 502
    except Exception as e:
        log.warning("角色卡生成失败：%s", e)
        return jsonify({"ok": False, "error": "生成失败：%s" % e}), 502
    card["brief"] = brief
    return jsonify({"ok": True, "card": card,
                    "warnings": card_maker.check(card)})


@bp.route("/api/card/list")
def api_card_list():
    items = card_maker.load_cards()
    items = sorted(items, key=lambda c: c.get("updated") or "", reverse=True)
    return jsonify({"ok": True, "cards": items})


@bp.route("/api/card/save", methods=["POST"])
def api_card_save():
    ok, why = _may_write()
    if not ok:
        return jsonify({"ok": False, "error": why}), 403
    data = request.get_json(silent=True) or {}
    card = data.get("card") or {}
    if not isinstance(card, dict):
        return jsonify({"ok": False, "error": "card 格式不对"}), 400
    cid = data.get("id") or card.get("id") or ""
    # 内置卡不许改（2026-11-03 用户要求）。`card_maker.save_card` 会拒绝并
    # 返回 None，这里翻译成人话——不然前端只会看到「存好了」但内容没变。
    if card_maker.is_builtin(cid or card.get("id")):
        return jsonify({"ok": False,
                        "error": "这是内置角色，不能修改。想改就另存一份新的。"}), 403
    rec = card_maker.save_card(card, cid)
    if rec is None:
        return jsonify({"ok": False, "error": "这张卡不能改"}), 403
    return jsonify({"ok": True, "card": rec, "warnings": card_maker.check(rec)})


@bp.route("/api/card/delete", methods=["POST"])
def api_card_delete():
    ok, why = _may_write()
    if not ok:
        return jsonify({"ok": False, "error": why}), 403
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": "缺 id"}), 400
    if card_maker.is_builtin(cid):
        return jsonify({"ok": False,
                        "error": "这是内置角色，不能删除。"}), 403
    with _LOCK:
        running = cid in _RUNNING
    if running:
        return jsonify({"ok": False, "error": "这张卡正在出封面，等它跑完再删"}), 409
    return jsonify({"ok": True, "deleted": card_maker.delete_card(cid)})


def _cover_worker(cid, card):
    try:
        card_maker.render_cover(cid, card)
        card_maker.update_card(cid, {"cover_state": "done", "cover_error": ""})
    except Exception as e:
        log.warning("角色卡封面失败（%s）：%s", cid, e)
        card_maker.update_card(cid, {"cover_state": "error",
                                     "cover_error": str(e)[:300]})
    finally:
        with _LOCK:
            _RUNNING.discard(cid)


@bp.route("/api/card/cover", methods=["POST"])
def api_card_cover():
    """出一张封面。卡必须先存过（图要落到它自己的目录里）。"""
    ok, why = _may_write()
    if not ok:
        return jsonify({"ok": False, "error": why}), 403
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id") or "").strip()
    card = card_maker.get_card(cid) or data.get("card") or {}
    if not cid:
        return jsonify({"ok": False, "error": "先保存这张卡再出图"}), 400
    if not str((card or {}).get("cover") or "").strip():
        return jsonify({"ok": False,
                        "error": "这张卡没有封面标签，先在上面点「重新生成」"}), 400
    with _LOCK:
        if cid in _RUNNING:
            return jsonify({"ok": True, "state": "rendering", "already": True})
        _RUNNING.add(cid)
    card_maker.update_card(cid, {"cover_state": "rendering", "cover_error": ""})
    threading.Thread(target=_cover_worker, args=(cid, dict(card)),
                     daemon=True, name="card-cover").start()
    return jsonify({"ok": True, "state": "rendering"})


@bp.route("/api/card/status")
def api_card_status():
    cid = (request.args.get("id") or "").strip()
    card = card_maker.get_card(cid)
    if not card:
        return jsonify({"ok": False, "error": "没有这张卡"}), 404
    fp = card_maker.cover_path(cid)
    state = str(card.get("cover_state") or "")
    if not state:
        state = "done" if os.path.isfile(fp) else "idle"
    return jsonify({"ok": True, "state": state,
                    "error": card.get("cover_error") or "",
                    "has_cover": os.path.isfile(fp),
                    "cover_url": ("/api/card/img?id=%s&t=%s"
                                  % (cid, os.path.getmtime(fp)))
                                 if os.path.isfile(fp) else "",
                    "card": card})


@bp.route("/api/card/img")
def api_card_img():
    cid = (request.args.get("id") or "").strip()
    fp = card_maker.cover_path(cid)
    if not cid or not os.path.isfile(fp):
        return jsonify({"ok": False, "error": "还没有封面"}), 404
    return send_file(fp, conditional=True)


@bp.route("/api/card/plain")
def api_card_plain():
    cid = (request.args.get("id") or "").strip()
    card = card_maker.get_card(cid)
    if not card:
        return jsonify({"ok": False, "error": "没有这张卡"}), 404
    from flask import Response
    body = card_maker.plain(card)
    fname = "%s.txt" % (str(card.get("name") or cid).replace("/", "_"))
    return Response(body, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename*=UTF-8''%s"
                             % urllib.parse.quote(fname)})


@bp.route("/card")
def card_page():
    return send_from_directory(WEB_DIR, "card_studio.html")

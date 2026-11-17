"""生图二审 / 旧图迁移的管理接口（Blueprint）。"""
import re
import json
import os
import logging
import base64
import ipaddress
import socket
import time
import requests
from flask import (Flask, request, jsonify, send_from_directory, send_file,
                   Response, stream_with_context)
from app.core import agents
from app.core import agents as agent_store
from app.core import cancel as cancel_mod
from app.features.image import comfy_out
from app.features.image import audit as image_audit
from app.features.image import migrate as image_migrate
from app.core import logsetup
from app.core import usage as usage_stats
from app.features.qq import bill as qq_bill
# 别名 `vision_mod` 是**必须**的：本文件里已有一个叫 `vision` 的路由函数
# （POST /api/vision，网页端「让 AI 看图」），模块名直接被它盖掉，
# 写成 `from app.integrations import vision` 会在调用处炸 AttributeError:
# 'function' object has no attribute 'default_prompt'。
from app.integrations import vision as vision_mod
from app.features.comic import routes as comic_api
from app.features.comic import moss_api
from app.features.image import routes as gen_api
from app.features.qq import friend_api
from app.features.qq import friend_site
from app.features.gallery import routes as gallery_api
from app.features.card import routes as card_api
from app.features.image import output_api
from app.features.image import outfit_api
from app.features.novel import routes as novel_api
from app.config import (AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR,
                        PROVIDERS, OLLAMA_BASE_URL, DEFAULT_AGENT_ID,
                        ADMIN_ALLOW_REMOTE, ADMIN_ALLOW_REMOTE_LAN,
                        CONTEXT_BUDGET, IMAGE_AUDIT_PROMPT_MAX,
                        QQ_PRIVATE_ENABLE, QQ_WHITELIST_USERS,
                        VISION_PROMPT_MAX, VISION_PROVIDER, VISION_MODEL,
                        QQ_AGENT_ID, provider_vision,
                        CUSTOM_PROVIDERS_FILE, load_custom_providers,
                        COMFYUI_OUTPUT_DIR)
from app.core.config.llm import get_llm_settings, update_llm_settings
from app.core.config import llm as llm_config
from app.core.config.runtime import runtime_path
from app.core import provider_store
from app.core.skills import list_skills, load_skill
from app.core import model_catalog
from app.features.chat.agent_prompt import build_stable_prompt, sync_session_system
from app.features.chat.memory import load_history, save_history, estimate_messages
from app.features.chat.agent import run_agent_stream, parse_tool_calls, _strip_tool_blocks
from app.tools import execute_tool
from app.tools.normal.documents import list_documents, file_info, read_document, search_document

from flask import Blueprint
from app.core.admin_access import admin_allowed as _admin_allowed
from app.core.agent_http import agent_or_400 as _agent_or_400
bp = Blueprint("image_admin", __name__)
log = logging.getLogger("image_admin")

# ─── 人工二审（审核 AI 判违规 → 管理员看着图点「过审」补发）──────────
#
# 队列由 app/image_review.py 管：审核闸门 allow_send 判 False 时把图留一份进
# state/review/，这里只负责「列出来」和「点一下」。图和记录都在 state/ 下，
# 跟生图账本同一套落盘约定（.gitignore 里，不进仓库）。


@bp.route("/api/agent/<agent_id>/image_review")
def get_image_review(agent_id):
    """人工二审队列。

    query：`state=pending|approved|rejected`（不传 = 全部）、`limit`（默认 50）。
    按时间倒序（最新在前）；每条附会话名，管理员要看着图判断是谁要的、什么时候。
    """
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    from app.features.image import review as image_review
    state = (request.args.get("state") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 50)
    except ValueError:
        limit = 50
    rows = image_review.items(state=state, limit=max(1, min(limit, 200)))
    out = []
    try:
        from app.features.qq import names as qq_names
        for r in rows:
            item = dict(r)
            item["name"] = qq_names.name_for(r.get("target"),
                                             r.get("target_id")) or ""
            out.append(item)
    except Exception:
        log.warning("二审队列补会话名失败，按无名显示")
        out = rows
    return jsonify({"ok": True, "agent": aid, "items": out,
                    "pending": image_review.pending_count()})


@bp.route("/api/agent/<agent_id>/image_review/<item_id>/approve",
           methods=["POST"])
def approve_image_review(agent_id, item_id):
    """人工过审 → **按正常格式**把这张图补发回原会话。

    「正常格式」是字面意思：caption 用拦下时存的那份原文、走 `qq_api.send_image`
    同一条路，对方收到的跟一张没被拦过的图一个字都不差。发完了补记生图账本
    （编号 → 提示词），否则「引用这张图问提示词」查不到。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    from app.features.image import review as image_review
    ok, msg = image_review.approve(item_id)
    return jsonify({"ok": ok, "agent": aid, "id": item_id, "msg": msg,
                    "pending": image_review.pending_count()}), \
        (200 if ok else 400)


@bp.route("/api/agent/<agent_id>/image_review/<item_id>/reject",
           methods=["POST"])
def reject_image_review(agent_id, item_id):
    """确认违规 → 标记拒发。图**留着不删**（备查，也防手滑点错还能翻回来）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    from app.features.image import review as image_review
    ok, msg = image_review.reject(item_id)
    return jsonify({"ok": ok, "agent": aid, "id": item_id, "msg": msg,
                    "pending": image_review.pending_count()}), \
        (200 if ok else 400)


@bp.route("/api/review/image/<path:name>")
def serve_review_image(name):
    """二审队列里那张图。只认 state/review 这一层目录下的文件名。

    `?thumb=1` 返回 480px WEBP 缩略图（列表与点开都用它，见 web/review.html
    与 admin.html）——原图 2000px 级、单张几 MB，列表铺 50 条走隧道卡死。
    缩略图生成失败自动退回原图，不影响判图。
    """
    from app.features.image import review as image_review
    d = image_review.REVIEW_DIR
    safe = os.path.basename(name)
    if not os.path.isfile(os.path.join(d, safe)):
        return jsonify({"error": "没有这张图"}), 404
    if request.args.get("thumb") == "1":
        thumb = image_review.thumb_for(safe)
        if thumb:
            return send_file(thumb, mimetype="image/webp")
    return send_from_directory(d, safe)


# ─── 人工二审·独立全局界面（/review）──────────────────────────────
# 跟上面的 per-agent 路由逻辑一致，只是**不绑 agent**：一个 URL 看全部
# agent / 群 / 私聊被拦的图。approve/reject 走的是队列记录里存的
# target/target_id，本来就不需要 agent，所以这里直接按 id 操作。


@bp.route("/review")
def review_page():
    """人工二审独立界面：全局看板，一条 URL 看所有 agent / 群 / 私聊被拦的图。"""
    return send_from_directory(WEB_DIR, "review.html")


@bp.route("/api/image_review")
def get_image_review_global():
    """人工二审全局队列（独立界面用）：不分 agent，一次看全部。

    query：`state=pending|approved|rejected`（不传 = 全部）、`limit`（默认 200）。
    每条附会话名（qq_names）+ agent，管理员看着图判断是谁、哪个 bot 要的、什么时候。
    """
    from app.features.image import review as image_review
    state = (request.args.get("state") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 200)
    except ValueError:
        limit = 200
    rows = image_review.items(state=state, limit=max(1, min(limit, 500)))
    out = []
    try:
        from app.features.qq import names as qq_names
        for r in rows:
            item = dict(r)
            item["name"] = qq_names.name_for(r.get("target"),
                                             r.get("target_id")) or ""
            out.append(item)
    except Exception:
        log.warning("二审全局队列补会话名失败，按无名显示")
        out = rows
    return jsonify({"ok": True, "items": out,
                    "pending": image_review.pending_count()})


@bp.route("/api/image_review/<item_id>/approve", methods=["POST"])
def approve_image_review_global(item_id):
    """全局过审（独立界面用）：逻辑与 agent 版一致，只是不绑 agent。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    from app.features.image import review as image_review
    ok, msg = image_review.approve(item_id)
    return jsonify({"ok": ok, "id": item_id, "msg": msg,
                    "pending": image_review.pending_count()}), \
        (200 if ok else 400)


@bp.route("/api/image_review/<item_id>/reject", methods=["POST"])
def reject_image_review_global(item_id):
    """全局驳回（独立界面用）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    from app.features.image import review as image_review
    ok, msg = image_review.reject(item_id)
    return jsonify({"ok": ok, "id": item_id, "msg": msg,
                    "pending": image_review.pending_count()}), \
        (200 if ok else 400)


# ─── 图片迁移（/migrate，独立界面）────────────────────────────
# 手工翻看一个文件夹里的图，挑中的**复制**一份到另一个文件夹。只复制不移动，
# 源目录永远不动；目标里已有同名文件就跳过（见 image_migrate 模块头）。
#
# 闸门分两档：
#   - 读（列目录 / 出图）：不挡，跟 /review 的取图接口一致。
#   - 写（复制 / 存配置）：走 `_admin_allowed()`，跟其它后台写接口同一道闸。
# 出图那一路**只认纯文件名**（`image_migrate.safe_name`），`send_from_directory`
# 再兜一层 —— 名字来自查询串，不挡住就能读任意文件。


@bp.route("/migrate")
def migrate_page():
    """图片迁移界面：一次一批、横向三列，每张下面一个「迁移」按钮。"""
    return send_from_directory(WEB_DIR, "migrate.html")


def _migrate_int(raw, default):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@bp.route("/api/migrate/config")
def get_migrate_config():
    """界面初始化用：上次填的输入/输出目录与批次大小。"""
    return jsonify(image_migrate.load_config())


@bp.route("/api/migrate/config", methods=["PUT"])
def put_migrate_config():
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    return jsonify(image_migrate.save_config(request.get_json(silent=True) or {}))


@bp.route("/api/migrate/list")
def list_migrate_images():
    """列一批。total / offset 都由后端算好，界面只管翻页。"""
    a = request.args
    return jsonify(image_migrate.scan(
        a.get("src", ""), a.get("dst", ""),
        _migrate_int(a.get("offset"), 0),
        _migrate_int(a.get("limit"), image_migrate.DEFAULT_BATCH)))


@bp.route("/api/migrate/image")
def serve_migrate_image():
    """出图。只认输入目录下的**顶层文件名**。

    带 `w` 参数就发缩略图（长边 w 的 JPEG）—— 源图常是 3072×4608 的 PNG，
    一批三张原图几十 MB，翻页会卡。不带 `w` 就是原图（点开大图走这条）。
    """
    src = image_migrate.normalize_dir(request.args.get("src", ""))
    name = image_migrate.safe_name(request.args.get("name", ""))
    if not src or not name:
        return jsonify({"error": "参数不合法"}), 400
    p = os.path.join(src, name)
    if not os.path.isfile(p):
        return jsonify({"error": "没有这张图"}), 404
    w = _migrate_int(request.args.get("w"), 0)
    if w > 0:
        data = image_migrate.thumbnail_bytes(p, max_side=w)
        if data:
            resp = Response(data, mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "no-store"
            return resp
    return send_from_directory(src, name)


@bp.route("/api/migrate/copy", methods=["POST"])
def copy_migrate_image():
    """复制一张。目标已有同名文件时**跳过**，返回 skipped=True。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    body = request.get_json(silent=True) or {}
    res = image_migrate.migrate(body.get("src", ""), body.get("name", ""),
                                body.get("dst", ""))
    return jsonify(res), (200 if res.get("ok") else 400)

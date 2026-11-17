"""管理面：生图 / 识图 / 私聊配额设置接口（Blueprint）。"""
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
from app.features.admin.common import put_override as _put_override
bp = Blueprint("admin_image_settings", __name__)
log = logging.getLogger("admin_image_settings")

@bp.route("/api/agent/<agent_id>/image_gen", methods=["PUT"])
def set_agent_image_gen(agent_id):
    """切该 agent 的生图总闸（一键关闭/恢复全部 QQ 生图）。热生效。

    settings.json 的 image_gen 字段：False = 全关；缺省 = 开。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["image_gen"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "image_gen": body["enabled"]})
@bp.route("/api/agent/<agent_id>/image_gen/<group_id>", methods=["PUT"])
def set_agent_image_gen_group(agent_id, group_id):
    """切某个群的生图开关（总闸开着时才有效）。热生效。

    存 settings.json 的 image_gen_muted（「关」的语义，与 interject_muted
    同型）：名单里的群不能生图，不在名单的可以。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    muted = set(settings.get("image_gen_muted") or [])
    if body["enabled"]:
        muted.discard(str(group_id))
    else:
        muted.add(str(group_id))
    settings["image_gen_muted"] = sorted(muted)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "image_gen": body["enabled"]})
@bp.route("/api/agent/<agent_id>/nai", methods=["PUT"])
def set_agent_nai(agent_id):
    """切该 agent 的 NAI 全局总闸（管理页「NAI·开 / 全关」）。热生效。

    settings.json 的 nai_enabled 字段：True = 启用；缺省 = 关（默认关，符合
    「默认关闭」的要求）。真正的紧急熔断在 .env 的 NAI_ENABLED。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    settings["nai_enabled"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "nai_enabled": body["enabled"]})
@bp.route("/api/agent/<agent_id>/nai/<group_id>", methods=["PUT"])
def set_agent_nai_group(agent_id, group_id):
    """把某个群加入 / 移出 NAI 白名单（总闸开着时才有效）。热生效。

    存 settings.json 的 nai_groups：名单里的群能用群主的 NAI，不在名单的
    不能用——这是群主「只给那个群」诉求的代码落地。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    groups = set(str(x) for x in (settings.get("nai_groups") or []))
    if body["enabled"]:
        groups.add(str(group_id))
    else:
        groups.discard(str(group_id))
    settings["nai_groups"] = sorted(groups)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "nai": body["enabled"]})
@bp.route("/api/agent/<agent_id>/nai_private/<qq_id>", methods=["PUT"])
def set_agent_nai_private(agent_id, qq_id):
    """把某个 QQ 加入 / 移出 NAI 私聊白名单（总闸开着时才有效）。热生效。

    存 settings.json 的 nai_private：名单里的 QQ 在**私聊**里能用 NAI，不在名单
    的不能用。与群白名单 nai_groups 相互独立——同一个 QQ 的私聊和群不互推。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    users = set(str(x) for x in (settings.get("nai_private") or []))
    if body["enabled"]:
        users.add(str(qq_id))
    else:
        users.discard(str(qq_id))
    settings["nai_private"] = sorted(users)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "qq": str(qq_id),
                    "nai": body["enabled"]})
def _image_send_format_from_body(body):
    """解析发图格式（jpg / png，大小写不敏感）；非法返回 None。"""
    v = body.get("format")
    if isinstance(v, str) and v.strip().lower() in agent_store.IMAGE_SEND_FORMATS:
        return v.strip().lower()
    return None
@bp.route("/api/agent/<agent_id>/image_send_format", methods=["PUT"])
def set_agent_image_send_format(agent_id):
    """设该 agent 的全局发图格式（QQ 里发出去的图用 jpg 还是 png）。热生效。

    settings.json 的 image_send_format 字段：缺省 = jpg（加这个开关之前的行为）。
    只管发出去那一张，ComfyUI output 里的原图不动；网页端不受影响。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    fmt = _image_send_format_from_body(body)
    if fmt is None:
        return jsonify({"error": "format 只能是 jpg 或 png"}), 400

    settings = agent_store.load_settings(aid)
    settings["image_send_format"] = fmt
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "image_send_format": fmt})
@bp.route("/api/agent/<agent_id>/image_send_format/<group_id>", methods=["PUT"])
def set_agent_image_send_format_group(agent_id, group_id):
    """设单群发图格式覆盖。format=null 删除覆盖（回落全局值）。热生效。

    存 settings.json 的 image_send_format_overrides（与
    interject_cooldown_overrides 同型：键缺 = 跟全局）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "format" not in body:
        return jsonify({"error": "需要字段 format（jpg / png，或 null）"}), 400
    fmt = None
    if body["format"] is not None:
        fmt = _image_send_format_from_body(body)
        if fmt is None:
            return jsonify({"error": "format 要么是 jpg / png，"
                                     "要么是 null（删除覆盖，跟全局）"}), 400

    if not _put_override(aid, group_id, "image_send_format", fmt):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "image_send_format": fmt})
def _set_audit_global(agent_id, scope):
    """两个总开关共用的实现（scope = "group" / "private"）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["image_audit_" + ("groups" if scope == "group" else "private")] = \
        body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "scope": scope,
                    "image_audit": body["enabled"]})
@bp.route("/api/agent/<agent_id>/image_audit_groups", methods=["PUT"])
def set_agent_image_audit_groups(agent_id):
    """设**所有群聊**的 NSFW 审核总开关。热生效。

    settings.json 的 image_audit_groups 字段：缺省 = False（默认全关）。
    开了之后，**所有群**生图发出去之前都会先让识图模型判一次（裸露 / 性暗示 /
    暧昧动作，口径适中：泳装内衣这类穿着本身放行，配性暗示动作或表情才算）；
    判违规**或审核没生效**都不发，各回一句提示（fail-closed，见 app/image_audit.py）。
    单个群想单独不同，用行里的「审核」开关覆盖。
    """
    return _set_audit_global(agent_id, "group")
@bp.route("/api/agent/<agent_id>/image_audit_private", methods=["PUT"])
def set_agent_image_audit_private(agent_id):
    """设**所有私聊**的 NSFW 审核总开关。热生效。

    跟 image_audit_groups 是**两个独立的开关**——只给群开、私聊不开（或反过来）
    是常见需求，合成一个的话每次都得再按会话类型逐个点。存
    settings.json 的 image_audit_private 字段，缺省 = False。
    """
    return _set_audit_global(agent_id, "private")
@bp.route("/api/agent/<agent_id>/image_audit_prompt", methods=["PUT"])
def set_agent_image_audit_prompt(agent_id):
    """设**自定义的审核提示词**（整份替换内置默认）。热生效。

    body: {"prompt": "..."}。传空串 / null / 空白 = 删掉自定义，回落内置默认
    （不是「用空提示词去审」——那等于不审，见 agents.image_audit_prompt）。

    ⚠️ 自定义的提示词必须自己保留「只输出一行 JSON」那条约定（`parse_verdict`
    只认它，`category` 只能是 ok|skin|sexual|nudity|other）。丢了输出格式 =
    每张图都解析失败 = fail-closed = **所有图都发不出去**。所以这里只做长度
    上限和类型校验，不替用户往文本里塞东西——塞了他就不知道自己到底在审什么了。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "prompt" not in body:
        return jsonify({"error": "需要字段 prompt（字符串；空串 = 恢复默认）"}), 400
    v = body["prompt"]
    if v is not None and not isinstance(v, str):
        return jsonify({"error": "prompt 要么是字符串，要么是 null"}), 400
    v = (v or "").strip()
    if len(v) > IMAGE_AUDIT_PROMPT_MAX:
        return jsonify({"error": "提示词太长（%d 字，上限 %d）：审核提示词每张图都要"
                                 "重发一遍，写长了纯烧 token"
                                 % (len(v), IMAGE_AUDIT_PROMPT_MAX)}), 400

    settings = agent_store.load_settings(aid)
    if v:
        settings["image_audit_prompt"] = v
    else:
        settings.pop("image_audit_prompt", None)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "prompt": v,
                    "using_default": not v,
                    "effective": v or image_audit.default_prompt()})
@bp.route("/api/agent/<agent_id>/vision_prompt", methods=["PUT"])
def set_agent_vision_prompt(agent_id):
    """设**自定义的识图提示词**（整份替换内置默认那份通用读图要求）。热生效。

    body: {"prompt": "..."}。传空串 / null / 空白 = 删掉自定义，回落内置默认
    （见 agents.vision_prompt）。

    与审核提示词最大的不同：**这里丢了输出格式 nothing 会坏**。识图的输出只是
    一段文字，拼进用户输入给下游模型看，写宽写窄都只是「读得细不细」，不像
    审核那样解析失败就 fail-closed 把每张图拦下。所以校验只有类型 + 长度上限
    （上限的理由见 config.VISION_PROMPT_MAX：识图每张图都要重发这一段）。

    唯一的软约定：文本里可以放 `{question}` 占位符，用户问题会替到那个位置；
    不放则在末尾补一行「用户的需求：…」。两种写法都保证识图模型知道用户想问
    什么——不带问题的话它只会泛泛描述一遍（见 app/vision.py 的注释）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "prompt" not in body:
        return jsonify({"error": "需要字段 prompt（字符串；空串 = 恢复默认）"}), 400
    v = body["prompt"]
    if v is not None and not isinstance(v, str):
        return jsonify({"error": "prompt 要么是字符串，要么是 null"}), 400
    v = (v or "").strip()
    if len(v) > VISION_PROMPT_MAX:
        return jsonify({"error": "提示词太长（%d 字，上限 %d）：识图每张图都要"
                                 "重发一遍，写长了纯烧 token"
                                 % (len(v), VISION_PROMPT_MAX)}), 400

    settings = agent_store.load_settings(aid)
    if v:
        settings["vision_prompt"] = v
    else:
        settings.pop("vision_prompt", None)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "prompt": v,
                    "using_default": not v,
                    "effective": v or vision_mod.default_prompt()})
@bp.route("/api/agent/<agent_id>/image_audit/<group_id>", methods=["PUT"])
def set_agent_image_audit_group(agent_id, group_id):
    """设单会话的审核覆盖（群号 / 对方 QQ 号都走这里）。enabled=null 删覆盖。

    存 settings.json 的 image_audit_overrides（与 image_send_format_overrides
    同型：键缺 = 跟全局）。群和私聊共用一张表，见 agents.image_audit_enabled。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return jsonify({"error": "需要字段 enabled（true / false，或 null）"}), 400
    v = body["enabled"]
    if v is not None and not isinstance(v, bool):
        return jsonify({"error": "enabled 要么是 true / false，"
                                 "要么是 null（删除覆盖，跟全局）"}), 400

    if not _put_override(aid, group_id, "image_audit", v):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "image_audit": v})
@bp.route("/api/agent/<agent_id>/private_enable", methods=["PUT"])
def set_agent_private_enable(agent_id):
    """切私聊总开关（一键关掉/恢复所有私聊，白名单都不看）。热生效。

    settings.json 的 private_enable 字段：False = 私聊全关；缺省回落 .env。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["private_enable"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "private_enable": body["enabled"]})
@bp.route("/api/agent/<agent_id>/private_whitelist_on", methods=["PUT"])
def set_agent_private_whitelist_on(agent_id):
    """切白名单开关。False = 名单不生效，任何人的私聊都放行（临时放开用）。

    存 settings.json 的 private_whitelist_on 字段，缺省视为 True。
    黑名单和私聊总开关不受影响，照常拦截。热生效。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["private_whitelist_on"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid,
                    "private_whitelist_on": body["enabled"]})
@bp.route("/api/agent/<agent_id>/private_image_quota", methods=["PUT"])
def set_agent_private_image_quota(agent_id):
    """设私聊每日生图额度：开关 + 每人每天上限。热生效。

    settings.json 两个字段：
    - `private_image_quota_on`：False = 不限量（**数字留着**，方便临时放开再开回来）；
    - `private_image_daily_limit`：每人每天张数，0 = 不限。
    只对**私聊**生效，群聊一行都不占（见 agents.image_quota_allowed）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body and "limit" not in body:
        return jsonify({"error": "至少要给 enabled 或 limit 之一"}), 400
    if "enabled" in body and not isinstance(body["enabled"], bool):
        return jsonify({"error": "enabled 需要布尔值"}), 400
    if "limit" in body:
        v = body["limit"]
        # 只收非负整数：bool 是 int 的子类，得单独挡掉（True 会变成 1 张）
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return jsonify({"error": "limit 需要 0 或正整数（0 = 不限量）"}), 400

    settings = agent_store.load_settings(aid)
    if "enabled" in body:
        settings["private_image_quota_on"] = body["enabled"]
    if "limit" in body:
        settings["private_image_daily_limit"] = body["limit"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid,
                    "private_image_quota_on":
                        settings.get("private_image_quota_on") is not False,
                    # 回**原始**数字而不是生效值：开关关掉时生效值是 0，
                    # 直接回它会把输入框清成 0，管理员就看不见原配置了。
                    "private_image_daily_limit":
                        agent_store.private_image_daily_limit_raw(aid)})
@bp.route("/api/agent/<agent_id>/private_image_quota_whitelist/<qq_id>",
           methods=["PUT"])
def set_agent_private_image_quota_whitelist(agent_id, qq_id):
    """把某个 QQ 加入 / 移出「私聊生图不限量」名单。热生效。

    存 settings.json 的 `private_image_quota_whitelist`。**和 private_whitelist
    是两回事**：那个管「谁能私聊」，这个管「谁生图不限量」——混用会导致哪天把
    私聊白名单打开时，名单里的人顺带变成生图不限量。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    users = set(str(x) for x in (settings.get("private_image_quota_whitelist") or []))
    if body["enabled"]:
        users.add(str(qq_id))
    else:
        users.discard(str(qq_id))
    settings["private_image_quota_whitelist"] = sorted(users)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "qq": str(qq_id),
                    "whitelisted": body["enabled"]})
@bp.route("/api/agent/<agent_id>/private_whitelist", methods=["PUT"])
def edit_agent_private_whitelist(agent_id):
    """私聊白名单增删（op: add / remove）。热生效。

    存 settings.json 的 private_whitelist。只要这个键被写进 settings，
    语义就是真白名单：名单外的 QQ（包括陌生人临时会话）一律静默不回，
    名单清空 = 谁都私聊不了。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    op = str(body.get("op") or "")
    user_id = str(body.get("user_id") or "").strip()
    if op not in ("add", "remove"):
        return jsonify({"error": "op 需要 add 或 remove"}), 400
    if not user_id.isdigit():
        return jsonify({"error": "user_id 需要是 QQ 号（纯数字）"}), 400

    settings = agent_store.load_settings(aid)
    wl = [str(x) for x in (settings.get("private_whitelist") or [])]
    if op == "add":
        if user_id not in wl:
            wl.append(user_id)
    else:
        wl = [x for x in wl if x != user_id]
    wl = sorted(set(wl))
    settings["private_whitelist"] = wl
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "op": op,
                    "private_whitelist": wl})

"""角色卡池读写接口（Blueprint）。"""
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
bp = Blueprint("persona_cards", __name__)

@bp.route("/api/characters")
def api_get_characters():
    """读卡库。卡库是**整表**读写的：卡的数量是几张、几十张的量级，
    整表替换比逐张 diff 简单得多，也不会出现半张卡存进去的状态。
    """
    from app.features.persona import service as persona
    from app.features.chat import background as chat_bg
    cards = persona.load_cards()
    for c in cards:
        c["background"] = chat_bg.info("card", str(c.get("id") or ""))
    return jsonify({"cards": cards})
@bp.route("/api/characters", methods=["PUT"])
def api_put_characters():
    """整表覆盖卡库。规范化（补字段 / 丢空名 / 去重 id）在 persona 里做。

    **只影响卡库，不动任何正在进行的会话**：某条会话线正在演的角色要是被
    删了，下一次说话时 `persona.active_card` 会取不到卡，自动退回生图机器人
    （不需要额外清理）。

    **批量删除闸**（2026-11-04 深夜）：整表覆盖 + 前端列表被截断 = 静默删卡。
    实测丢过 14 张。现在 `save_cards` 在「提交的卡不到磁盘一半」时抛
    `SaveCardsRejected`，这里把它变成 409 回给前端。想真删一批要带
    `{"force": true}`——故意多一个词，让「删掉一大片」变成一个有意识的动作。
    """
    from app.features.persona import service as persona
    from app.features.chat import background as chat_bg
    data = request.get_json(silent=True) or {}
    cards = data.get("cards")
    if not isinstance(cards, list):
        return jsonify({"ok": False, "error": "cards 必须是列表"}), 400
    try:
        ok = persona.save_cards(cards, force=bool(data.get("force")))
    except persona.SaveCardsRejected as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "rejected": True,
            "oldCount": e.old_n,
            "newCount": e.new_n,
            "lost": e.lost,
            "hint": "如果确实要删这么多，请在请求里带 force=true。",
        }), 409
    if not ok:
        return jsonify({"ok": False, "error": "卡库路径不可用"}), 500
    out = persona.load_cards()
    for c in out:
        c["background"] = chat_bg.info("card", str(c.get("id") or ""))
    return jsonify({"ok": True, "cards": out})

"""静态页面入口（Blueprint）：只负责把 web/ 下的页面发出去。"""
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
bp = Blueprint("pages", __name__)

@bp.route("/admin")
def admin_page():
    """agent 管理页（独立页面，不动 index.html）。"""
    return send_from_directory(WEB_DIR, "admin.html")
@bp.route("/status")
def status_page():
    """状态后台：队列 / 轮次 / ComfyUI / SnowLuma 的实时看板。"""
    return send_from_directory(WEB_DIR, "status.html")
@bp.route("/comic")
def comic_page():
    """漫画工作台：编剧 → 逐格渲染 → 加气泡（见 app/comic_api.py）。"""
    return send_from_directory(WEB_DIR, "comic_studio.html")
@bp.route("/comic_admin")
def comic_admin_page():
    """群友漫画台管理：独立页面，不塞在 /admin 的 QQ 机器人卡片里。

    只在 5174 开：5175 不反代这个路径，群友也拿不到 /api/comic/admin 的数据。
    """
    return send_from_directory(WEB_DIR, "comic_admin.html")
@bp.route("/comic_works")
def comic_works_page():
    """群友作品浏览：站主按账号翻群友跑的漫画 / 写真作品集（只读）。

    和 /comic_admin 同一信任边界——只在 5174 开，5175 不反代这个路径；数据
    走 /api/comic/works，那个接口还会再挡一次群友态（5175 是通配反代）。
    """
    return send_from_directory(WEB_DIR, "comic_works.html")
@bp.route("/moss")
def moss_comic_page():
    """moss 漫画台：一句话 → 编剧写整页 → 一次出图（见 app/moss_api.py）。"""
    return send_from_directory(WEB_DIR, "moss_studio.html")
@bp.route("/persona")
def persona_page():
    """网页端角色扮演（2026-11-03）。

    三栏：左选角色 / 中聊天 / 右看状态。和 QQ 用同一套 `persona` 引擎，
    见 `app/persona_api.py`。
    """
    return send_from_directory(WEB_DIR, "persona.html")
@bp.route("/novel")
def novel_page():
    """小说模式：丧尸末日生存模拟（见 app/novel.py、app/novel_api.py）。

    独立的第三套页面，左边玩法列表、中间小说正文、右边纯事实状态卡；
    跟 /persona 的好感数值条没有任何关系。
    """
    return send_from_directory(WEB_DIR, "novel.html")
@bp.route("/novel_admin")
def novel_admin_page():
    """小说模式记录：站主按 QQ 翻别人写了什么（只读）。

    跟 `/comic_admin` 同一个信任边界——只在 5174 开，5175 不反代这个路径；
    接口 `/api/novel/admin/*` 还会再挡一次群友态（5175 是通配反代）。
    """
    return send_from_directory(WEB_DIR, "novel_admin.html")
@bp.route("/persona_admin")
def persona_admin_page():
    """角色扮演查看台（2026-11-03）：谁玩过、聊了什么。

    只在 5174，且 5174 不对外——公网只映射 5175 的网关。同 `admin` / `comic`
    那些管理页一个口径。
    """
    return send_from_directory(WEB_DIR, "persona_admin.html")
@bp.route("/compat")
def compat_page():
    """兼容自检（2026-11-03）。

    群友反馈「看不到输入框」，但不同手机/浏览器表现不一样。这一页把
    UA、视口实测值、各特性是否支持全列出来，让问题可以截图定位，
    不用靠猜。
    """
    return send_from_directory(WEB_DIR, "compat.html")
@bp.route("/output")
def output_page():
    """图片管理台：ComfyUI output 目录的网页资源管理器（见 app/output_api.py）。"""
    return send_from_directory(WEB_DIR, "output_manager.html")
@bp.route("/characters")
def characters_page():
    """人设卡池：QQ 角色扮演用的人设卡（见 app/persona.py）。"""
    return send_from_directory(WEB_DIR, "characters.html")

@bp.route("/gallery")
def gallery_page():
    """作品墙。本机 / 内网管理端直连 5174 打开，后端按站主身份免登录；
    公网群友仍走 5175 隧道带 token。"""
    return send_from_directory(WEB_DIR, "gallery.html")

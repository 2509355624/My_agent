"""管理面：agent 详情 / 附言（Blueprint）。"""
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
bp = Blueprint("admin_agent_profile", __name__)
log = logging.getLogger("admin_agent_profile")

_NOTE_TEXT_MAX = 400

def _receipt_note_default():
    from app.tools.normal.generate_image import receipt_note_default
    return receipt_note_default()
def _caption_note_default():
    from app.features.image.delivery import caption_note_default
    return caption_note_default()
@bp.route("/api/agent/<agent_id>/receipt_note", methods=["PUT"])
def set_agent_receipt_note(agent_id):
    """设**提交回执的附言**（跟在「任务已提交…」+「当前渠道：X」后面那段）。热生效。"""
    return _set_note(agent_id, "receipt_note")
@bp.route("/api/agent/<agent_id>/caption_note", methods=["PUT"])
def set_agent_caption_note(agent_id):
    """设**出图 caption 的附言**（跟在 `编号 · 分辨率 · 渠道 · seed` 那行后面）。热生效。"""
    return _set_note(agent_id, "caption_note")
def _set_note(agent_id, key):
    """两张附言共用的写入口。

    body: {"note": "..."}。语义按用户 2026-10-06 的口径分三种，别混：
    - 传字符串（**含空串**）→ 原样存。空串 = 「这段我不要」，回执/caption
      就只发原来那一行；
    - 传 null → 删键，回到内置默认；
    - 不是字符串 → 400。

    ⚠️ 与 `set_agent_vision_prompt` 最大的不同：那边空串当「恢复默认」，这边
    空串是「关闭附言」——附言是给人看的装饰，关掉不影响任何功能，而「把框清空
    还想让我看到默认那段」才是会让人莫名其妙的。那边也不能照抄：审核/识图
    提示词写坏了会改变模型行为，附言写坏了只是刷屏多几行字，所以上限之外
    不做任何内容校验（不加「恢复默认」按钮，2026-10-06 用户明确不要）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "note" not in body:
        return jsonify({"error": "需要字段 note（字符串；空串 = 不要这段附言，"
                                 "null = 用内置默认）"}), 400
    v = body["note"]
    if v is None:
        settings = agent_store.load_settings(aid)
        settings.pop(key, None)
        if not agent_store.save_settings(aid, settings):
            return jsonify({"error": "写入 settings.json 失败"}), 500
        return jsonify({"ok": True, "agent": aid, "note": None,
                        "using_default": True})
    if not isinstance(v, str):
        return jsonify({"error": "note 要么是字符串，要么是 null"}), 400
    v = v.strip()
    if len(v) > _NOTE_TEXT_MAX:
        return jsonify({"error": "附言太长（%d 字，上限 %d）：这段每次发图都要"
                                 "刷屏一遍，写长了没人看" % (len(v), _NOTE_TEXT_MAX)}), 400
    settings = agent_store.load_settings(aid)
    settings[key] = v
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    default = (_receipt_note_default() if key == "receipt_note"
               else _caption_note_default())
    return jsonify({"ok": True, "agent": aid, "note": v,
                    "using_default": False,
                    "effective": v or default})
def _agent_detail(aid):
    """管理页需要的完整状态。provider / model 为空串表示「继承全局默认」。

    继承的语义（2026-10-04 用户定）= **走 .env 的整条降级链**：链头先上，
    失败顺位往下；选了具体 provider 才是钉死那一家（strict，不换家）。
    所以「实际生效」在继承时取链头，而不是固定的 LLM_PROVIDER。
    """
    cfg = agent_store.agent_config(aid)
    raw = agent_store.agent_raw_config(aid)
    from app.integrations.llm import parse_chain
    settings = get_llm_settings()
    chain = parse_chain(settings["fallback_chain"])
    default_model = PROVIDERS.get(settings["provider"], {}).get("model", MODEL)
    if cfg["provider"] or cfg["model"]:
        eff_provider = cfg["provider"] or settings["provider"]
        eff_model = cfg["model"] or PROVIDERS.get(eff_provider, {}).get("model", "")
    else:
        # 继承全局 = 降级链链头（链空才退默认 provider）
        eff_provider = chain[0][0] if chain else settings["provider"]
        eff_model = chain[0][1] if chain else default_model
    # 识图实际生效的那份：界面选过就按界面那份，否则退回 .env。
    # active_choice 内部会重读 settings（mtime 缓存），所以管理页改完立刻变。
    _v_pid, _v_model = vision_mod.active_choice(aid)
    _v_from_env = not agent_store.vision_choice(aid)[0]
    return {
        "id": aid,
        "name": cfg["name"] or aid,
        "description": cfg["description"],
        "provider": cfg["provider"],
        "model": cfg["model"],
        "prompt_file": cfg["prompt_file"],
        "prompt": agent_store.persona_text(aid),
        # vision 一并下发：管理页在下拉里换来换去时，用它即时判断该不该提示
        # 「这个模型读不了图」，不必等保存后再问后端
        "providers": [{"id": k, "label": v["label"], "model": v["model"],
                       "vision": bool(v.get("vision"))}
                      for k, v in PROVIDERS.items()],
        "global_provider": settings["provider"],
        "global_model": default_model,
        # 全局降级链（继承全局时文本模型实际跑的顺序），管理页拿它把
        # 「继承全局」的真正含义亮出来，别让人以为继承=固定某一家
        "global_chain": [{"pid": pid, "model": model,
                          "label": PROVIDERS.get(pid, {}).get("label", pid)}
                         for pid, model in chain],
        # 把「继承」算进去后实际会用的模型，让用户改完能立刻看到是什么效果
        "effective_provider": eff_provider,
        "effective_model": eff_model,
        # 带图能力：生效模型能不能直接读图。不能的话，带图请求会先经过识图
        # 预处理——识图固定走 vision_provider，与该 agent 自己的模型无关。
        "vision": provider_vision(eff_provider, eff_model),
        # 识图（对方发图给机器人时当眼睛用的那个）：给**实际生效**的那份，
        # 下方 novisual 提示里要显示它；`vision_from_env` = 有没有在界面上配过
        # （true 时用的是 .env 的值，改 .env 要重启才变）。
        "vision_provider": _v_pid,
        "vision_model": (_v_model
                         or PROVIDERS.get(_v_pid, {}).get("model", "")),
        "vision_from_env": _v_from_env,
        # 上下文预算：0 → 继承全局。session_tokens 是主会话的粗估体量，
        # 让「改完到底有没有用」立刻可见（网页端会话就是这一条）。
        # QQ 那些群各自一条会话线，不在这里体现，看日志里的 [cache] 行。
        "context_budget": cfg["context_budget"],
        "global_context_budget": CONTEXT_BUDGET,
        "effective_context_budget": cfg["context_budget"] or CONTEXT_BUDGET,
        "session_tokens": estimate_messages(load_history(aid)),
        # 只读展示：白名单收窄过没有（null = 不限制）。不提供编辑入口
        "tools": raw.get("tools"),
        "skills": raw.get("skills"),
    }
@bp.route("/api/agent/<agent_id>")
def get_agent_detail(agent_id):
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    return jsonify(_agent_detail(aid))
@bp.route("/api/agent/<agent_id>", methods=["PUT"])
def put_agent_detail(agent_id):
    """保存人设、模型与上下文预算。

    只接受 prompt / provider / model / context_budget 四项。agent.json 以磁盘
    原文为底做部分更新，所以 tools / skills 这些界面没暴露的字段会原样保留
    ——直接用规范化后的配置回写会把它们冲成默认值（等于悄悄放开白名单）。
    空串 / 0 是有效值，表示「该字段继承全局默认」。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    if "provider" in data or "model" in data:
        for key in ("provider", "model"):
            val = data.get(key, "")
            if not isinstance(val, str):
                return jsonify({"error": "%s 必须是字符串" % key}), 400
        provider = (data.get("provider") or "").strip().lower()
        model = (data.get("model") or "").strip()
        if provider and provider not in PROVIDERS:
            return jsonify({"error": "未知的 provider：%s" % provider}), 400

        raw = agent_store.agent_raw_config(aid)
        raw["provider"] = provider
        raw["model"] = model
        if not agent_store.save_agent_config(aid, raw):
            return jsonify({"error": "写入 agent.json 失败"}), 500

    if "context_budget" in data:
        val = data.get("context_budget")
        try:
            budget = int(val)
        except (TypeError, ValueError):
            return jsonify({"error": "context_budget 必须是整数，"
                                     "0 表示继承全局"}), 400
        if budget and not (agent_store.MIN_CONTEXT_BUDGET
                           <= budget <= agent_store.MAX_CONTEXT_BUDGET):
            return jsonify({
                "error": "context_budget 需在 %d ~ %d 之间，或填 0 继承全局"
                         % (agent_store.MIN_CONTEXT_BUDGET,
                            agent_store.MAX_CONTEXT_BUDGET)}), 400

        raw = agent_store.agent_raw_config(aid)
        raw["context_budget"] = budget
        if not agent_store.save_agent_config(aid, raw):
            return jsonify({"error": "写入 agent.json 失败"}), 500

    if "prompt" in data:
        prompt = data.get("prompt")
        if not isinstance(prompt, str):
            return jsonify({"error": "prompt 必须是字符串"}), 400
        if not agent_store.save_persona(aid, prompt):
            return jsonify({"error": "写入人设文件失败"}), 500

    # 保存后回读：返回「已落盘并已生效」的状态，而不是前端提交上来的值
    detail = _agent_detail(aid)
    detail["ok"] = True
    return jsonify(detail)

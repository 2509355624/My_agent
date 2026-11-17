"""管理面：会话提示词 / 插话 / 视觉模型设置接口（Blueprint）。"""
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
bp = Blueprint("admin_interject", __name__)
log = logging.getLogger("admin_interject")

@bp.route("/api/agent/<agent_id>/session_prompt/<path:session_key>",
           methods=["PUT"])
def set_agent_session_prompt(agent_id, session_key):
    """保存某个会话线（group_<群号> / private_<QQ号>）的提示词配置。

    三个字段各自独立、互不覆盖，空串 = 清除该项：
    - text   → session_prompts（会话附加词，拼在人设后面）
    - agent  → session_prompt_agents（借用某个 agent 的完整系统提示词当基底）
    - system → session_system_prompts（人设覆盖：只顶 prompt.md 那一层，
               工具列表 / Skill 目录 / 环境说明照常动态拼装；优先于借用）
    热生效：qq_bot 每轮重建首条 system。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("text"), str):
        return jsonify({"error": "需要字符串字段 text（空串=清除自定义）"}), 400
    if "system" in body and not isinstance(body.get("system"), str):
        return jsonify({"error": "system 需要字符串（空串=清除独立提示词）"}), 400

    borrow = str(body.get("agent") or "").strip()
    if borrow:
        from app.core.agents import safe_agent_id
        if safe_agent_id(borrow) is None or borrow not in {
                a["id"] for a in agent_store.list_agents()}:
            return jsonify({"error": "要借用的 agent 不存在：" + borrow}), 400

    settings = agent_store.load_settings(aid)

    # 完整独立提示词：优先级最高。存成独立字典，不跟另外两项互相覆盖——
    # 这样清了它，原来的借用/附加词还能原样回来。
    systems = settings.get("session_system_prompts")
    if not isinstance(systems, dict):
        systems = {}
    system = str(body.get("system") or "").strip()
    if system:
        systems[session_key] = system
    else:
        systems.pop(session_key, None)
    if systems:
        settings["session_system_prompts"] = systems
    else:
        settings.pop("session_system_prompts", None)

    prompts = settings.get("session_prompts")
    if not isinstance(prompts, dict):
        prompts = {}
    text = body["text"].strip()
    if text:
        prompts[session_key] = text
    else:
        prompts.pop(session_key, None)
    settings["session_prompts"] = prompts

    borrowed = settings.get("session_prompt_agents")
    if not isinstance(borrowed, dict):
        borrowed = {}
    if borrow and borrow != aid:
        borrowed[session_key] = borrow
    else:
        borrowed.pop(session_key, None)
    if borrowed:
        settings["session_prompt_agents"] = borrowed
    else:
        settings.pop("session_prompt_agents", None)

    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "session_key": session_key,
                    "set": bool(text), "borrowed": borrow or None,
                    "system": bool(system)})
@bp.route("/api/agent/<agent_id>/session_prompt_bulk", methods=["PUT"])
def set_agent_session_prompt_bulk(agent_id):
    """批量保存多条会话线的提示词配置（管理页勾选多个群/私聊一键应用）。

    body：
      keys   : ["group_123", "private_456", ...] 必填、非空、最多 500 条
      clear  : true → 把这几条会话的三项配置**全部清掉**（回到 agent 自己的
               prompt.md），此时忽略下面三个字段
      text   : 会话附加词（非空才写）
      agent  : 借用助手 id（非空才写）
      system : 人设覆盖（非空才写）

    **只写非空字段 —— 没填的一律不动**（2026-10-03 用户拍板）。所以「只想清掉
    某一个字段」走单条接口 /api/agent/<id>/session_prompt/<key>，或整条 clear。

    ⚠️ 三项语义别搞混（与单条接口一致）：`system` 是**顶替** prompt.md 的人设段
    （不是叠加，抄工具目录会跟自动拼的那份重复）；`text` 是**拼**在人设后面；
    `agent` 是整份借用 TA 的系统提示词。优先级 system > agent > text。
    ⚠️ 每条被改的会话下次发言都会**整段前缀缓存作废**（system 是消息数组第一条，
    前缀缓存只认从头逐字节相同的最长前缀），约 15K token 全价 —— 一次改到位最省。

    实现上**一次 load_settings → 改 → 一次 save_settings**：绝不循环调单条接口，
    那会写 N 次盘，而且每次都在旧快照上改，彼此覆盖。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    raw = body.get("keys")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "需要非空数组字段 keys"}), 400
    if len(raw) > 500:
        return jsonify({"error": "一次最多 500 条会话"}), 400

    keys, skipped = [], []
    for k in raw:
        safe = agent_store.safe_session_key(k)
        if safe is None:
            skipped.append(str(k)[:64])
        elif safe not in keys:
            keys.append(safe)
    if not keys:
        return jsonify({"error": "keys 里没有合法的会话 key"}), 400

    for f in ("text", "system"):
        if f in body and not isinstance(body.get(f), str):
            return jsonify({"error": "%s 需要字符串" % f}), 400
    text = str(body.get("text") or "").strip()
    system = str(body.get("system") or "").strip()
    borrow = str(body.get("agent") or "").strip()
    if borrow:
        if agent_store.safe_agent_id(borrow) is None or borrow not in {
                a["id"] for a in agent_store.list_agents()}:
            return jsonify({"error": "要借用的 agent 不存在：" + borrow}), 400
    do_clear = bool(body.get("clear"))
    # 三项各自独立、互不覆盖（与单条接口同一套存储键）。**没填的不动**；
    # 借自己 = 等于不借，直接当没填（否则会 200 却什么都不写，很难查）。
    fields = (("session_system_prompts", system),
              ("session_prompts", text),
              ("session_prompt_agents", borrow))
    writes = [] if do_clear else [(n, v) for n, v in fields if v and v != aid]
    if not do_clear and not writes:
        return jsonify({"error": "text / agent / system 至少要填一项"
                                 "（借自己 = 等于不借），或者传 clear=true 清除"}), 400

    settings = agent_store.load_settings(aid)
    applied, cleared = 0, set()
    if do_clear:
        for name, _value in fields:
            d = settings.get(name)
            if not isinstance(d, dict):
                continue
            for k in keys:
                if k in d:
                    d.pop(k, None)
                    cleared.add(k)
            if d:
                settings[name] = d
            else:
                settings.pop(name, None)      # 空了就删键，settings.json 别留空壳
    else:
        for name, value in writes:
            d = settings.get(name)
            if not isinstance(d, dict):
                d = {}
            for k in keys:
                d[k] = value
            settings[name] = d
        applied = len(keys)

    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "keys": keys,
                    "applied": applied, "cleared": len(cleared),
                    "skipped": skipped})
@bp.route("/api/agent/<agent_id>/interject/<group_id>", methods=["PUT"])
def set_agent_interject(agent_id, group_id):
    """切某个群的「主动发言」开关。热生效，不用重启。

    管理类写操作与 DELETE 同一道门：默认只允许本机。设置存 settings.json
    （agent 级运行时开关），不动 agent.json（那是重启级配置）。
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
    muted = set(settings.get("interject_muted") or [])
    if body["enabled"]:
        muted.discard(str(group_id))
    else:
        muted.add(str(group_id))
    settings["interject_muted"] = sorted(muted)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "interject": body["enabled"]})
@bp.route("/api/agent/<agent_id>/groups_muted", methods=["PUT"])
def set_groups_muted(agent_id):
    """切「所有群都不回」总开关。热生效，不用重启。

    开了之后群里连 @ 都不接（私聊照常），用于把机器人当纯私人工具。
    与逐群的 at_only 正交：那条收窄到「只认 @」，这条是「彻底不理群」。
    存 settings.json 的 groups_muted=true。

    写之前必须 read-modify-write（同 at_only：settings.json 还有 tools/skills
    白名单、会话人设、NAI 名单等键，整体覆盖会冲掉）。
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
    settings["groups_muted"] = bool(body["enabled"])
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "groups_muted": bool(body["enabled"])})
def _local_vision_model_ids():
    """本机 ollama 里**真能读图**的模型名集合。探不到返回空集（= 跳过校验）。

    判据两条同时成立：ollama 报了 vision（`is True`，None 是不确定、不算），
    且名字里有真视觉架构标记（`_ollama_vision_untrustworthy` 说明为什么不能
    只看 capabilities）。清单接口和保存校验都调这个——两处判据必须一致，
    否则会出现「下拉里看不到但能存进去」或反之。

    ⚠️ 别把它写成集合推导式里的 `and not`：vision=True 的真模型会被
    `is not True` 一起滤掉，结果本地清单整个变空、下拉里一个本地模型都没有
    （我踩过一次）。写成显式循环，条件只有一个地方。
    """
    try:
        cat = model_catalog.list_models("ollama")
    except Exception:                               # noqa: BLE001
        return set()
    good = set()
    for m in cat.get("models") or []:
        mid = m.get("id") or ""
        if m.get("vision") is not True:
            continue
        if _ollama_vision_untrustworthy(mid):
            continue
        good.add(mid)
    return good
def _ollama_vision_untrustworthy(model_id):
    """这个本地模型「报了 vision 但实测不读图」→ 别列进识图下拉。

    ollama 的 /api/tags 里 capabilities 是**按模型名猜**的，不是实测：纯文本的
    qwen3.5-abliterated:9b 与 nexusriot/Qwen3.5-Uncensored-...:9b 都报 vision，
    真发一张图过去它不读（同款现象在 qwen3.8-9b-heretic 上实测过：HTTP 400
    does not support multimodal）。列出来等于骗用户切过去、然后每轮识图失败。

    判据看**模型名里有没有真的视觉架构标记**（vl / vision / omni），不看
    capabilities。新装了别的视觉模型时这里不用改——只挡「名字里没有任何
    视觉标记」的那批，不写死具体名字。
    """
    name = (model_id or "").lower()
    if any(k in name for k in ("vl", "vision", "-vl-", "omni")):
        return False
    # 没有任何视觉架构标记 → 一律不信 capabilities。
    return True
@bp.route("/api/vision/models")
def vision_models():
    """列出可选的识图模型：云端带 vision 的 provider + 本地 ollama 视觉模型。

    带上当前生效的那个（`current`），好让前端默认选中它。
    `agent` 参数决定读谁的配置（管理页切 agent 时要跟着变），缺省用 QQ_AGENT_ID。
    """
    from app.integrations import vision as vmod

    aid = (request.args.get("agent") or QQ_AGENT_ID).strip()
    if agent_store.safe_agent_id(aid) is None:
        aid = QQ_AGENT_ID
    cur_pid, cur_model = vmod.active_choice(aid)
    items = []
    for pid, cfg in PROVIDERS.items():
        if not provider_vision(pid, None):
            continue
        # llama 是本地服务，标「云端」会误导（实测它识图 3.6 秒，也不慢）。
        if pid == "llama":
            note = "本地 llama.cpp，几秒"
        else:
            note = "云端，1~2 秒"
        items.append({
            "value": pid + ":",
            "provider": pid,
            "model": "",
            "label": "%s · %s（%s）"
                     % (cfg.get("label", pid), cfg.get("model", ""), note),
        })
    # 本地：判据只有一处（_local_vision_model_ids），别在下拉里另写一份
    local = []
    items_error = ""
    try:
        cat = model_catalog.list_models("ollama")
        good = _local_vision_model_ids()
        for mid in sorted(good):
            local.append({
                "value": "ollama:" + mid,
                "provider": "ollama",
                "model": mid,
                "label": "%s（%s）" % (mid, _VISION_LOCAL_SLOW_NOTE),
            })
        if cat.get("error"):
            items_error = "本地模型清单不完整（%s）" % cat["error"]
    except Exception as e:                        # noqa: BLE001
        # ollama 没开不是错误：云端那几项照样能用，只是本地那几项缺席。
        items_error = "拿不到本地模型清单（%s）" % e
    items.extend(local)

    cur_value = (cur_pid + ":" + (cur_model or "")) if cur_pid else ""
    return jsonify({"items": items, "current": cur_value,
                    "error": items_error,
                    "from_env": not agent_store.vision_choice(aid)[0]})
@bp.route("/api/agent/<agent_id>/vision_model", methods=["PUT"])
def set_vision_model(agent_id):
    """切识图模型。热生效，不用重启（vision.active_choice 每次调用重读配置）。

    body: {"value": "ollama:qwen3-vl:2b"} 或 {"value": ""}（= 退回 .env）。

    ⚠️ 只影响「对方发图给机器人时把它读成文字」这条**对话**链路。
    生图发出去前的 NSFW 审核不跟着切（用户 2026-10-04 拍板）——审核是
    fail-closed 且超时只有 30 秒，本地那个速度会把每张图都拦死。见
    vision.audit_choice。

    校验查 PROVIDERS 有没有这个 provider、本地模型在不在 ollama 清单里：
    写错名字的话，表现是每轮识图都 HTTP 404，而用户看不出是自己拼错了。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "value" not in body or not isinstance(body["value"], str):
        return jsonify({"error": "需要字符串字段 value"}), 400
    value = body["value"].strip()

    if value:
        pid, _, model = value.partition(":")
        pid = pid.strip()
        if pid not in PROVIDERS:
            return jsonify({"error": "没有这个 provider：%r" % pid}), 400
        if pid == "ollama" and model.strip():
            names = _local_vision_model_ids()
            if names and model.strip() not in names:
                return jsonify({"error": "本机 ollama 里没有这个能读图的模型：%r"
                                 % model.strip()}), 400

    # 写之前 read-modify-write（settings.json 还有 tools/skills 白名单等键）
    settings = agent_store.load_settings(aid)
    if value:
        settings[agent_store.VISION_MODEL_KEY] = value
    else:
        settings.pop(agent_store.VISION_MODEL_KEY, None)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    pid, model = vision_mod.active_choice(aid)
    return jsonify({"ok": True, "agent": aid, "value": value,
                    "effective_provider": pid, "effective_model": model})
@bp.route("/api/agent/<agent_id>/at_only/<group_id>", methods=["PUT"])
def set_group_at_only(agent_id, group_id):
    """切某个群的「只认 @」开关。热生效，不用重启。

    开了之后这个群**不再认 QQ_GROUP_KEYWORDS 那套免 @ 呼叫词**，只有真 @ 到
    才回。存 settings.json 的 at_only_groups（「加严」的语义），与
    interject_muted 的「关掉主动发言」正交——某群两个都开 = 只有 @ 才有反应。

    写之前必须 read-modify-write：settings.json 里还有 tools/skills 白名单、
    会话人设、NAI 名单等一大堆键，整体覆盖会把它们冲掉。
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
    only = set(str(x) for x in (settings.get("at_only_groups") or []))
    if body["enabled"]:
        only.add(str(group_id))
    else:
        only.discard(str(group_id))
    settings["at_only_groups"] = sorted(only)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "at_only": body["enabled"]})
def _cooldown_from_body(body):
    """从请求体解析冷却秒数。合法返回 0~3600 的整数（0=不限频），
    不合法/越界返回 None——写入层明确拒绝，越界收敛只在读取层兜底。"""
    v = body.get("cooldown")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not (agent_store.INTERJECT_COOLDOWN_MIN
            <= v <= agent_store.INTERJECT_COOLDOWN_MAX):
        return None
    return int(v)
@bp.route("/api/agent/<agent_id>/interject_cooldown", methods=["PUT"])
def set_interject_cooldown(agent_id):
    """设全局主动发言冷却秒数（settings.json 的 interject_cooldown）。热生效。

    0 = 不限频；删除限制走 PUT cooldown=0。回落 .env 默认目前没有做 UI，
    想恢复出厂就把值设成 .env 里的 QQ_INTERJECT_COOLDOWN。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    cd = _cooldown_from_body(body)
    if cd is None:
        return jsonify({"error": "需要数字字段 cooldown（0~3600 秒）"}), 400

    settings = agent_store.load_settings(aid)
    settings["interject_cooldown"] = cd
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "cooldown": cd})
@bp.route("/api/agent/<agent_id>/interject_cooldown/<group_id>",
           methods=["PUT"])
def set_interject_cooldown_group(agent_id, group_id):
    """设单群主动发言冷却覆盖。cooldown=null 删除覆盖（回落全局值）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "cooldown" not in body:
        return jsonify({"error": "需要字段 cooldown（0~3600 的数字，或 null）"}), 400
    if body["cooldown"] is None:
        cd = None
    else:
        cd = _cooldown_from_body(body)
        if cd is None:
            return jsonify({"error": "cooldown 要么是 0~3600 的数字，"
                                     "要么是 null（删除覆盖）"}), 400

    settings = agent_store.load_settings(aid)
    overrides = settings.get("interject_cooldown_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    if cd is None:
        overrides.pop(str(group_id), None)
    else:
        overrides[str(group_id)] = cd
    settings["interject_cooldown_overrides"] = overrides
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "cooldown": cd})
def _percent_from_body(body):
    """解析 0~100 的整数百分比；非数字/越界返回 None（写入层明确拒绝）。"""
    v = body.get("chance")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not 0 <= v <= 100:
        return None
    return int(v)
def _gap_from_body(body):
    """解析判断间隔秒数（0~3600）。0 = 不做这道闸。"""
    v = body.get("min_gap")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not (agent_store.INTERJECT_GAP_MIN <= v <= agent_store.INTERJECT_GAP_MAX):
        return None
    return int(v)
@bp.route("/api/agent/<agent_id>/interject_chance", methods=["PUT"])
def set_interject_chance(agent_id):
    """设全局主动发言的触发概率（百分比整数）。热生效。

    12 ≈ 1/8（默认）；0 = 从不主动开口；100 = 每批消息都去问模型。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    chance = _percent_from_body(body)
    if chance is None:
        return jsonify({"error": "需要数字字段 chance（0~100）"}), 400
    settings = agent_store.load_settings(aid)
    settings["interject_chance"] = chance
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "chance": chance})
@bp.route("/api/agent/<agent_id>/interject_chance/<group_id>",
           methods=["PUT"])
def set_interject_chance_group(agent_id, group_id):
    """设单群触发概率覆盖。chance=null 删除覆盖（回落全局值）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "chance" not in body:
        return jsonify({"error": "需要字段 chance（0~100 的数字，或 null）"}), 400
    if body["chance"] is None:
        chance = None
    else:
        chance = _percent_from_body(body)
        if chance is None:
            return jsonify({"error": "chance 要么是 0~100 的数字，"
                                     "要么是 null（删除覆盖）"}), 400
    if not _put_override(aid, group_id, "interject_chance", chance):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "chance": chance})
@bp.route("/api/agent/<agent_id>/interject_min_gap", methods=["PUT"])
def set_interject_min_gap(agent_id):
    """设全局「两次判断之间的最小秒数」。热生效。

    判断也是一次 API 调用，这道闸防刷屏时每条都问。0 = 不做这道闸
    （那就只剩概率门挡着）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    gap = _gap_from_body(body)
    if gap is None:
        return jsonify({"error": "需要数字字段 min_gap（0~3600 秒）"}), 400
    settings = agent_store.load_settings(aid)
    settings["interject_min_gap"] = gap
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "min_gap": gap})
@bp.route("/api/agent/<agent_id>/interject_min_gap/<group_id>",
           methods=["PUT"])
def set_interject_min_gap_group(agent_id, group_id):
    """设单群判断间隔覆盖。min_gap=null 删除覆盖（回落全局值）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "min_gap" not in body:
        return jsonify({"error": "需要字段 min_gap（0~3600 的数字，或 null）"}), 400
    if body["min_gap"] is None:
        gap = None
    else:
        gap = _gap_from_body(body)
        if gap is None:
            return jsonify({"error": "min_gap 要么是 0~3600 的数字，"
                                     "要么是 null（删除覆盖）"}), 400
    if not _put_override(aid, group_id, "interject_min_gap", gap):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "min_gap": gap})
@bp.route("/api/agent/<agent_id>/sessions/<key>", methods=["DELETE"])
def delete_agent_session(agent_id, key):
    """删除一条会话线（= 重置这条对话）。不可恢复。

    与 PUT 同一道门：管理类写操作默认只允许本机。二次确认在前端做，
    后端不做「要不要删」的判断——它只守住「谁有资格删」。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err

    ok, msg = agent_store.delete_session(aid, key)
    if not ok:
        return jsonify({"error": msg}), (404 if msg == "会话不存在" else 400)
    return jsonify({"ok": True, "agent": aid, "key": key})

# -*- coding: utf-8 -*-
"""公开产品展示页 API。

与内部扮演、小说、漫画和群聊接口隔离：访客只接触 showcase_prompts 中的
白名单素材，会话使用独立的高熵 id，RAG 使用 showcase_<hash> 会话线。
经 friend_app 转发时必须带网关令牌；5174 主站另有来源与隧道白名单双闸。
"""

import copy
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from app.config import (
    LLM_FALLBACK_CHAIN,
    LLM_PROVIDER,
    MODEL,
    PERSONA_PROVIDER,
    PERSONA_MODEL,
    PROVIDERS,
)
from app import comfy_out, friend_auth, image_audit, image_jobs, llm, persona_rag
from app import showcase_prompts
from app.tools.normal.generate_image import build_t2i_workflow


log = logging.getLogger("showcase")
bp = Blueprint("showcase", __name__)

_MAX_MESSAGE = 500
_MAX_HISTORY = 12
_MAX_SESSIONS = 128
_MAX_JOBS = 128
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_JOB_TAG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MODES = {item["id"] for item in showcase_prompts.PLAY_MODES}
_TERMINAL_JOBS = {"ready", "blocked", "error"}

# 展示会话和任务只存进程内；长期记忆由 persona_rag 按独立会话线持久化。
# 不读取内部 persona 存档，也不把访客消息写进任何群聊历史。
_LOCK = threading.RLock()
_SESSIONS = {}
_JOBS = {}
_STATS = {
    "calls": 0,
    "total_tokens": 0,
    "hit_tokens": 0,
    "miss_tokens": 0,
    "last_call": None,
}


@bp.before_request
def _friend_token_gate():
    """只认 5175 转发令牌；公网访客匿名，但不等于可绕过网关直连 API。"""
    try:
        good = friend_auth.secret()
    except Exception:
        log.warning("展示页拿不到内部密钥", exc_info=True)
        return jsonify({"error": "服务未就绪"}), 503
    token = request.headers.get("X-Friend-Token", "")
    if not token or not hmac.compare_digest(token, good):
        return jsonify({"error": "forbidden"}), 403
    return None


def _session(session_id=None, create=True):
    """取独立展示会话；只接受本接口签发的高熵 id。"""
    session_id = str(session_id or "").strip()
    if session_id and not _SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session")
    with _LOCK:
        if not session_id:
            if not create:
                return None, None
            session_id = secrets.token_urlsafe(32)
        sess = _SESSIONS.get(session_id)
        if sess is None:
            if not create:
                return session_id, None
            if len(_SESSIONS) >= _MAX_SESSIONS:
                oldest = min(_SESSIONS, key=lambda key: _SESSIONS[key]["last_active"])
                _SESSIONS.pop(oldest, None)
            sess = {
                "created": time.time(),
                "last_active": time.time(),
                "history": [],
                "turns": 0,
                "lock": threading.Lock(),
            }
            _SESSIONS[session_id] = sess
        sess["last_active"] = time.time()
        return session_id, sess


def _rag_line(session_id):
    """真实 session id 不落进向量库名，且不可能撞到内部 web_/guest_ 会话。"""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
    return "showcase_" + digest


def _history_context(history):
    """短上下文只服务第二路召回；完整注入仍以 messages 为准。"""
    parts = []
    for item in history[-4:]:
        role = "访客" if item.get("role") == "user" else "岚灯"
        parts.append(role + "：" + str(item.get("content") or "")[:180])
    return "\n".join(parts)


def _build_memory(session_id, query, history):
    trace = {}
    try:
        block = persona_rag.build_block(
            _rag_line(session_id), query,
            context=_history_context(history), trace=trace)
    except Exception:
        # 记忆故障不应让公开聊天整轮不可用；轨迹必须如实标记降级。
        log.exception("展示页记忆检索失败")
        trace = {
            "query": query,
            "hit_count": 0,
            "hits": [],
            "selected": [],
            "injected_count": 0,
            "block": "",
            "degraded": True,
        }
        block = ""
    trace["block"] = block
    return block, trace


def _record_usage():
    usage = llm.current_usage()
    with _LOCK:
        _STATS["calls"] += 1
        for key in ("total_tokens", "hit_tokens", "miss_tokens"):
            _STATS[key] += int(usage.get(key) or 0)
        _STATS["last_call"] = dict(usage)
    return usage


def _chain_snapshot():
    """完整配置链 + 当前冷却状态；不暴露 base_url、api_key 或错误详情。"""
    provider = (PERSONA_PROVIDER or "").strip().lower()
    model = (PERSONA_MODEL or "").strip()
    if provider in PROVIDERS:
        model = model or str(PROVIDERS[provider].get("model") or "")
        head = (provider, model)
    else:
        head = (LLM_PROVIDER, MODEL)
    ordered = []
    for item in [head] + llm.parse_chain(LLM_FALLBACK_CHAIN):
        if item not in ordered:
            ordered.append(item)
    now = time.time()
    with llm._DEAD_LOCK:
        dead = dict(llm._DEAD)
    entries = []
    for index, (pid, model_name) in enumerate(ordered):
        remaining = max(0, int(dead.get((pid, model_name), 0) - now))
        entries.append({
            "position": index + 1,
            "provider": pid,
            "model": model_name,
            "state": "cooldown" if remaining else "available",
            "cooldown_seconds": remaining,
            "next_on_error": None,
        })
    for index, entry in enumerate(entries):
        following = [item for item in entries[index + 1:]
                     if item["state"] == "available"]
        target = following[0] if following else None
        if target:
            entry["next_on_error"] = {
                "provider": target["provider"],
                "model": target["model"],
            }
    effective = next((item for item in entries if item["state"] == "available"),
                     entries[0] if entries else None)
    with _LOCK:
        stats = copy.deepcopy(_STATS)
    total = stats["total_tokens"]
    stats["hit_rate"] = (stats["hit_tokens"] / total) if total else 0.0
    return {
        "configured_head": entries[0] if entries else None,
        "effective_head": effective,
        "chain": entries,
        "blacklist": [item for item in entries if item["state"] == "cooldown"],
        "cache": stats,
        "failure_policy": "当前候选失败后按 chain 顺序切换；冷却项暂时跳过。",
    }


@bp.get("/api/showcase/config")
def config():
    return jsonify(showcase_prompts.public_config())


@bp.get("/api/showcase/chain")
def chain():
    return jsonify(_chain_snapshot())


@bp.post("/api/showcase/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    mode = str(data.get("mode") or "daily").strip()
    if not message or len(message) > _MAX_MESSAGE:
        return jsonify({"error": "消息需为 1 到 500 字"}), 400
    if mode not in _MODES:
        return jsonify({"error": "未知玩法"}), 400
    try:
        session_id, sess = _session(data.get("session_id"))
    except ValueError:
        return jsonify({"error": "无效展示会话"}), 400

    # 同一会话串行，避免双击导致历史顺序颠倒；不同访客互不阻塞。
    with sess["lock"]:
        history = copy.deepcopy(sess["history"])
        memory_block, rag_trace = _build_memory(session_id, message, history)
        system = showcase_prompts.roleplay_system(mode)
        messages = [{"role": "system", "content": system}]
        if memory_block:
            messages.append({"role": "system", "content": memory_block})
        messages.extend(history)
        messages.append({"role": "user", "content": message})
        trace = {
            "history_count": len(history),
            "rag": rag_trace,
            "messages": copy.deepcopy(messages),
        }
        try:
            reply = str(llm.call_llm(
                messages, provider=PERSONA_PROVIDER or None,
                model=PERSONA_MODEL or None, strict=False,
                max_tokens=800, temperature=0.8) or "").strip()
            if not reply:
                raise RuntimeError("empty model reply")
        except Exception:
            log.exception("展示页模型调用失败")
            return jsonify({
                "error": "模型暂时不可用，请稍后重试",
                "session_id": session_id,
                "trace": trace,
            }), 503

        usage = _record_usage()
        sess["history"].extend([
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ])
        sess["history"] = sess["history"][-_MAX_HISTORY:]
        sess["turns"] += 1
        try:
            persona_rag.remember(
                _rag_line(session_id), message, reply,
                custom=showcase_prompts.ROLE_CARD["background"],
                card_name=showcase_prompts.ROLE_CARD["name"])
        except Exception:
            # 回复已经生成，记忆后写失败不回滚聊天；日志保留排障入口。
            log.exception("展示页记忆写入排队失败")
        return jsonify({
            "session_id": session_id,
            "reply": reply,
            "mode": mode,
            "turn": sess["turns"],
            "usage": usage,
            "trace": trace,
        })


@bp.get("/api/showcase/memory")
def memory():
    session_id = request.args.get("session_id", "")
    query = str(request.args.get("query") or "").strip()
    if not query or len(query) > _MAX_MESSAGE:
        return jsonify({"error": "检索内容需为 1 到 500 字"}), 400
    try:
        session_id, sess = _session(session_id, create=False)
    except ValueError:
        return jsonify({"error": "无效展示会话"}), 400
    if sess is None:
        return jsonify({"error": "展示会话不存在"}), 404
    with sess["lock"]:
        history = copy.deepcopy(sess["history"])
    _block, trace = _build_memory(session_id, query, history)
    return jsonify({"session_id": session_id, "rag": trace})


@bp.get("/api/showcase/comic/replay")
def comic_replay():
    return jsonify(copy.deepcopy(showcase_prompts.COMIC_REPLAY))


def _active_job_for(session_id):
    with _LOCK:
        for record in _JOBS.values():
            if (record["owner"] == session_id
                    and record.get("status") not in _TERMINAL_JOBS):
                return record
    return None


def _job_view(record):
    """把内部 Job 收敛成公开展示字段，不转发其他任务的队列信息。"""
    job = record["job"]
    if job.entry and not record.get("images"):
        try:
            # 必须把 job 交给 output_images：History 里的 subfolder 可能为空
            # （扮演/批量渠道的老坑），这时要按 job 算出这次提交的输出目录，
            # 否则图明明出了，接口却读成“没有产物”。
            record["images"] = image_jobs.output_images(job.entry, job=job)
        except Exception:
            log.exception("展示页读取生图产物失败 %s", record["tag"])
            record["images"] = []
    if job.error:
        record["status"] = "error"
        record["error"] = "生图任务未完成，请稍后重试"
    elif job.done and record.get("images"):
        if record["audit"]["state"] == "pending":
            _start_audit(record)
        elif record["audit"]["state"] == "running":
            record["status"] = "reviewing"
    elif job.done:
        record["status"] = "error"
        record["error"] = "图片产物暂不可用，请刷新状态或重新生成"
    elif job.started:
        record["status"] = "running"
    else:
        record["status"] = "queued"

    position = None
    if record["status"] == "queued":
        try:
            position = image_jobs.ahead_of(job)
        except Exception:
            log.exception("展示页读取排队位置失败 %s", record["tag"])
    try:
        depth = image_jobs.queue_depth()
    except Exception:
        depth = None
    now = time.time()
    result = {
        "tag": record["tag"],
        "session_id": record["owner"],
        "status": record["status"],
        "scene": copy.deepcopy(record["scene"]),
        "trace": {
            "prompt": record["prompt"],
            "prompt_contract": "预置干净场景；请求体不接受自定义提示词",
            "prompt_review": "passed",
            "seed": record["seed"],
            "queue_position": position,
            "queue_depth": depth,
            "elapsed_seconds": round(now - record["created"], 1),
            "output_review": copy.deepcopy(record["audit"]),
        },
        "image_url": None,
    }
    if record["status"] == "ready":
        result["image_url"] = (
            "/api/showcase/image?tag=" + record["tag"]
            + "&session_id=" + record["owner"])
    if record["status"] == "error":
        result["error"] = record.get("error") or "生图任务未完成"
    if record["status"] == "blocked":
        result["error"] = "图片未通过公开展示审核"
    return result


def _start_audit(record):
    """网页公开展示不能走 allow_send 的网页豁免，必须显式审图。"""
    with _LOCK:
        if record["audit"]["state"] != "pending":
            return
        record["audit"] = {"state": "running", "allow": None}
        record["status"] = "reviewing"
        thread = threading.Thread(
            target=_audit_job, args=(record,),
            name="showcase-audit-" + record["tag"], daemon=True)
        record["audit_thread"] = thread
    thread.start()


def _audit_job(record):
    try:
        images = record.get("images") or []
        local = comfy_out.local_output_file(images[0]) if images else None
        if not local:
            verdict = image_audit.Verdict(
                False, category="other", failed=True, reason="展示图本地文件不存在")
        else:
            verdict = image_audit.check(local)
        with _LOCK:
            record["audit"] = {
                "state": "passed" if verdict.allow else "blocked",
                "allow": bool(verdict.allow),
                "category": str(verdict.category or "other"),
                "failed": bool(verdict.failed),
            }
            record["status"] = "ready" if verdict.allow else "blocked"
            if not verdict.allow:
                record["error"] = "图片未通过公开展示审核"
    except Exception:
        # 审核服务本身异常也必须关闸，不能先把未审图发出去再补记录。
        log.exception("展示页图片审核失败 %s", record["tag"])
        with _LOCK:
            record["audit"] = {
                "state": "blocked", "allow": False,
                "category": "other", "failed": True,
            }
            record["status"] = "blocked"
            record["error"] = "图片审核暂未完成"


@bp.post("/api/showcase/image/submit")
def image_submit():
    data = request.get_json(silent=True) or {}
    scene = showcase_prompts.image_scene(str(data.get("scene_id") or ""))
    if scene is None:
        return jsonify({"error": "请选择预置展示场景"}), 400
    try:
        session_id, _sess = _session(data.get("session_id"))
    except ValueError:
        return jsonify({"error": "无效展示会话"}), 400
    active = _active_job_for(session_id)
    if active:
        active_view = _job_view(active)
        if active_view["status"] not in _TERMINAL_JOBS:
            return jsonify({"error": "当前会话已有一张图在途",
                            "job": active_view}), 409

    seed = secrets.randbelow(2 ** 31 - 1)
    try:
        workflow = build_t2i_workflow(
            scene["skill"], scene["prompt"], seed,
            user_text="", key="")
        if not isinstance(workflow, dict) or not workflow:
            raise RuntimeError("empty workflow")
        job, reason = image_jobs.enqueue(
            None, session_id, workflow, skill=scene["skill"],
            prompt=scene["prompt"], intent="showcase:" + scene["id"],
            seed=seed, quiet=True, out_dir="web/showcase")
    except Exception:
        log.exception("展示页生图入队失败")
        return jsonify({"error": "生图渠道暂时不可用，请稍后重试"}), 503
    if not job:
        log.warning("展示页生图未入队：%s", reason)
        return jsonify({"error": "生图队列暂时无法接收新任务"}), 503

    record = {
        "tag": job.tag,
        "owner": session_id,
        "scene": scene,
        "prompt": scene["prompt"],
        "seed": seed,
        "job": job,
        "created": time.time(),
        "status": "queued",
        "images": [],
        "audit": {"state": "pending", "allow": None},
    }
    with _LOCK:
        if len(_JOBS) >= _MAX_JOBS:
            terminal = [key for key, value in _JOBS.items()
                        if value.get("status") in _TERMINAL_JOBS]
            for key in terminal[:max(1, len(_JOBS) - _MAX_JOBS + 1)]:
                _JOBS.pop(key, None)
        _JOBS[job.tag] = record
    return jsonify({"job": _job_view(record)}), 202


def _owned_job(tag, session_id):
    if not _JOB_TAG.fullmatch(str(tag or "")):
        return None
    try:
        session_id, sess = _session(session_id, create=False)
    except ValueError:
        return None
    if sess is None:
        return None
    with _LOCK:
        record = _JOBS.get(str(tag or ""))
    if not record or record["owner"] != session_id:
        return None
    return record


@bp.get("/api/showcase/image/status")
def image_status():
    record = _owned_job(request.args.get("tag"), request.args.get("session_id"))
    if record is None:
        return jsonify({"error": "展示任务不存在"}), 404
    return jsonify({"job": _job_view(record)})


@bp.get("/api/showcase/image")
def image_file():
    record = _owned_job(request.args.get("tag"), request.args.get("session_id"))
    if record is None:
        return jsonify({"error": "展示图片不存在"}), 404
    _job_view(record)
    if record["status"] != "ready" or not record.get("images"):
        return jsonify({"error": "图片尚未通过审核"}), 409
    local = comfy_out.local_output_file(record["images"][0])
    if not local:
        return jsonify({"error": "图片文件暂不可用"}), 404
    return send_file(local, conditional=True, max_age=0)


def register(app):
    app.register_blueprint(bp)

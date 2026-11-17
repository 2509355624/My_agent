# -*- coding: utf-8 -*-
"""公开产品展示页 API。

与内部扮演、小说、漫画和群聊接口隔离：访客只接触 showcase_prompts 中的
白名单素材，会话使用独立的高熵 id，RAG 使用 showcase_<hash> 会话线。
经 friend_app 转发时必须带网关令牌；5174 主站另有来源与隧道白名单双闸。
"""

import copy
import hashlib
import hmac
import json
import logging
import os
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
    WEB_DIR,
    state_path,
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

# 只读母本知识库：由 tools/_seed_showcase_guide.py 灌入，250 轮讲解 / 377 条。
# 展示页只读它，绝不写入；访客自己的对话写进 `_rag_line(session_id)` 私有线。
_KB_LINE = "showcase_guide_v1"

# 新访客的「上次参观」：直接填满 12 条窗口，一开口就能演长记忆。
# 刻意不给答案——第 4 轮只说「里面有只亮着的东西，没看清牌子」，
# 把「那只会发光的是什么」留给检索去答，否则等于把答案写在题目里。
_PRESET = [
    {"role": "user", "content": "今天只有一个小时，能走完吗"},
    {"role": "assistant", "content": "走不完，咱们先挑重点。从门口的大洋池开始，蓝鲸那缸先看一眼，再往里走。"},
    {"role": "user", "content": "这缸水得多大啊"},
    {"role": "assistant", "content": "两千多吨。它游一圈要花不少时间，别急，等它自己从你面前过去就行。"},
    {"role": "user", "content": "蓝鲸真的吃虾吗"},
    {"role": "assistant", "content": "吃磷虾，一天能吞好几吨。它没有牙，靠鲸须把水滤出去，就像拿筛子捞面条——水走了，面留下。"},
    {"role": "user", "content": "前面那片黑的区域是什么"},
    {"role": "assistant", "content": "深海区。那边为了模拟深海基本不开灯，里面有只亮着的东西，上回太暗我没看清牌子，下次带你去看。"},
    {"role": "user", "content": "那我今天先到这儿"},
    {"role": "assistant", "content": "行。下次从深海区接着往里走，过了就是水母厅，那片的灯是紫的。"},
    {"role": "user", "content": "出口在哪，我朋友在等我"},
    {"role": "assistant", "content": "直走右转，水母厅门口有指示牌。下次早点来，深海区那边值得多待一会儿。"},
]

# 展示会话和任务只存进程内；长期记忆由 persona_rag 按独立会话线持久化。
# 不读取内部 persona 存档，也不把访客消息写进任何群聊历史。
_LOCK = threading.RLock()
_SESSIONS = {}
_JOBS = {}
_STATE_FILE = state_path("showcase_state.json")
_STATS = {
    "calls": 0,
    "total_tokens": 0,
    "hit_tokens": 0,
    "miss_tokens": 0,
    "last_call": None,
}


def _persist():
    """把会话与任务的可序列化部分落盘，刷新/重启后能恢复终态结果。

    只存「无副作用」的字段：会话的 history/turns、任务的 tag/owner/scene/
    prompt/seed/created/status/images/audit。`job` 是 image_jobs 的 Job 对象
    （含 threading.Event、entry 等），`audit_thread` 是线程句柄，都不能
    JSON 序列化，落盘时剔除。恢复后的任务带 job=None，_job_view 走终态分支。
    """
    try:
        with _LOCK:
            jobs = {}
            for tag, record in _JOBS.items():
                jobs[tag] = {
                    "tag": record.get("tag"),
                    "owner": record.get("owner"),
                    "scene": record.get("scene"),
                    "prompt": record.get("prompt"),
                    "seed": record.get("seed"),
                    "created": record.get("created"),
                    "status": record.get("status"),
                    "images": record.get("images"),
                    "audit": record.get("audit"),
                }
            sessions = {}
            for sid, sess in _SESSIONS.items():
                sessions[sid] = {
                    "created": sess.get("created"),
                    "last_active": sess.get("last_active"),
                    "history": sess.get("history"),
                    "turns": sess.get("turns"),
                }
            payload = {"jobs": jobs, "sessions": sessions}
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)
    except Exception:
        log.exception("展示页状态落盘失败")


def _load_state():
    """启动时读回上次落盘的会话与任务（终态结果）。"""
    try:
        if not os.path.exists(_STATE_FILE):
            return
        with open(_STATE_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        with _LOCK:
            for sid, sess in (payload.get("sessions") or {}).items():
                _SESSIONS[sid] = {
                    "created": sess.get("created") or time.time(),
                    "last_active": sess.get("last_active") or time.time(),
                    "history": sess.get("history") or [],
                    "turns": int(sess.get("turns") or 0),
                    "lock": threading.Lock(),
                }
            for tag, record in (payload.get("jobs") or {}).items():
                if record.get("status") not in _TERMINAL_JOBS:
                    # 进程重启后，还在跑/排队的任务句柄已丢失，无法继续推进；
                    # 与其让访客永远轮询一个死任务，不如只恢复终态结果。
                    continue
                record["job"] = None
                _JOBS[tag] = record
    except Exception:
        log.exception("展示页状态读回失败")



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
                # 新访客自带「上次参观」的 6 轮窗口，一开口就能演长记忆。
                "history": copy.deepcopy(_PRESET),
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
    name = showcase_prompts.ROLE_CARD["name"]
    for item in history[-4:]:
        role = "访客" if item.get("role") == "user" else name
        parts.append(role + "：" + str(item.get("content") or "")[:180])
    return "\n".join(parts)


def _build_memory(session_id, query, history, max_hits=None):
    trace = {}
    try:
        block = persona_rag.build_block(
            _rag_line(session_id), query,
            context=_history_context(history), trace=trace,
            extra_lines=(_KB_LINE,), max_hits=max_hits)
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
    mode = str(data.get("mode") or "guide").strip()
    if not message or len(message) > _MAX_MESSAGE:
        return jsonify({"error": "消息需为 1 到 500 字"}), 400
    if mode not in _MODES:
        return jsonify({"error": "未知玩法"}), 400
    try:
        session_id, sess = _session(data.get("session_id"))
    except ValueError:
        return jsonify({"error": "无效展示会话"}), 400

    # 召回候选上限：页面可选 5 / 10 / 20，只截候选、不动阈值
    # （见 persona_rag.build_block 的 max_hits）。
    try:
        top = int(data.get("top") or 0)
    except (TypeError, ValueError):
        top = 0
    top = min(20, max(3, top)) if top else None

    # 同一会话串行，避免双击导致历史顺序颠倒；不同访客互不阻塞。
    with sess["lock"]:
        history = copy.deepcopy(sess["history"])
        memory_block, rag_trace = _build_memory(session_id, message, history,
                                                max_hits=top)
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
        _persist()
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
            "preset_turns": len(_PRESET) // 2,
            "recall_top": top or 0,
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


def _rag_client():
    """拿 RAG 客户端；没起就起一次，最多等 4 秒。

    daemon 冷启动要 3~8 秒（加载 torch + bge 模型）。展示页是公开页，
    访客点开就得看到库，不能让他自己去猜「稍后再试」。等不到也返回
    客户端，调用方按 `is_ready()` 决定说什么。
    """
    client = persona_rag._client()
    if client.is_ready():
        return client
    if not client.start():
        return client
    for _ in range(8):
        if client.is_ready():
            return client
        time.sleep(0.5)
    return client


def _kind_of(entry):
    return str((entry.get("metadata") or {}).get("kind") or "")


def _turn_span(meta):
    """条目覆盖的轮次区间 `(start, end)`，取不到返回 `(0, 0)`。

    原文层一条就是一轮（`turn`）；批次叙事和原子事实是每 5 轮压出来的，
    元数据里只有 `turn_start`/`turn_end`、没有 `turn`。之前这里只读 `turn`
    和 `seq`，于是批次/事实在页面上全显示成「第 ? 轮」（2026-11-16 反馈）。
    """
    def _as_int(key):
        try:
            return int(meta.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    for key in ("turn", "seq"):
        value = _as_int(key)
        if value:
            return value, value
    start, end = _as_int("turn_start"), _as_int("turn_end")
    if start and end and end < start:
        start, end = end, start
    return start, (end or start)


def _entry_view(entry, line_tag):
    """只读条目视图：前端要看到「写进库之前的片」，所以正文原样给出。"""
    meta = entry.get("metadata") or {}
    content = str(entry.get("content") or "")
    start, end = _turn_span(meta)
    return {
        "id": str(entry.get("id") or ""),
        "line": line_tag,
        "kind": _kind_of(entry),
        "role": str(meta.get("role") or ""),
        "turn": start,
        "turn_start": start,
        "turn_end": end,
        "day": meta.get("day") or 0,
        "chars": len(content),
        "content": content[:1200],
    }


# ─── 母本完整参观记录（只读） ─────────────────────────────
# 页面上能看到的「历史」只有窗口里那 12 条，剩下 238 条其实都在母本里。
# 原本只能靠翻库里的 raw 片段一条条看，读起来不像对话（一条一段，还带着
# 「她：」「用户说：」的前缀）。这里直接读母本源文件——它才是权威版本，
# 按「站」每 5 轮一组发给前端，翻起来跟真的参观记录一样。
_TRANSCRIPT_PATH = os.path.join(WEB_DIR, "showcase_guide.json")
_TRANSCRIPT_LOCK = threading.Lock()
_TRANSCRIPT_CACHE = {"mtime": -1.0, "turns": 0, "groups": []}
_TURNS_PER_STOP = 5
# 讲解词里的展项名长这样：「第一站就是大洋池·蓝鲸展区」。直接按 `·` 截
# 会把「前面就是」这种引导语一起带进去，所以左右各自剥掉常见前后缀。
_STOP_LEAD = ("第一站就是", "这一站是", "前面就是", "这里是", "接下来是",
              "现在到", "到了", "就是", "这是", "是")
_STOP_BREAK = re.compile(r"[，。、；：！？\s]")


def _stop_name(text):
    """从一句讲解词里取「展区·展项」，取不到返回空串。"""
    idx = text.find("·")
    if idx <= 0:
        return ""
    left = _STOP_BREAK.split(text[max(0, idx - 12):idx])[-1]
    right = _STOP_BREAK.split(text[idx + 1:idx + 14])[0]
    for lead in _STOP_LEAD:
        if left.startswith(lead):
            left = left[len(lead):]
            break
    left = re.sub(r"^第[一二三四五六七八九十百]+站?", "", left)
    left = re.sub(r"^(这个|那个|来到|走进|走到)", "", left)
    right = re.sub(r"(到了|就在这里|在这儿|了)$", "", right)
    if not left or not right or len(left) > 8 or len(right) > 12:
        return ""
    return left + "·" + right


def _transcript_groups():
    """母本 turns → 按站分组；母本被重新生成（mtime 变了）就重读。"""
    try:
        mtime = os.path.getmtime(_TRANSCRIPT_PATH)
    except OSError:
        return {"turns": 0, "groups": []}
    with _TRANSCRIPT_LOCK:
        if _TRANSCRIPT_CACHE["mtime"] == mtime:
            return dict(_TRANSCRIPT_CACHE)
    try:
        with open(_TRANSCRIPT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        log.exception("展示页读取母本记录失败")
        return {"turns": 0, "groups": []}

    turns = data.get("turns") or []
    groups = []
    for start in range(0, len(turns), _TURNS_PER_STOP):
        chunk = turns[start:start + _TURNS_PER_STOP]
        if not chunk:
            continue
        no = len(groups) + 1
        name = ""
        for item in chunk:
            if item.get("role") == "guide":
                name = _stop_name(str(item.get("text") or ""))
                if name:
                    break
        groups.append({
            "no": no,
            "name": name or ("第 %d 站" % no),
            "turn_start": int(chunk[0].get("r") or start + 1),
            "turn_end": int(chunk[-1].get("r") or start + len(chunk)),
            "turns": [{
                "r": int(item.get("r") or 0),
                "role": "guide" if item.get("role") == "guide" else "visitor",
                "text": str(item.get("text") or ""),
            } for item in chunk],
        })

    view = {"mtime": mtime, "turns": len(turns), "groups": groups}
    with _TRANSCRIPT_LOCK:
        _TRANSCRIPT_CACHE.update(view)
    return dict(view)


@bp.get("/api/showcase/guide/transcript")
def guide_transcript():
    """母本 250 轮的完整参观记录，按站翻页；只读 JSON，不碰向量库也不调模型。"""
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        offset = 0
    try:
        limit = min(25, max(1, int(request.args.get("limit") or 10)))
    except ValueError:
        limit = 10
    data = _transcript_groups()
    groups = data["groups"]
    return jsonify({
        "total_groups": len(groups),
        "total_turns": data["turns"],
        "offset": offset,
        "limit": limit,
        "groups": groups[offset:offset + limit],
    })


@bp.get("/api/showcase/guide/state")
def guide_state():
    """一次性给出右栏 ① 需要的全部状态：轮次、窗口、库统计。"""
    try:
        session_id, sess = _session(request.args.get("session_id"), create=True)
    except ValueError:
        return jsonify({"error": "无效展示会话"}), 400
    client = _rag_client()
    ready = client.is_ready()
    kb_entries = []
    private_entries = []
    if ready:
        try:
            kb_entries = client.list_entries(
                persona_rag.collection_name(_KB_LINE), timeout=30).get("entries") or []
        except Exception:
            log.exception("展示页读取母本库失败")
        try:
            private_entries = client.list_entries(
                persona_rag.collection_name(_rag_line(session_id)),
                timeout=30).get("entries") or []
        except Exception:
            log.exception("展示页读取访客线失败")

    def _count(entries):
        out = {"raw": 0, "batch": 0, "fact": 0, "total": len(entries)}
        for entry in entries:
            kind = _kind_of(entry)
            if kind in out:
                out[kind] += 1
        return out

    wire = _transcript_groups()
    with sess["lock"]:
        history = copy.deepcopy(sess["history"])
        turns = int(sess["turns"])
    return jsonify({
        "session_id": session_id,
        "ready": ready,
        "turn": turns,
        "preset_turns": len(_PRESET) // 2,
        "window": {
            "limit": _MAX_HISTORY,
            "count": len(history),
            "messages": history,
        },
        # 母本全程：250 轮、50 站。页面左边「现场对话」只有窗口那 12 条，
        # 这个数字才是「整条参观路线已经走了多少」。
        "transcript": {"turns": wire["turns"], "stops": len(wire["groups"])},
        "kb": {"line": _KB_LINE, "counts": _count(kb_entries)},
        "private": {"line": _rag_line(session_id), "counts": _count(private_entries)},
    })


@bp.get("/api/showcase/guide/library")
def guide_library():
    """分页浏览库里的条目；`line=kb|private|all`，`kind=raw|batch|fact`。"""
    try:
        session_id, sess = _session(request.args.get("session_id"), create=False)
    except ValueError:
        return jsonify({"error": "无效展示会话"}), 400
    which = str(request.args.get("line") or "kb").strip().lower()
    if which not in ("kb", "private", "all"):
        which = "kb"
    kind = str(request.args.get("kind") or "").strip().lower()
    keyword = str(request.args.get("q") or "").strip()
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        offset = 0
    try:
        limit = min(60, max(1, int(request.args.get("limit") or 20)))
    except ValueError:
        limit = 20

    targets = []
    if which in ("kb", "all"):
        targets.append(("kb", _KB_LINE))
    if which in ("private", "all") and sess is not None:
        targets.append(("private", _rag_line(session_id)))

    client = _rag_client()
    if not client.is_ready():
        return jsonify({"error": "检索服务还没就绪，稍等几秒再试"}), 503

    rows = []
    for tag, line in targets:
        try:
            entries = client.list_entries(
                persona_rag.collection_name(line), timeout=30).get("entries") or []
        except Exception:
            log.exception("展示页列库失败 %s", line)
            continue
        for entry in entries:
            view = _entry_view(entry, tag)
            if kind and view["kind"] != kind:
                continue
            if keyword and keyword not in view["content"]:
                continue
            rows.append(view)

    order = {"raw": 0, "batch": 1, "fact": 2}
    rows.sort(key=lambda item: (order.get(item["kind"], 9),
                                int(item["turn"] or 0) if str(item["turn"]).isdigit() else 0,
                                item["id"]))
    total = len(rows)
    page = rows[offset:offset + limit]
    return jsonify({
        "line": which,
        "kind": kind,
        "q": keyword,
        "total": total,
        "offset": offset,
        "limit": limit,
        "entries": page,
    })


@bp.post("/api/showcase/guide/search")
def guide_search():
    """手动查库：只做向量检索，不调模型、不写库。

    阈值按条目类型分开判（原话 0.70 / 提炼 0.60），和注入时一致；阈值外
    的也返回，前端标成「未过阈值」，否则看不到「为什么这条没被用上」。
    """
    data = request.get_json(silent=True) or {}
    query = str(data.get("query") or "").strip()
    if not query or len(query) > _MAX_MESSAGE:
        return jsonify({"error": "检索内容需为 1 到 500 字"}), 400
    try:
        session_id, sess = _session(data.get("session_id"), create=False)
    except ValueError:
        return jsonify({"error": "无效展示会话"}), 400
    try:
        top = min(20, max(1, int(data.get("k") or 8)))
    except (TypeError, ValueError):
        top = 8

    targets = [("kb", _KB_LINE)]
    if sess is not None:
        targets.append(("private", _rag_line(session_id)))

    client = _rag_client()
    if not client.is_ready():
        return jsonify({"error": "检索服务还没就绪，稍等几秒再试"}), 503

    started = time.time()
    hits = []
    for tag, line in targets:
        try:
            result = client.search(
                persona_rag.collection_name(line), query, k=max(12, top * 2),
                timeout=30, allow_start=False)
        except Exception:
            log.exception("展示页查库失败 %s", line)
            continue
        for doc in result.get("docs") or []:
            meta = doc.get("metadata") or {}
            kind = str(meta.get("kind") or "")
            try:
                distance = float(doc.get("distance") or 1.0)
            except (TypeError, ValueError):
                distance = 1.0
            limit = (persona_rag.RAW_MAX_DISTANCE if kind == "raw"
                     else persona_rag.PERSONA_RAG_MAX_DISTANCE)
            content = str(doc.get("content") or "")
            hits.append({
                "line": tag,
                "kind": kind,
                "id": str(doc.get("id") or ""),
                "distance": round(distance, 4),
                "similarity": round(max(0.0, 1.0 - distance), 4),
                "threshold": limit,
                "passed": distance <= limit,
                "chars": len(content),
                "content": content[:1200],
            })
    hits.sort(key=lambda item: item["distance"])
    return jsonify({
        "query": query,
        "took_ms": int((time.time() - started) * 1000),
        "scanned": len(hits),
        "hits": hits[:top],
    })


@bp.get("/api/showcase/comic/replay")
def comic_replay():
    return jsonify(copy.deepcopy(showcase_prompts.COMIC_REPLAY))


# 回放成品图只认契约里登记过的分镜 id，文件必须真实躺在展示素材目录里；
# 目录里的图是生成后逐张过公开审核（image_audit.check）才落盘的。
_COMIC_ASSET_DIR = os.path.join(WEB_DIR, "showcase", "comic")


@bp.get("/api/showcase/comic/asset/<panel_id>")
def comic_asset(panel_id):
    allowed = {p["id"] for p in showcase_prompts.COMIC_REPLAY["panels"]}
    if panel_id not in allowed:
        return jsonify({"error": "未知分镜"}), 404
    local = os.path.join(_COMIC_ASSET_DIR, panel_id + ".jpg")
    if not os.path.isfile(local):
        return jsonify({"error": "素材尚未就绪"}), 404
    # 素材原地覆盖后靠契约里的 ?v= 版本号击穿缓存；这里不再额外缓存 1 小时，
    # 否则换图后浏览器/CDF 仍会拿旧字节，用户以为“成品图没换”。
    return send_file(local, conditional=True, max_age=0)


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
    if job is None:
        # 进程重启后从状态文件恢复的任务：job 句柄已丢失，只能呈现落盘时的
        # 终态（ready/blocked/error），不再能推进审核或排队位置。产物路径已
        # 随 record["images"] 一起落盘，可直读。
        pass
    else:
        if job.entry and not record.get("images"):
            try:
                # 必须把 job 交给 output_images：History 里的 subfolder 可能为空
                # （扮演/批量渠道的老坑），这时要按 job 算出这次提交的输出目录，
                # 否则图明明出了，接口却读成“没有产物”。
                record["images"] = image_jobs.output_images(job.entry, job=job)
            except Exception:
                log.exception("展示页读取生图产物失败 %s", record["tag"])
                record["images"] = []
        # job.done 是 threading.Event，不是布尔值：必须用 is_set() 判断「任务已
        # 终结」，否则 Event 对象永远 truthy，刚入队的任务就被误判成「已完成但没
        # 产物」→ error。job.started 是时间戳或 None，可直接当布尔用。
        if job.error:
            record["status"] = "error"
            # 把真实失败原因透传出来（2026-11-14）：原来写死「生图任务未完成」，
            # 用户等 3 分多钟只看到一句废话，看不到是排队没轮到 / 内存重启 /
            # 超时。image_jobs._reason 已把异常截成一句可读文案。
            record["error"] = "生图未完成：" + image_jobs._reason(job.error)
        elif job.done.is_set() and record.get("images"):
            if record["audit"]["state"] == "pending":
                _start_audit(record)
            elif record["audit"]["state"] == "running":
                record["status"] = "reviewing"
        elif job.done.is_set():
            record["status"] = "error"
            record["error"] = "图片产物暂不可用，请刷新状态或重新生成"
        elif job.started:
            record["status"] = "running"
        else:
            record["status"] = "queued"

    position = None
    if record["status"] == "queued" and job is not None:
        try:
            position = image_jobs.ahead_of(job)
        except Exception:
            log.exception("展示页读取排队位置失败 %s", record["tag"])
    try:
        depth = image_jobs.queue_depth()
    except Exception:
        depth = None
    now = time.time()
    # 提示词契约文案里的渠道要跟着实际走的渠道（2026-11-17 起不止 silver）。
    scene_skill = (record.get("scene") or {}).get("skill") or "silver"
    if record.get("ai"):
        contract = "DeepSeek 扩写（你的原话 → 英文提示词）"
    elif record.get("custom"):
        contract = "自定义提示词（原文直送 %s 工作流）" % scene_skill
    else:
        contract = "预置干净场景；请求体不接受自定义提示词"
    result = {
        "tag": record["tag"],
        "session_id": record["owner"],
        "status": record["status"],
        "scene": copy.deepcopy(record["scene"]),
        "trace": {
            "prompt": record["prompt"],
            "prompt_contract": contract,
            "original": record.get("original") or "",
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
        _persist()
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
        _persist()


@bp.post("/api/showcase/image/submit")
def image_submit():
    data = request.get_json(silent=True) or {}
    # mode=custom 原文直送；mode=ai 走 DeepSeek 扩写（大大怪同款 _translate）。
    # 不传 mode 时仍按老规矩只认 scene_id 白名单、忽略 prompt，原安全边界不变。
    mode = str(data.get("mode") or "").strip()
    # 生图渠道（2026-11-17）：访客在页面上二选一，默认 silver。
    # ⚠️ 只认 `showcase_prompts.IMAGE_SKILLS` 里的 key，其他任何值（空串、
    # 内部渠道 id、别的重渠道）都回落默认档——公网无登录，不能靠改请求体
    # 点名任意工作流。
    skill = showcase_prompts.image_skill(data.get("skill"))
    original = ""
    if mode == "ai":
        brief = str(data.get("prompt") or "").strip()
        if not brief:
            return jsonify({"error": "先写一句你想画什么"}), 400
        if len(brief) > showcase_prompts.CUSTOM_PROMPT_MAX:
            return jsonify({"error": "需求太长了（上限 %d 字）"
                            % showcase_prompts.CUSTOM_PROMPT_MAX}), 400
        try:
            # 局部导入避免启动期加载大模块；按访客选的渠道扩写，让提示词符合
            # 该渠道的写法（krea2 要质量词开头的完整英文串）。
            from app import direct_gen
            translated = direct_gen._translate(brief, [], skill=skill)
        except Exception:
            log.exception("展示页 AI 扩写调用失败")
            return jsonify({"error": "AI 扩写暂时不可用，请稍后重试"}), 503
        expanded = ""
        if isinstance(translated, dict):
            expanded = str(translated.get("prompt") or "").strip()
        if not expanded:
            return jsonify({"error": "AI 没能把这句变成画面，写具体一点再试"
                            "（比如「雨天的旧书店，暖光，没有人」）"}), 400
        scene = showcase_prompts.ai_image_scene(expanded, skill=skill)
        original = brief
    elif mode == "custom":
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "请先输入提示词"}), 400
        if len(prompt) > showcase_prompts.CUSTOM_PROMPT_MAX:
            return jsonify({"error": "提示词太长了（上限 %d 字）"
                            % showcase_prompts.CUSTOM_PROMPT_MAX}), 400
        scene = showcase_prompts.custom_image_scene(prompt, skill=skill)
    else:
        scene = showcase_prompts.image_scene(str(data.get("scene_id") or ""),
                                            skill=skill)
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
        "custom": mode == "custom",
        "ai": mode == "ai",
        "original": original,
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
    _persist()
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


@bp.get("/api/showcase/image/queue")
def image_queue():
    """当前 GPU 队列深度（无需 tag，供「还没提交任务」时预先显示排队情况）。"""
    try:
        depth = image_jobs.queue_depth()
    except Exception:
        log.exception("展示页读取队列深度失败")
        depth = None
    return jsonify({"queue_depth": depth})


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
    _load_state()
    app.register_blueprint(bp)

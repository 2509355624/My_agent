# -*- coding: utf-8 -*-
"""文档问答实验室（`/api/raglab/*`）：把 RAG 的每一步摊开给访客看。

## 为什么单独一个模块

展示页要的是**过程数据**：清洗删了哪些行、大模型怎么切的、每块被压成什么样
的数字、问题凭什么命中这一条。现成的 `app/tools/rag/kb_tools.py` 是给 Agent
用的——`chunk_document` 只回前 5 块预览，`ingest` 只回条数，中间全丢。所以这里
直接和 `RagClient` / `llm.call_llm` 对话，把中间件原样交给前端。

## 和角色扮演那套向量库的关系

**完全隔离**：独立的 daemon、独立的数据目录（`vector_store/chroma_lab`）、
小模型 `bge-small-zh-v1.5`（512 维）。理由（2026-11-15 用户拍板）：bge-m3 加载
十几秒、1024 维、内存大，本机还要跑 ComfyUI 生图，扛不住。小模型实测加载
126 ms（`logs/agent.log` 00:43:56,530 → 00:43:56,656），512 token 上限 → 中文
约 400~500 字，所以 chunk 目标就是 300~450 字，这条约束也正好是演示页要讲的点。

## 切分交给大模型

1 万字一次性出不来（输出约 1.5 万 token，会被服务端上限截在半句话上），按
2400 字分批。提示词原样回给前端展示——这是这个演示页最想让人看到的东西。

## 会话与临时库

`demo_<8位>` 一个会话一个库，最多同时 3 个（超了淘汰最久未用的），30 分钟
无人访问惰性删除（每个请求顺带扫一遍，不起后台线程）。
"""

import json
import logging
import os
import re
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from app import llm
from app.config import DEEPSEEK_MODEL, state_path

log = logging.getLogger("rag_lab")

bp = Blueprint("raglab", __name__)

# ─── 硬上限（2026-11-15）──────────────────────────────
MAX_CHARS = 10000          # 一个演示文档最多 1 万字，超出直接拒
BATCH_CHARS = 2400         # 每次交给大模型的正文字数（输出约 4000 token）
CHUNK_TARGET = (300, 450)  # 小模型 512 token ≈ 中文 400~500 字
CHUNK_HARD_MAX = 500       # 超过就标 over，让页面显示「会被截断」
MAX_CHUNKS = 200           # 再多向量化就会明显拖慢本机
MAX_LIBS = 3               # 同时最多 3 个临时库
TTL_SECONDS = 1800         # 30 分钟无人访问就删
RATE_LIMIT = 12            # 每分钟每会话最多 12 次检索/问答
MAX_ANSWER_TOKENS = 1200

LAB_EMBED_MODEL = os.getenv("RAG_LAB_MODEL", "BAAI/bge-small-zh-v1.5").strip()
LAB_DATA_DIR = os.getenv(
    "RAG_LAB_DATA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "vector_store", "chroma_lab"))

_STATE = state_path("rag_lab_sessions.json")
_LOCK = threading.Lock()
_HITS = {}                 # sid -> [ts, ...]（进程内，重启即清）
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════
# 会话簿
# ══════════════════════════════════════════════════════
def _now():
    return int(time.time())


def _load():
    try:
        with open(_STATE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：会话簿读不出来（%s），按空处理", exc)
    return {"sessions": {}}


def _save(data):
    """落盘。先写 .tmp 再 replace——并发写不会留下半截 JSON。"""
    tmp = _STATE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATE)
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：会话簿写不进去（%s）", exc)


def _client():
    """演示专用向量 daemon：小模型 + 独立目录，绝不碰主 RAG。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from app.rag.rag_client import RagClient
            _CLIENT = RagClient(env_extra={
                "LOCAL_EMBEDDING_MODEL": LAB_EMBED_MODEL,
                "RAG_DATA_DIR": LAB_DATA_DIR,
                "RAG_AUTO_DOWNLOAD": "0",
            })
            _CLIENT.start()
            # 小模型 100 多毫秒，给 60 秒纯粹是给「第一次要下载」留余地
            for _ in range(600):
                if _CLIENT.is_ready():
                    break
                time.sleep(0.1)
            log.info("rag_lab：向量 daemon ready=%s（%s）",
                     _CLIENT.is_ready(), LAB_EMBED_MODEL)
        return _CLIENT


def _gc(data=None):
    """惰性清理：过期会话删库，超过 MAX_LIBS 的按最久未用淘汰。"""
    data = data or _load()
    sessions = data["sessions"]
    now = _now()
    changed = False
    expired = [sid for sid, s in sessions.items()
               if now - int(s.get("last") or 0) > TTL_SECONDS]
    for sid in expired:
        _drop(sessions, sid)
        changed = True
    if len(sessions) > MAX_LIBS:
        order = sorted(sessions.items(), key=lambda kv: int(kv[1].get("last") or 0))
        for sid, _ in order[:len(sessions) - MAX_LIBS]:
            _drop(sessions, sid)
            changed = True
    # 2026-11-15：这里必须用 changed 判断，不能回头再看 len(sessions)——淘汰完
    # 刚好等于上限，条件不再成立，删掉的会话就不会落盘（单测逮到过）。
    if changed:
        _save(data)
    return data


def _drop(sessions, sid):
    rec = sessions.pop(sid, None)
    if not rec:
        return
    try:
        _client().delete_kb(rec.get("kb") or "")
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：删库 %s 失败（%s）", rec.get("kb"), exc)


def _new_session(filename):
    data = _gc()
    sid = uuid.uuid4().hex[:8]
    data["sessions"][sid] = {
        "kb": "demo_" + sid,
        "created": _now(),
        "last": _now(),
        "filename": filename or "未命名文档",
        "chars": 0,
        "chunks": 0,
        "ingested": False,
    }
    _save(data)
    return sid, data["sessions"][sid]


def _touch(sid):
    data = _gc()
    rec = data["sessions"].get(sid)
    if rec is None:
        return None
    rec["last"] = _now()
    _save(data)
    return rec


def _rate_ok(sid):
    """每会话每分钟 12 次检索/问答。内存计数，进程重启即清。"""
    now = time.time()
    bucket = [t for t in _HITS.get(sid, []) if now - t < 60]
    if len(bucket) >= RATE_LIMIT:
        _HITS[sid] = bucket
        return False
    bucket.append(now)
    _HITS[sid] = bucket
    return True


# ══════════════════════════════════════════════════════
# ① 清洗
# ══════════════════════════════════════════════════════
_PAGE_RES = (
    re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$"),
    re.compile(r"^\s*第\s*\d{1,4}\s*页(?:\s*[/／,，]\s*共?\s*\d{1,4}\s*页)?\s*$"),
    re.compile(r"^\s*\d{1,4}\s*[/／]\s*\d{1,4}\s*$"),
)
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_BLANK = re.compile(r"^\s*$")


def clean_text(raw):
    """规则清洗。返回 (clean_text, report)。

    只做**确定性**的事：去零宽、去行尾空白、去页码行、去重复页眉页脚、
    压缩连续空行。语义判断一律不在这里做——那是下一步大模型的活。
    """
    raw = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    report = {
        "raw_chars": len(raw),
        "removed": {"zero_width": 0, "page": 0, "repeat": 0,
                    "blank": 0, "trimmed": 0},
        "samples": [],
    }

    zero = len(_ZERO_WIDTH.findall(raw))
    if zero:
        report["removed"]["zero_width"] = zero
        raw = _ZERO_WIDTH.sub("", raw)

    lines = raw.split("\n")
    trimmed = 0
    step1 = []
    for line in lines:
        stripped = line.rstrip()
        if stripped != line:
            trimmed += 1
        step1.append(stripped)
    report["removed"]["trimmed"] = trimmed

    # 重复短行：长度 ≤ 40 且出现 ≥ 3 次 → 页眉/页脚/水印
    counter = {}
    for line in step1:
        s = line.strip()
        if s and len(s) <= 40:
            counter[s] = counter.get(s, 0) + 1
    repeats = {s for s, n in counter.items() if n >= 3}

    kept = []
    for line in step1:
        s = line.strip()
        if s and any(p.match(s) for p in _PAGE_RES):
            report["removed"]["page"] += 1
            if len(report["samples"]) < 12:
                report["samples"].append({"line": s, "reason": "页码行"})
            continue
        if s and s in repeats:
            report["removed"]["repeat"] += 1
            if len(report["samples"]) < 12:
                report["samples"].append({"line": s, "reason": "重复出现 ≥3 次的短行（页眉/页脚/水印）"})
            continue
        kept.append(line)

    # 连续空行压成一行
    out = []
    blank_run = 0
    for line in kept:
        if _BLANK.match(line):
            blank_run += 1
            if blank_run > 1:
                report["removed"]["blank"] += 1
                continue
        else:
            blank_run = 0
        out.append(line)

    text = "\n".join(out).strip()
    report["clean_chars"] = len(text)
    report["kept_lines"] = sum(1 for line in out if line.strip())
    report["drop_chars"] = report["raw_chars"] - report["clean_chars"]
    return text, report


# ══════════════════════════════════════════════════════
# ② 大模型切分
# ══════════════════════════════════════════════════════
SPLIT_SYSTEM = """你是文档切分器。把用户给的正文切成若干语义完整的片段，供向量检索使用。

1. 每段目标 350 字左右，一般不要低于 250 字；硬上限 500 字（超过会被向量模型截断，尾部直接丢失）。
2. 只有在「主题确实变了」或「接着合并会超过 500 字」时才断开；同一个标题下讲同一件事的
   几段要合并成一块，不要一句话、两句话就切一刀。
3. 一段只讲一件事；不把两个主题塞进一段，也不把一句话拆到两段。
4. 原文照抄，不改写、不总结、不补内容。
5. 段落开头若是小标题，把标题留在该段第一行。
6. 表格按行拆，表头复制到每一段。
7. 只输出 JSON 数组，不要任何解释、不要 Markdown 代码块。

输出格式：
[{"heading": "所属小节", "content": "片段原文", "why": "为什么在这里断开（≤20字）"}]
"""


def _split_batches(text, limit=BATCH_CHARS):
    """按段落聚合到 limit 附近，尽量在空行处断开。"""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    batches, cur = [], ""
    for para in paras:
        if cur and len(cur) + len(para) + 2 > limit:
            batches.append(cur)
            cur = para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur:
        batches.append(cur)
    # 单段本身超 limit（比如整篇没空行）：按句子再切一刀
    fixed = []
    for b in batches:
        if len(b) <= limit * 1.6:
            fixed.append(b)
            continue
        buf = ""
        for sent in re.split(r"(?<=[。！？；\n])", b):
            if buf and len(buf) + len(sent) > limit:
                fixed.append(buf)
                buf = sent
            else:
                buf += sent
        if buf.strip():
            fixed.append(buf)
    return fixed


def _extract_json_array(reply):
    """从模型回复里抠出 JSON 数组。失败抛 ValueError（带结尾原文，便于排查）。"""
    text = (reply or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 数组，回复结尾：" + text[-160:])
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 解析失败（%s），回复结尾：%s"
                         % (exc, text[-160:])) from exc
    if not isinstance(data, list):
        raise ValueError("返回的不是数组，回复结尾：" + text[-160:])
    return data


def _call_split_llm(batch, no, total):
    """单独一层是为了单测能直接 patch 掉，不打真模型。"""
    user = "以下是第 %d/%d 批正文（本批 %d 字）：\n\n%s" % (no, total, len(batch), batch)
    return llm.call_llm(
        [{"role": "system", "content": SPLIT_SYSTEM},
         {"role": "user", "content": user}],
        provider="deepseek", model=DEEPSEEK_MODEL, strict=True,
        max_tokens=4096, temperature=0.2, timeout=180,
    )


def split_with_llm(text):
    """分批切分。返回 (chunks, batches_debug)。"""
    batches = _split_batches(text)
    total = len(batches)
    chunks, debug = [], []
    for i, batch in enumerate(batches, 1):
        started = time.time()
        reply = _call_split_llm(batch, i, total)
        secs = round(time.time() - started, 2)
        try:
            raw = _extract_json_array(reply)
            err = ""
        except ValueError as exc:
            raw, err = [], str(exc)
        debug.append({
            "no": i, "chars": len(batch), "secs": secs,
            "reply_chars": len(reply or ""), "error": err,
            "raw_tail": (reply or "")[-600:],
            "count": len(raw),
        })
        for idx, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            chunks.append({
                "id": "b%d_%02d" % (i, idx),
                "heading": str(item.get("heading") or "").strip(),
                "content": content,
                "chars": len(content),
                "why": str(item.get("why") or "").strip(),
                # 只标事实：超了就是超了，改不改由页面告诉用户，不替模型重切
                "over": len(content) > CHUNK_HARD_MAX,
            })
    return chunks, debug


# ══════════════════════════════════════════════════════
# ③ 检索 / ④ 回答
# ══════════════════════════════════════════════════════
def _hits_of(rec, question, k=4):
    res = _client().search(rec["kb"], question, k=k)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "检索失败")
    out = []
    for i, doc in enumerate(res.get("docs") or [], 1):
        dist = float(doc.get("distance") or 0.0)
        out.append({
            "no": i,
            "id": doc.get("id") or "",
            "content": (doc.get("content") or "")[:800],
            "chars": len(doc.get("content") or ""),
            "heading": (doc.get("metadata") or {}).get("heading", ""),
            "distance": round(dist, 4),
            "score": round(1.0 - dist, 4),
        })
    return out


ANSWER_SYSTEM = """你在回答用户关于一份文档的问题。

- 只能依据下面给出的资料作答；资料里没有的，直接说「文档里没有提到」，不要猜。
- 每条论断后面用 [1] [2] 标注来源编号。
- 用中文，讲清楚，不要罗列无意义的条目。"""


def _build_answer_prompt(question, hits):
    lines = []
    for h in hits:
        head = ("· " + h["heading"]) if h.get("heading") else ""
        lines.append("[%d]%s\n%s" % (h["no"], head, h["content"]))
    return ("资料：\n\n" + "\n\n".join(lines) +
            "\n\n问题：%s" % question)


# ══════════════════════════════════════════════════════
# 路由
# ══════════════════════════════════════════════════════
@bp.post("/api/raglab/clean")
def api_clean():
    body = request.get_json(silent=True) or {}
    raw = str(body.get("text") or "")
    filename = str(body.get("filename") or "").strip()
    if not raw.strip():
        return jsonify({"ok": False, "error": "正文是空的"}), 400
    if len(raw) > MAX_CHARS:
        return jsonify({"ok": False,
                        "error": "演示只支持 %d 字以内（这份 %d 字）"
                                 % (MAX_CHARS, len(raw))}), 400
    started = time.time()
    text, report = clean_text(raw)
    sid, rec = _new_session(filename)
    data = _load()
    if sid in data["sessions"]:
        data["sessions"][sid]["chars"] = len(text)
        _save(data)
    return jsonify({
        "ok": True, "sid": sid, "kb": rec["kb"], "filename": rec["filename"],
        "text": text, "report": report,
        "secs": round(time.time() - started, 3),
        "limits": {"max_chars": MAX_CHARS, "chunk_target": list(CHUNK_TARGET),
                   "hard_max": CHUNK_HARD_MAX, "max_chunks": MAX_CHUNKS,
                   "max_libs": MAX_LIBS, "ttl": TTL_SECONDS},
    })


@bp.post("/api/raglab/split")
def api_split():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    sid = str(body.get("sid") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "正文是空的"}), 400
    if len(text) > MAX_CHARS:
        return jsonify({"ok": False, "error": "正文超过 %d 字" % MAX_CHARS}), 400
    started = time.time()
    try:
        chunks, batches = split_with_llm(text)
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：切分失败 %s", exc)
        return jsonify({"ok": False, "error": "切分失败：%s" % exc}), 502
    if len(chunks) > MAX_CHUNKS:
        return jsonify({"ok": False,
                        "error": "切出 %d 块，超过上限 %d（换小一点的文档）"
                                 % (len(chunks), MAX_CHUNKS)}), 400
    if sid:
        data = _load()
        if sid in data["sessions"]:
            data["sessions"][sid]["chunks"] = len(chunks)
            data["sessions"][sid]["last"] = _now()
            _save(data)
    return jsonify({
        "ok": True, "chunks": chunks, "batches": batches,
        "prompt": SPLIT_SYSTEM,
        "secs": round(time.time() - started, 2),
        "stats": {
            "count": len(chunks),
            "chars": sum(c["chars"] for c in chunks),
            "over": sum(1 for c in chunks if c["over"]),
            "avg": round(sum(c["chars"] for c in chunks) / len(chunks), 1) if chunks else 0,
        },
    })


@bp.post("/api/raglab/ingest")
def api_ingest():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    chunks = body.get("chunks") or []
    rec = _touch(sid)
    if rec is None:
        return jsonify({"ok": False, "error": "会话已过期，重新上传文档"}), 404
    if not chunks:
        return jsonify({"ok": False, "error": "没有要入库的块"}), 400
    if len(chunks) > MAX_CHUNKS:
        return jsonify({"ok": False, "error": "块数超过上限"}), 400

    entries = []
    for i, c in enumerate(chunks[:MAX_CHUNKS], 1):
        content = str(c.get("content") or "").strip()
        if not content:
            continue
        entries.append({
            "id": "%s_%s" % (sid, c.get("id") or i),
            "content": content,
            "metadata": {
                "source": rec.get("filename") or "",
                "heading": str(c.get("heading") or "")[:80],
                "idx": i,
                "chars": len(content),
            },
        })
    started = time.time()
    res = _client().ingest(rec["kb"], entries)
    secs = round(time.time() - started, 2)
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error") or "入库失败"}), 502
    data = _load()
    if sid in data["sessions"]:
        data["sessions"][sid]["ingested"] = True
        data["sessions"][sid]["chunks"] = len(entries)
        data["sessions"][sid]["last"] = _now()
        _save(data)
    return jsonify({
        "ok": True, "kb": rec["kb"], "count": res.get("count", 0),
        "dim": res.get("dim", 0), "preview": res.get("preview") or [],
        "model": LAB_EMBED_MODEL, "secs": secs,
    })


@bp.post("/api/raglab/query")
def api_query():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    question = str(body.get("question") or "").strip()
    k = int(body.get("k") or 4)
    rec = _touch(sid)
    if rec is None:
        return jsonify({"ok": False, "error": "会话已过期"}), 404
    if not question:
        return jsonify({"ok": False, "error": "问题是空的"}), 400
    if not rec.get("ingested"):
        return jsonify({"ok": False, "error": "文档还没入库"}), 400
    if not _rate_ok(sid):
        return jsonify({"ok": False, "error": "问得太快了，喘口气再来"}), 429
    started = time.time()
    try:
        hits = _hits_of(rec, question, k=max(1, min(k, 10)))
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"ok": False, "error": "检索失败：%s" % exc}), 502
    return jsonify({"ok": True, "hits": hits,
                    "secs": round(time.time() - started, 3)})


@bp.post("/api/raglab/answer")
def api_answer():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    question = str(body.get("question") or "").strip()
    k = int(body.get("k") or 4)
    rec = _touch(sid)
    if rec is None:
        return jsonify({"ok": False, "error": "会话已过期"}), 404
    if not question:
        return jsonify({"ok": False, "error": "问题是空的"}), 400
    if not rec.get("ingested"):
        return jsonify({"ok": False, "error": "文档还没入库"}), 400
    if not _rate_ok(sid):
        return jsonify({"ok": False, "error": "问得太快了，喘口气再来"}), 429

    started = time.time()
    try:
        hits = _hits_of(rec, question, k=max(1, min(k, 10)))
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"ok": False, "error": "检索失败：%s" % exc}), 502
    if not hits:
        return jsonify({"ok": True, "hits": [], "answer": "文档里没有检索到相关内容。",
                        "prompt": "", "model": DEEPSEEK_MODEL, "secs": round(time.time() - started, 2),
                        "usage": {}})

    prompt = _build_answer_prompt(question, hits)
    try:
        answer = llm.call_llm(
            [{"role": "system", "content": ANSWER_SYSTEM},
             {"role": "user", "content": prompt}],
            provider="deepseek", model=DEEPSEEK_MODEL, strict=True,
            max_tokens=MAX_ANSWER_TOKENS, temperature=0.3, timeout=120,
        )
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：回答失败 %s", exc)
        return jsonify({"ok": False, "error": "模型调用失败：%s" % exc}), 502

    try:
        usage = llm.current_usage() or {}
    except Exception:                                   # noqa: BLE001
        usage = {}
    return jsonify({"ok": True, "hits": hits, "answer": answer,
                    "prompt": prompt, "system": ANSWER_SYSTEM,
                    "model": DEEPSEEK_MODEL, "usage": usage,
                    "secs": round(time.time() - started, 2)})


@bp.post("/api/raglab/reset")
def api_reset():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "缺少 sid"}), 400
    data = _load()
    existed = sid in data["sessions"]
    _drop(data["sessions"], sid)
    _save(data)
    _HITS.pop(sid, None)
    return jsonify({"ok": True, "dropped": existed})


_WARM_LOCK = threading.Lock()
_WARM_ONCE = [False]


def _warm_bg():
    try:
        _client()
        log.info("rag_lab：预热完成，向量 daemon 已就绪")
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：预热失败（%s）", exc)


@bp.post("/api/raglab/warm")
def api_warm():
    """页面打开就先把模型拉起来。

    实测冷启动 12.2 秒（`RagClient.start()` + sentence-transformers 导入），
    等用户真点上传再加载，第一步就要干等。这里起一个后台线程，接口立刻返回，
    用户读说明、选文件的这段时间正好覆盖掉。
    """
    with _WARM_LOCK:
        if not _WARM_ONCE[0]:
            _WARM_ONCE[0] = True
            threading.Thread(target=_warm_bg, daemon=True).start()
    ready = bool(_CLIENT is not None and _CLIENT.is_ready())
    return jsonify({"ok": True, "ready": ready, "model": LAB_EMBED_MODEL})


@bp.get("/api/raglab/state")
def api_state():
    """页面刷新后用 sid 认回自己的库；会话没了就返回 ok=false。"""
    sid = str(request.args.get("sid") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "缺少 sid"}), 400
    data = _gc()
    rec = data["sessions"].get(sid)
    if not rec:
        return jsonify({"ok": False, "error": "会话已过期"})
    return jsonify({"ok": True, "sid": sid, "kb": rec["kb"],
                    "filename": rec["filename"], "chars": rec.get("chars", 0),
                    "chunks": rec.get("chunks", 0),
                    "ingested": bool(rec.get("ingested")),
                    "ttl": TTL_SECONDS})

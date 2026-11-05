"""角色扮演的长期记忆向量库（2026-11-05）。

## 为什么不是存原文

参考项目 `picture_prompt_produce` 的做法：**每 5 次 AI 回复**把最近一批
对话交给「记忆提炼器」，只留下可以长期复用的事实，再把事实入库。

直接存「用户：…… / 她：……」的原文有两个根本问题：

  · 检索命中的是一段旧对话，演员会照着复述，容易把当前话题带跑；
  · 寒暄、重复、临场玩笑全混在里面，库里 90% 是垃圾。

所以这里也只存**提炼后的事实**：

    {"about": "user|character|shared", "content": "用户：……",
     "topics": "…", "primary_emotion": "…", "open_threads": "…",
     "turn_start": 3, "turn_end": 5, "source": "batch_summary_v3"}

归属（about）是关键：用户亲口说的才算用户事实；角色说的不能写成用户事实；
只有双方明确同意的计划才算 shared。检索时按归属标注，并带一条「旧事实
优先于近期提及」的裁决规则。

## 隔离

一条会话线（用户 + 角色 + 平台）一个 collection：

    persona_<sha256(会话线)[:16]>

清空存档只删这一条线，别人的向量库不受影响。

## 慢加载

CPU 小模型冷启动十几秒，绝不能在聊天轮里同步等：
  · 提炼和入库都在后台线程/队列里做；
  · 检索只在 daemon ready 时发生，否则这一轮不带回忆。
"""
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time

from app.config import (
    PERSONA_RAG, PERSONA_RAG_DATA_DIR, PERSONA_RAG_INGEST_EVERY,
    PERSONA_RAG_K, PERSONA_RAG_MAX_DISTANCE, PERSONA_RAG_MODEL,
    PERSONA_RAG_TIMEOUT, state_path,
)

log = logging.getLogger("persona_rag")

# 单元测试里不拉真 daemon（小模型冷启动十几秒 + ~1GB 内存，纯属浪费）：
# 测试进程一定 import 了 unittest；生产进程不会。
_TESTING = "unittest" in sys.modules

_COLL_PREFIX = "persona_"
_SOURCE = "batch_summary_v3"
_LOCK = threading.Lock()
_BLOCKED = set()          # 正在删库的线：删完之前不准检索，避免旧回忆漏出来
_QUEUE = queue.Queue()
_WORKER = None
_CLIENT = None
_FAIL_UNTIL = 0.0
_WARMED = False

# 提炼器提示词。原则和参考项目一致：先钉死「谁说的」，再只记长期有用的事实。
_MEMORY_PROMPT = """你是角色扮演对话的记忆提炼器。把这一批对话提炼成「以后能直接拿来用的事实」，**不要保留原对话**。

【对话（第 {start}–{end} 次回复）】
{dialogue}

【最重要：发言归属】
1. 「用户：」开头的话只能记成用户事实；「角色：」开头的话只能记成角色事实。
2. 角色的提问、猜测、想象、提议、自我介绍，一律不算用户观点。
3. 禁止把角色说过的喜好/经历写成「用户喜欢……」「用户会……」。
4. 只有用户亲口说出的偏好、经历、承诺、关系信息，才能写 about="user"。
5. 角色亲口说出的设定、喜好、承诺，写 about="character"。
6. 只有双方都明确同意的计划，才写 about="shared"。

【只记这些】
- 偏好/厌恶、亲历事件、承诺与约定、关系变化、重要物品/地点、明确未解决的线头。

【不要记】
- 寒暄、重复、没有推进的日常、临场玩笑。
- 出图 tags、穿着、镜头、场景描写、格式元数据。
- 角色单方面编造、双方没有确认的设定，不要当成事实。

【格式】
- 最多 4 条；每条不超过 120 字；必须以「用户：」「角色：」「双方：」开头。
- 确实没有值得记的内容 → skip=true、entries=[]。
- 只输出 JSON：
{{
  "skip": false,
  "entries": [
    {{"about": "user", "content": "用户：……",
      "topics": ["主题"], "primaryEmotion": "", "openThreads": ["没说开的事"]}},
    {{"about": "character", "content": "角色：……",
      "topics": [], "primaryEmotion": "", "openThreads": []}}
  ]
}}
"""


def enabled():
    return bool(PERSONA_RAG) and not _TESTING


def enabled_for(line):
    """这条线要不要用长期记忆。

    游客固定只玩 30 轮，存了也很快过期，纯属白占内存和磁盘——直接跳过。
    登录用户 / QQ 私聊 / 网页线照常。
    """
    return enabled() and not str(line or "").startswith("guest_")


def collection_name(line):
    """一条会话线 → 一个 collection 名。哈希是单向的，不把 QQ/角色 id 写进路径。"""
    raw = str(line or "").encode("utf-8")
    return _COLL_PREFIX + hashlib.sha256(raw).hexdigest()[:16]


def _client():
    global _CLIENT
    if _CLIENT is None:
        from app.rag.rag_client import RagClient
        _CLIENT = RagClient(env_extra={
            "LOCAL_EMBEDDING_MODEL": PERSONA_RAG_MODEL,
            "RAG_EMBED_DEVICE": "cpu",
            "RAG_DATA_DIR": PERSONA_RAG_DATA_DIR,
            "RAG_AUTO_DOWNLOAD": "0",
        })
    return _CLIENT


def _ensure_worker():
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_run, name="persona-rag", daemon=True)
        _WORKER.start()


def _enqueue(kind, line, payload=None):
    _QUEUE.put((kind, line, payload))
    _ensure_worker()


def warm():
    """把 daemon 在后台拉起来（幂等）。第一次扮演时调一次就够。"""
    global _WARMED
    if not enabled() or _WARMED:
        return
    with _LOCK:
        if _WARMED:
            return
        _WARMED = True
    _enqueue("warm", "")


# ── 待提炼缓冲（跨进程重启不丢）────────────────────────
#
# 每轮先攒着，攒够 PERSONA_RAG_INGEST_EVERY 轮才提炼一次。
# 文件结构：{line: {"seq": 12, "items": [{"turn": 11, "user": ..., "assistant": ...}]}}

_PENDING_LOCK = threading.Lock()


def _pending_path():
    return state_path("persona_rag_pending.json")


def _pending_load():
    try:
        with open(_pending_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _pending_save(data):
    path = _pending_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        log.exception("保存待提炼记忆失败")


def pending_for(line):
    """测试/排查用：某条线当前攒着多少轮。"""
    with _PENDING_LOCK:
        rec = _pending_load().get(str(line or "")) or {}
        return dict(rec) if isinstance(rec, dict) else {}


def remember(line, user_text, reply, custom=None):
    """这一轮先攒进缓冲；够一批了才交给提炼器。

    只收「用户可见的话 + 她的回复」。用户的「想」由调用方过滤掉，
    这里不做二次判断。
    """
    if not enabled_for(line):
        return
    line = str(line or "").strip()
    reply = str(reply or "").strip()
    if not line or not reply:
        return
    batch = None
    with _PENDING_LOCK:
        data = _pending_load()
        rec = data.get(line)
        if not isinstance(rec, dict):
            rec = {"seq": 0, "items": []}
        rec["seq"] = int(rec.get("seq") or 0) + 1
        items = rec.get("items") if isinstance(rec.get("items"), list) else []
        items.append({"turn": rec["seq"],
                      "user": str(user_text or "").strip(),
                      "assistant": reply})
        rec["items"] = items
        if len(items) >= PERSONA_RAG_INGEST_EVERY:
            batch = list(items)
            rec["items"] = []
            rec["last_batch"] = [int(batch[0].get("turn") or 0),
                                 int(batch[-1].get("turn") or 0)]
        data[line] = rec
        _pending_save(data)
    if batch:
        _enqueue("distill", line, {"turns": batch, "custom": custom})


def forget_last(line):
    """回退上一轮：撤掉还没提炼的那一轮；已经入过库的删那一条批次。

    批次的 id 是 `start-end-idx`，所以只要记住最近一批的范围就够了。
    """
    if not enabled_for(line):
        return
    line = str(line or "").strip()
    if not line:
        return
    batch = None
    with _PENDING_LOCK:
        data = _pending_load()
        rec = data.get(line)
        if isinstance(rec, dict):
            items = rec.get("items") if isinstance(rec.get("items"), list) else []
            if items:
                items.pop()                       # 最新那轮还没提炼
                rec["items"] = items
                rec["seq"] = max(0, int(rec.get("seq") or 0) - 1)
                data[line] = rec
                _pending_save(data)
                return
            lb = rec.get("last_batch")
            if isinstance(lb, list) and len(lb) == 2:
                batch = (int(lb[0]), int(lb[1]))
    if batch:
        _enqueue("forget", line, {"start": batch[0], "end": batch[1]})


def drop_line(line):
    """清空存档：删这一条线的整个 collection + 待提炼缓冲。

    先 blocked 再入队：删库要等 daemon，这期间用户立刻开口也不能检索旧内容。
    """
    if not enabled_for(line):
        return
    line = str(line or "").strip()
    if not line:
        return
    with _PENDING_LOCK:
        data = _pending_load()
        if data.pop(line, None) is not None:
            _pending_save(data)
    with _LOCK:
        _BLOCKED.add(line)
    _enqueue("drop", line)


def retrieve(line, query, k=None):
    """按当前用户的话找几条提炼过的事实；daemon 没 ready 就返回空。"""
    if not enabled_for(line):
        return []
    line = str(line or "").strip()
    query = str(query or "").strip()
    if not line or not query:
        return []
    with _LOCK:
        if line in _BLOCKED:
            return []
    client = _CLIENT
    if client is None or not client.is_ready():
        return []
    try:
        r = client.search(collection_name(line), query,
                          k=k or PERSONA_RAG_K, timeout=PERSONA_RAG_TIMEOUT,
                          allow_start=False,
                          where={"source": _SOURCE})
    except Exception:                                    # noqa: BLE001
        log.exception("回忆检索失败 %s", line)
        return []
    if not r.get("ok"):
        return []
    out = []
    for d in r.get("docs") or []:
        try:
            dist = float(d.get("distance", 1.0))
        except (TypeError, ValueError):
            dist = 1.0
        if dist <= PERSONA_RAG_MAX_DISTANCE:
            out.append(d)
    return out[:k or PERSONA_RAG_K]


def build_block(line, query, k=None):
    """拼成能直接塞进系统提示词的一段；没命中返回空串。"""
    docs = retrieve(line, query, k=k)
    if not docs:
        return ""
    who = {"user": "用户事实", "character": "角色言行", "shared": "双方约定"}
    lines = ["【长期记忆 · 从过去提炼出的事实，不是原对话】"]
    for d in docs:
        meta = d.get("metadata") or {}
        about = who.get(str(meta.get("about") or ""), "记忆")
        start = meta.get("turn_start")
        end = meta.get("turn_end")
        rng = "第 %s–%s 次回复" % (start, end) if start and end else ""
        topics = meta.get("topics") or ""
        head = "[%s%s%s]" % (
            about, "｜" + rng if rng else "", "｜主题：" + topics if topics else "")
        text = " ".join(str(d.get("content") or "").split())
        if not text:
            continue
        if len(text) > 160:
            text = text[:160] + "…"
        lines.append(head + "\n" + text)
    if len(lines) == 1:
        return ""
    lines.append("【用法】用户问起自己/双方/角色的既有事实时，以此为准；"
                 "最近对话只是提到某个话题，不等于偏好改变——旧事实只能补充，"
                 "不能被近期闲聊覆盖。")
    return "\n".join(lines)


def _run():
    while True:
        kind, line, payload = _QUEUE.get()
        try:
            _process(kind, line, payload)
        except Exception:                                # noqa: BLE001
            log.exception("角色扮演 RAG 后台任务失败：%s", kind)
        finally:
            _QUEUE.task_done()


def _process(kind, line, payload):
    global _FAIL_UNTIL
    if not enabled():
        return
    if time.time() < _FAIL_UNTIL:
        return
    client = _client()
    if not client.is_ready():
        if not client.start():
            _FAIL_UNTIL = time.time() + 300
            log.warning("角色扮演 RAG 启动失败，5 分钟内不再重试")
            return
    if kind == "warm":
        return
    coll = collection_name(line)
    if kind == "distill":
        turns = (payload or {}).get("turns") or []
        custom = (payload or {}).get("custom")
        entries = distill(turns, custom)
        if not entries:
            return
        r = client.ingest(coll, entries)
        if not r.get("ok"):
            log.warning("记忆入库失败 %s：%s", coll, r.get("error"))
        return
    if kind == "drop":
        r = client.delete_kb(coll)
        if r.get("ok"):
            with _LOCK:
                _BLOCKED.discard(line)
        else:
            log.warning("记忆清库失败 %s：%s", coll, r.get("error"))
        return
    if kind == "forget":
        start = int((payload or {}).get("start") or 0)
        end = int((payload or {}).get("end") or 0)
        if start and end:
            for idx in range(4):
                entry_id = "%d-%d-%d" % (start, end, idx)
                r = client.delete_entry(coll, entry_id)
                if not r.get("ok"):
                    log.warning("记忆删除失败 %s/%s：%s", coll, entry_id,
                                r.get("error"))


def _format_dialogue(turns):
    parts = []
    for it in turns:
        t = it.get("turn")
        u = str(it.get("user") or "").strip()
        a = str(it.get("assistant") or "").strip()
        if u:
            parts.append("----- 第%s次回复 · 用户 -----\n用户：%s" % (t, u))
        if a:
            parts.append("----- 第%s次回复 · 角色 -----\n角色：%s" % (t, a))
    return "\n\n".join(parts)


def distill(turns, custom=None):
    """调 LLM 提炼一批对话，返回可入库的 entries；失败/空批返回 []。"""
    turns = [t for t in (turns or []) if isinstance(t, dict)]
    if not turns:
        return []
    start = int(turns[0].get("turn") or 0)
    end = int(turns[-1].get("turn") or 0)
    prompt = _MEMORY_PROMPT.format(start=start, end=end,
                                   dialogue=_format_dialogue(turns))
    from app import llm
    if custom:
        llm.set_custom_override(custom)
    try:
        raw = llm.call_llm([{"role": "user", "content": prompt}],
                           temperature=0.2, max_tokens=1200)
    except Exception:                                    # noqa: BLE001
        log.exception("记忆提炼调用失败 %s-%s", start, end)
        return []
    finally:
        if custom:
            llm.set_custom_override(None)
    return parse_memory_json(raw, start, end)


def parse_memory_json(raw, start, end):
    """解析提炼器输出 → Chroma entries。坏 JSON / 空内容一律返回 []。"""
    text = str(raw or "")
    lo = text.find("{")
    hi = text.rfind("}")
    if lo < 0 or hi <= lo:
        return []
    try:
        data = json.loads(text[lo:hi + 1])
    except ValueError:
        log.warning("记忆提炼输出不是 JSON：%r", text[:120])
        return []
    if not isinstance(data, dict) or data.get("skip"):
        return []
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return []
    out = []
    for i, e in enumerate(raw_entries[:4]):
        if not isinstance(e, dict):
            continue
        content = str(e.get("content") or "").strip()
        if not content:
            continue
        about = _norm_about(e.get("about"), content)
        content = _ensure_prefix(about, content)
        if len(content) > 160:                    # 提示词说 120，留点余量
            content = content[:160]
        topics = e.get("topics") if isinstance(e.get("topics"), list) else []
        opens = e.get("openThreads") if isinstance(e.get("openThreads"), list) else []
        out.append({
            "id": "%d-%d-%d" % (start, end, i),
            "content": content,
            "metadata": {
                "about": about,
                "turn_start": start,
                "turn_end": end,
                "topics": ",".join(str(x) for x in topics if x)[:120],
                "primary_emotion": str(e.get("primaryEmotion") or "")[:60],
                "open_threads": ",".join(str(x) for x in opens if x)[:160],
                "source": _SOURCE,
            },
        })
    return out


def _norm_about(about, content):
    a = str(about or "").strip().lower()
    if a in ("user", "character", "shared"):
        return a
    c = str(content or "")
    if re.match(r"^(角色|她)[：:]", c) or c.startswith("【角色】"):
        return "character"
    if re.match(r"^(双方|我们)[：:]", c) or c.startswith("【双方】"):
        return "shared"
    return "user"


def _ensure_prefix(about, content):
    text = str(content or "").strip()
    if re.match(r"^(用户|角色|双方)[：:]", text) or \
            re.match(r"^【(用户|角色|双方)】", text):
        return text
    return {"user": "用户：", "character": "角色：",
            "shared": "双方："}.get(about, "用户：") + text

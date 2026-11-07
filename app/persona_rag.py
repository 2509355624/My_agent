"""角色扮演的长期记忆向量库（2026-11-05，2026-11-07 加原文层）。

## 两层：提炼事实 + 原文片段

**事实层**（`_SOURCE`）：**每 5 次 AI 回复**把最近一批对话交给「记忆提炼器」，
只留下可以长期复用的事实，再把事实入库。入库形状：

    {"about": "user|character|shared", "content": "用户：……",
     "topics": "…", "primary_emotion": "…", "open_threads": "…",
     "turn_start": 3, "turn_end": 5, "source": "batch_summary_v3"}

归属（about）是关键：用户亲口说的才算用户事实；角色说的不能写成用户事实；
只有双方明确同意的计划才算 shared。检索时按归属标注，并带一条「旧事实
优先于近期提及」的裁决规则。

**原文层**（`_RAW_SOURCE`，2026-11-07 加）：每批顺手把这一批的对话原文
也入库，一条消息一条，按 `RAW_MAX_CHARS` 截断。

为什么要加：提炼后的批次叙事只有 200~300 字，细节全丢了。用户问「上次
你说的那句」「我们当时是怎么说好的」时，演员手里只有摘要，回答只能靠猜。
原文条目专门补这个洞——它**只用来说明细节**，注入块里写明「当前这一拍
接着往下演，正在发生的比旧话更重要」，避免演员照着旧对话复述。

原来担心的两个问题（演员复述旧对话、库里全是寒暄垃圾）用三件事压住：
阈值放宽但条数限死（3 条）、注入时再压到 260 字、块内明确它只是细节来源。

## 隔离

一条会话线（用户 + 角色 + 平台）一个 collection：

    persona_<sha256(会话线)[:16]>

清空存档只删这一条线，别人的向量库不受影响。

## 慢加载

CPU 小模型冷启动十几秒，绝不能在聊天轮里同步等：
  · 提炼、原文入库都在后台线程/队列里做；
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
    PERSONA_MODEL, PERSONA_PROVIDER,
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

# 原文层（2026-11-07）。和事实层并存，不替代它：
#   · 入库截 400 字——`bge-small-zh-v1.5` 上下文 512 token，再长会被截；
#   · 阈值 0.70 比事实的 0.60 宽——原文句子长、复述多，距离天然更远；
#   · 每轮最多 3 条、每条注入时压到 260 字、总预算 900 字。
_RAW_SOURCE = "raw_msg_v1"
RAW_MAX_CHARS = 400
RAW_MAX_DISTANCE = 0.70
RAW_BLOCK_ITEMS = 3
RAW_ITEM_CHARS = 260
RAW_BLOCK_CHARS = 900
# 「昨天/上次/之前」这类问法：检索时放宽阈值、多取几条（2026-11-05）。
_TIME_QUERY = re.compile(r"昨天|昨日|昨晚|昨夜|前一天|前天|上次|之前")
_LOCK = threading.Lock()
_BLOCKED = set()          # 正在删库的线：删完之前不准检索，避免旧回忆漏出来
_QUEUE = queue.Queue()
_WORKER = None
_CLIENT = None
_FAIL_UNTIL = 0.0
_WARMED = False

# 提炼器提示词（2026-11-06 改为「批次叙事 + 原子事实」两层）。
# 之前的版本只让模型抽 4 条碎事实，一批 5 轮最多约 480 字，既没有整体经过、
# 也没有角色名和天数；用户问「昨天太招摇」时手里只有零散条目。
_MEMORY_PROMPT = """你是角色扮演对话的记忆提炼器。把这一批对话整理成两层：
1) 一段「批次叙事」——以后问「昨天/上次」时能直接拿来回答「那时发生了什么」；
2) 最多 4 条「原子事实」——偏好、承诺、关系变化、重要物品/地点、未解决线头。

【对话（第 {start}–{end} 次回复）】
{dialogue}

【批次叙事怎么写】
- 200–300 字，按时间顺序写清：地点/场合 → 用户做了什么、说了什么 →
  {name}怎么反应、身体/情绪怎么变 → 这一批结束时停在哪。
- 用「用户」「{name}」称呼，不要写「角色」。
- 不要写「画面/心情/状态」这些格式元数据；不要复述原对话，不要评价文笔。
- 不要写「第 N 天」——调用方会加。

【原子事实怎么写】
- 只有用户亲口说出的偏好/经历/承诺/关系信息，才能写 about="user"。
- {name}亲口说出的设定、喜好、承诺，写 about="character"。
- 只有双方都明确同意的计划，才写 about="shared"。
- 每条 content ≤120 字，必须以「用户：」「{name}：」「双方：」开头。
- 不要记寒暄、重复日常、临场玩笑、出图 tags、穿着和镜头。
- 确实没有值得记的原子事实 → entries=[]，但 summary 仍要写。

【锚点事实（anchors）】
如果这一批里出现了下面这些**只发生一次**的事，必须单独写一条 anchor：
初次性行为（第一次/破处/初夜）、第一次接吻、第一次口交、第一次后庭、
第一次叫某个称呼、关系确认、第一次吵架/和好、搬家/换工作/被发现。
- 必须写清**在哪、什么时候、当时穿的什么**——这三样是以后被问
  「第一次在哪」时唯一能救场的东西。
- 一条只写一件事，不合并；没有就是空数组。
- `key` 用英文短标识：first_time / first_kiss / first_oral / first_anal /
  relationship / moved / discovered。
- anchor 不占 entries 的 4 条名额，另算。

【输出 JSON】
{{
  "skip": false,
  "summary": "地点…用户…{name}…这一批结束时…",
  "entries": [
    {{"about": "user", "content": "用户：……",
      "topics": ["主题"], "primaryEmotion": "", "openThreads": []}},
    {{"about": "character", "content": "{name}：……",
      "topics": [], "primaryEmotion": "", "openThreads": []}}
  ],
  "anchors": [
    {{"key": "first_time", "content": "第一次（破处）发生在……",
      "place": "卧室", "outfit": "她当时穿什么"}}
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


def _epoch(rec):
    """这条线的回退代次。回退批次时 +1，旧的 distill 结果据此作废。"""
    try:
        return max(0, int(rec.get("epoch") or 0))
    except (TypeError, ValueError):
        return 0


def _current_epoch(line):
    with _PENDING_LOCK:
        rec = _pending_load().get(str(line or "")) or {}
    return _epoch(rec if isinstance(rec, dict) else {})


def remember(line, user_text, reply, custom=None, day=None, phase="",
             card_name="角色"):
    """这一轮先攒进缓冲；够一批了才交给提炼器。

    只收「用户可见的话 + 她的回复」。用户的「想」由调用方过滤掉，
    这里不做二次判断。

    `day/phase`（2026-11-06）：记下这一轮发生在游戏第几天、什么时段。
    批次叙事要带「第 N 天 · 晚上」；跨天时强制切批，避免一批横跨两天，
    检索「昨天」时把前天的事也算进去。
    """
    if not enabled_for(line):
        return
    line = str(line or "").strip()
    reply = str(reply or "").strip()
    if not line or not reply:
        return
    batch = None
    try:
        day_n = int(day or 0) if day is not None else 0
    except (TypeError, ValueError):
        day_n = 0
    with _PENDING_LOCK:
        data = _pending_load()
        rec = data.get(line)
        if not isinstance(rec, dict):
            rec = {"seq": 0, "items": [], "epoch": 0}
        epoch = _epoch(rec)
        rec["seq"] = int(rec.get("seq") or 0) + 1
        items = rec.get("items") if isinstance(rec.get("items"), list) else []
        # 跨天强制切批：这批里已经有别的一天 → 先把它送走，再开新批。
        if items and day_n:
            first_day = int(items[0].get("day") or 0)
            if first_day and first_day != day_n:
                batch = list(items)
                items = []
        items.append({"turn": rec["seq"],
                      "user": str(user_text or "").strip(),
                      "assistant": reply,
                      "day": day_n,
                      "phase": str(phase or "")[:12]})
        rec["items"] = items
        if len(items) >= PERSONA_RAG_INGEST_EVERY:
            batch = list(items)
            rec["items"] = []
        if batch:
            start = int(batch[0].get("turn") or 0)
            end = int(batch[-1].get("turn") or 0)
            rec["last_batch"] = [start, end]
            rec["last_batch_items"] = batch
            rec["last_batch_epoch"] = epoch
            rec["last_batch_id"] = "batch-%d-%d-%d" % (start, end, epoch)
        data[line] = rec
        _pending_save(data)
    if batch:
        _enqueue("distill", line, {
            "turns": batch,
            "custom": custom,
            "card_name": str(card_name or "角色"),
            "epoch": rec.get("last_batch_epoch") if batch else 0,
            "batch_id": rec.get("last_batch_id") if batch else "",
        })


def raw_entries(turns):
    """把一批对话原文转成可入库的条目（2026-11-07）。

    一条消息一条，id 用 `raw-<轮次>-u/a`，回退时能按轮次精确删掉。
    `content` 前缀「第N天·时段 用户说：/ 她：」——检索命中后模型能直接
    看出这是哪一天、谁说的，不用再猜。
    """
    out = []
    for it in turns or []:
        if not isinstance(it, dict):
            continue
        try:
            t = int(it.get("turn") or 0)
        except (TypeError, ValueError):
            t = 0
        if not t:
            continue
        try:
            day = int(it.get("day") or 0)
        except (TypeError, ValueError):
            day = 0
        phase = str(it.get("phase") or "")[:12]
        stamp = ("第%d天%s " % (day, ("·" + phase) if phase else "")) if day else ""
        for role, tag, label in (("user", "u", "用户说"),
                                 ("assistant", "a", "她")):
            text = " ".join(str(it.get(role) or "").split())
            if not text:
                continue
            out.append({
                "id": "raw-%d-%s" % (t, tag),
                "content": (stamp + label + "：" + text)[:RAW_MAX_CHARS],
                "metadata": {
                    "kind": "raw",
                    "role": role,
                    "day": day,
                    "phase": phase,
                    "turn": t,
                    "source": _RAW_SOURCE,
                },
            })
    return out


def forget_last(line):
    """回退上一轮：只撤销最近这一轮，不碰更早的回合。

    2026-11-06 重做（用户报「第 5 轮刚入库又回退」）：
      · pending 里还有未提炼的轮次 → 直接 pop，不动已入库的批次；
      · 最近一轮正好是第 5 轮、批次已成形/已入库 → epoch +1 作废在途结果，
        把这一批的前 4 轮放回 pending，并让后台删掉旧批次；
        下一轮进来会重新凑成 5 轮、用新 epoch 提炼。
    只支持回退最近一轮；不允许连续回退，避免把整条历史拆碎。
    """
    if not enabled_for(line):
        return
    line = str(line or "").strip()
    if not line:
        return
    batch = None
    batch_ids = []
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
            stock = rec.get("last_batch_items")
            lb = rec.get("last_batch")
            if isinstance(stock, list) and stock:
                old_id = str(rec.get("last_batch_id") or "")
                stock.pop()
                rec["epoch"] = _epoch(rec) + 1
                rec["items"] = stock
                rec["seq"] = max(0, int(rec.get("seq") or 0) - 1)
                rec.pop("last_batch", None)
                rec.pop("last_batch_items", None)
                rec.pop("last_batch_epoch", None)
                rec.pop("last_batch_id", None)
                data[line] = rec
                _pending_save(data)
                if isinstance(lb, list) and len(lb) == 2:
                    batch = (int(lb[0]), int(lb[1]))
                if old_id:
                    batch_ids.append(old_id)
            elif isinstance(lb, list) and len(lb) == 2:
                # 老存档没有 last_batch_items：退回只删不知重提。
                # 但 epoch 仍要 +1，否则在途的 distill 会把刚删的批次又灌回去。
                rec["epoch"] = _epoch(rec) + 1
                data[line] = rec
                _pending_save(data)
                batch = (int(lb[0]), int(lb[1]))
    if batch:
        if not batch_ids:
            batch_ids.append("batch-%d-%d" % (batch[0], batch[1]))
        with _LOCK:
            _BLOCKED.add(line)      # 回退清理完成前，这条线不检索
        _enqueue("forget", line, {"start": batch[0], "end": batch[1],
                                  "batch_ids": batch_ids})


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


_RECENT_CACHE = {}
_RECENT_TTL = 300


def _recent_docs(line, batches=3, limit=12):
    """最近几批的叙事。带 5 分钟缓存（2026-11-08）：现在每轮都要带，
    不能每轮扫一遍全库。"""
    key = (line, int(batches or 1), int(limit or 12))
    row = _RECENT_CACHE.get(key)
    if row and time.time() - row[0] < _RECENT_TTL:
        return row[1]
    docs = _recent_docs_uncached(line, batches, limit)
    _RECENT_CACHE[key] = (time.time(), docs)
    return docs


def _recent_docs_uncached(line, batches=3, limit=12):
    """按批次取最近几条事实（2026-11-05）。

    为什么需要：用户问「昨天」「上次」时，小模型对代词的语义匹配很差——
    「昨天太招摇」和「被人看见/丢脸」在 512 维 embedding 里可能差 0.6~0.7，
    正常阈值直接把它过滤掉，演员就完全想不起来。时间问法额外把最近几批
    事实**不看距离**带进去，让演员手里至少有昨天发生过什么。
    """
    client = _client()
    if not client.is_ready():
        time.sleep(1.5)          # 冷启动空窗，见 `warm()` 注释
    if not client.is_ready() or not hasattr(client, "list_entries"):
        return []
    try:
        r = client.list_entries(collection_name(line), timeout=PERSONA_RAG_TIMEOUT)
    except Exception:                                    # noqa: BLE001
        log.exception("回忆批次读取失败 %s", line)
        return []
    entries = [e for e in (r.get("entries") or [])
               if str((e.get("metadata") or {}).get("source") or "") == _SOURCE]
    groups = {}
    for e in entries:
        m = e.get("metadata") or {}
        try:
            key = (int(m.get("turn_start") or 0), int(m.get("turn_end") or 0))
        except (TypeError, ValueError):
            continue
        groups.setdefault(key, []).append(e)
    # 取**最近**几批（2026-11-08 改）：原来是 `[:batches]` 取最早的，
    # 问「昨天」时拿到的可能是前天以前的内容。现在取末尾几批，组内按时间正序。
    keys = sorted(groups.keys())[-max(1, int(batches or 1)):]
    out = []
    for key in keys:
        out.extend(groups[key])
    return out[:limit]


def _bigrams(text):
    """字符 bigram 集合。中文里「她做了早餐」和「早餐是她做的」重叠率很高，
    比词袋稳（没有分词器也能跑）。"""
    s = re.sub(r"\s+", "", str(text or ""))
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _chars(text):
    """去重后的字符集合。短句上比 bigram 宽容得多——
    「她做了早餐，煎了蛋」和「她做早餐的时候煎了鸡蛋」按字集算重叠七成，
    按 bigram 只有两成，MMR 会当场失效。"""
    return set(re.sub(r"[\s，。、！？,.!?~～]+", "", str(text or "")))


def _text_sim(a, b):
    """0~1 的重复度：六成看字符集，四成看 bigram（防「同一批字不同顺序」）。"""
    A, B = _chars(a), _chars(b)
    if not A or not B:
        return 0.0
    uni = len(A & B) / float(len(A | B))
    Ab, Bb = _bigrams(a), _bigrams(b)
    bi = len(Ab & Bb) / float(len(Ab | Bb)) if (Ab and Bb) else 0.0
    return 0.6 * uni + 0.4 * bi


_ANCHOR_RE = re.compile(
    r"第一次|初次|初夜|破处|第一次接吻|第一次口|第一次后庭|"
    r"确认关系|在一起了|搬家|被发现|第一次叫")
_ANCHOR_CACHE = {}
_ANCHOR_TTL = 300


def _anchor_docs(line):
    """锚点事实：只发生一次的节点（第一次/初夜/关系确认/搬家…）。

    为什么单开一条路（2026-11-08）：364 条记忆里每轮只取 3 条，靠向量相似度
    选。「第一次操你是在什么地方」这种问法，语义上更像「第一次高潮」（在数
    次数那几条），而不是「第一次在卧室」——它永远挤不进 top-k。所以这类条目
    不看距离，直接全量塞进回忆块（十几条以内，成本可接受）。

    缓存 5 分钟：每轮 list_entries 拉全量太重（几百条 JSON）。
    """
    now = time.time()
    row = _ANCHOR_CACHE.get(line)
    if row and now - row[0] < _ANCHOR_TTL:
        return row[1]
    client = _client()
    if not client.is_ready():
        time.sleep(1.5)
    docs = []
    if client.is_ready() and hasattr(client, "list_entries"):
        try:
            r = client.list_entries(collection_name(line),
                                    timeout=PERSONA_RAG_TIMEOUT)
            for e in (r.get("entries") or []):
                text = str(e.get("content") or e.get("text") or "")
                meta = dict(e.get("metadata") or {})
                # 两种来源：新版蒸馏直接标 kind=anchor；旧数据靠关键词兜住。
                if meta.get("kind") != "anchor" and not _ANCHOR_RE.search(text):
                    continue
                meta["_anchor"] = True
                docs.append({"id": e.get("id"), "content": text,
                             "metadata": meta, "distance": 0.0})
        except Exception:                                # noqa: BLE001
            log.warning("锚点事实读取失败 %s", line, exc_info=True)
    docs = docs[:20]
    _ANCHOR_CACHE[line] = (now, docs)
    if docs:
        log.info("[回忆] %s 锚点 %d 条", line, len(docs))
    return docs


def mmr_select(docs, k, lam=0.7, dup=0.30):
    """文本级 MMR（2026-11-07）：先挑最相关，之后每次挑「和已选最不重复」的。

    为什么需要：纯 top-k 会把同一件事的三个说法全选进来——
    「她做了早餐 / 她做早餐煎了蛋 / 早餐是她做的」三条占掉一半位置，
    真正新的那条反而挤不进去。玩家看到的「回忆里全是重复」就是这个。

    相关性用 `distance` 换算（1 - 距离），冗余度用字符集 + bigram 的相似度。
    不依赖向量：`client.search` 只回文本和距离，拿不到 embedding。

    两个参数：
      `lam`  1.0 = 退化成纯 top-k；0.0 = 只看多样性。0.7 = 七分相关三分多样。
      `dup`  和已选条目的相似度**达到这个值就直接不要**。只靠 lam 压不住：
             重复条目的相关性往往最高，扣完分还是赢（实测 0.73×0.7−0.34×0.3
             仍然高于一条真正新的 0.58）。所以加一道硬闸。
    """
    if not docs or k <= 0:
        return []
    rest = list(docs)
    picked = []
    while rest and len(picked) < k:
        best = None
        best_score = -1e9
        for d in rest:
            try:
                rel = 1.0 - float(d.get("distance") or 1.0)
            except (TypeError, ValueError):
                rel = 0.0
            red = 0.0
            if picked:
                red = max(_text_sim(d.get("content") or d.get("text") or "",
                                    s.get("content") or s.get("text") or "")
                          for s in picked)
                if red >= dup:                       # 硬闸：几乎同一件事
                    continue
            score = lam * rel - (1.0 - lam) * red
            if score > best_score:
                best_score = score
                best = d
        if best is None:                             # 剩下的全重复了
            break
        picked.append(best)
        rest.remove(best)
    return picked


def retrieve(line, query, k=None, day=None):
    """按当前用户的话找几条提炼过的记忆；daemon 没 ready 就返回空。

    2026-11-06：支持按游戏天数过滤。时间问法（昨天/前天/上次）先试
    `day=当前-1` 的 metadata 过滤，能拿到那一天所有批次叙事和事实；
    旧数据没有 day 时，回退到原来的时间问法兜底（放宽阈值 + 最近批次）。
    """
    if not enabled_for(line):
        return []
    line = str(line or "").strip()
    query = str(query or "").strip()
    if not line or not query:
        return []
    with _LOCK:
        if line in _BLOCKED:
            return []
    client = _client()
    if not client.is_ready():
        # 冷启动空窗（2026-11-08）：daemon 懒启动要 3~8 秒，重启后第一轮必丢。
        # 以前这里直接返回空，模型手里一条记忆都没有，只能从 12 条窗口里编
        # （实测编出「第一次在门板」，而库里明明记着「卧室、床没铺好」）。
        # 等一次，2 秒还不行才放弃。
        time.sleep(1.5)
    if not client.is_ready():
        log.info("[回忆] %s daemon 未就绪，本轮不检索", line)
        return []
    limit = PERSONA_RAG_MAX_DISTANCE
    # 返回几条 vs 检索几条（2026-11-07）：候选池放大到 12 起步，再交给 MMR
    # 按「相关性 - 重复度」挑。取多少回多少的话，MMR 没得挑。
    kk_out = max(1, int(k or PERSONA_RAG_K))
    kk = max(kk_out * 4, 12)
    time_q = bool(_TIME_QUERY.search(query))
    if time_q:
        limit = max(limit, 0.65)
        kk = max(kk, 12)
    # 先按天过滤（只对时间问法 + 有新数据 day 时）
    day_q = 0
    if time_q and day:
        try:
            day_q = int(day) - 1
        except (TypeError, ValueError):
            day_q = 0
    if day_q > 0:
        try:
            r = client.search(collection_name(line), query,
                              k=kk, timeout=PERSONA_RAG_TIMEOUT,
                              allow_start=False,
                              where={"$and": [{"source": _SOURCE},
                                              {"day": day_q}]})
        except Exception:                                # noqa: BLE001
            log.exception("回忆按天检索失败 %s", line)
            r = None
        if r and r.get("ok"):
            out = [d for d in (r.get("docs") or [])
                   if str((d.get("metadata") or {}).get("kind") or "") in
                   ("batch", "fact")]
            if out:
                log.info("[回忆] %s 按第 %d 天命中 %d 条（query=%r）",
                         line, day_q, len(out), query[:40])
                return mmr_select(out, kk_out)
    # 普通路径 / 天数过滤无结果时回退
    try:
        r = client.search(collection_name(line), query,
                          k=kk, timeout=PERSONA_RAG_TIMEOUT,
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
        if dist <= limit:
            out.append(d)
    # 时间问法：把最近几批事实排在最前面（不靠 embedding 距离）。
    if time_q:
        recent = _recent_docs(line)
        seen = {str(d.get("id") or "") for d in recent}
        merged = recent + [d for d in out
                           if str(d.get("id") or "") not in seen]
        out = merged[:12]
    log.info("[回忆] %s 命中 %d 条（query=%r，阈值=%.2f，时间问法=%s）",
             line, len(out), query[:40], limit, time_q)
    return mmr_select(out, kk_out)


def retrieve_raw(line, query, k=None):
    """按当前输入找几条窗口外的**原话**；daemon 没 ready 返回空。

    阈值比提炼事实宽（`RAW_MAX_DISTANCE=0.70` 对 0.60）：原文句子长、
    还带着叙事和对话，和一句提问的 embedding 距离天然更远，用 0.60
    一条都召不回。代价是偶尔召回弱相关内容，靠「最多 3 条 + 每条 260 字」
    和注入块里的用法说明压住。
    """
    if not enabled_for(line):
        return []
    line = str(line or "").strip()
    query = str(query or "").strip()
    if not line or not query:
        return []
    with _LOCK:
        if line in _BLOCKED:
            return []
    client = _client()
    if not client.is_ready():
        time.sleep(1.5)          # 同 `retrieve`，冷启动空窗
    if not client.is_ready():
        return []
    kk = max(1, int(k or RAW_BLOCK_ITEMS))
    kk_pool = max(kk * 3, 12)      # 候选池放大，MMR 才有得挑
    try:
        r = client.search(collection_name(line), query, k=kk_pool,
                          timeout=PERSONA_RAG_TIMEOUT, allow_start=False,
                          where={"source": _RAW_SOURCE})
    except Exception:                                    # noqa: BLE001
        log.exception("原话检索失败 %s", line)
        return []
    if not r.get("ok"):
        return []
    out = []
    for d in r.get("docs") or []:
        meta = d.get("metadata") or {}
        # 真实 Chroma 已经用 where 过滤过；这里再兜一层，防止别的实现
        # 忽略 where 时把批次叙事当成「原话」贴进来。
        if str(meta.get("kind") or "") != "raw":
            continue
        try:
            dist = float(d.get("distance", 1.0))
        except (TypeError, ValueError):
            dist = 1.0
        if dist <= RAW_MAX_DISTANCE:
            out.append(d)
    if out:
        log.info("[原话] %s 命中 %d 条（query=%r）", line, len(out), query[:40])
    # 原话层重复更凶（同一个场景被记三四遍），MMR 挑完再给。
    return mmr_select(out, kk)


def _fact_block(docs):
    """提炼事实 → 一段；没有可用的返回空串。"""
    batches, facts = [], []
    for d in docs:
        meta = d.get("metadata") or {}
        kind = str(meta.get("kind") or "")
        if kind == "raw":
            # 原话条目走 `_raw_block`；混进事实层会被当成「原子事实」读。
            continue
        if kind == "batch":
            batches.append(d)
        else:
            facts.append(d)
    # 锚点排最前，不受「2 批 + 3 条」限制：它们是「问起来必须答对」的事，
    # 被 top-k 挤掉就等于让模型编（2026-11-08 实测「第一次在门板」）。
    anchors = [d for d in docs if (d.get("metadata") or {}).get("_anchor")]
    _aid = {id(d) for d in anchors}
    batches = [d for d in batches if id(d) not in _aid]
    facts = [d for d in facts if id(d) not in _aid]
    pick = anchors[:8] + batches[:2] + facts[:3]
    if not pick:
        return ""
    who = {"user": "用户事实", "character": "角色言行", "shared": "双方约定"}
    lines = ["【长期记忆 · 从过去提炼出的经过与事实】"]
    for d in pick:
        meta = d.get("metadata") or {}
        if meta.get("_anchor"):
            # 锚点单独标出来（2026-11-08）：以前它们混进 facts，顶着
            # 「[双方约定]」这种标签，读起来像普通的计划，模型不会当回事。
            head = "[锚点·只发生过一次]"
            limit = 200
        elif str(meta.get("kind") or "") == "batch":
            head = "[当时的经过]"
            limit = 320
        else:
            about = who.get(str(meta.get("about") or ""), "记忆")
            head = "[%s]" % about
            limit = 160
        start = meta.get("turn_start")
        end = meta.get("turn_end")
        rng = "｜第 %s–%s 次回复" % (start, end) if start and end else ""
        topics = meta.get("topics") or ""
        head = head[:-1] + rng + ("｜主题：" + topics if topics else "") + "]"
        text = " ".join(str(d.get("content") or "").split())
        if not text:
            continue
        if len(text) > limit:
            text = text[:limit] + "…"
        lines.append(head + "\n" + text)
    return "\n".join(lines) if len(lines) > 1 else ""


def _raw_block(docs):
    """原文条目 → 一段；总字数受 RAW_BLOCK_CHARS 限制。"""
    if not docs:
        return ""
    lines, used = [], 0
    for d in docs:
        text = " ".join(str(d.get("content") or "").split())
        if not text:
            continue
        if len(text) > RAW_ITEM_CHARS:
            text = text[:RAW_ITEM_CHARS] + "…"
        if lines and used + len(text) > RAW_BLOCK_CHARS:
            break
        lines.append("- " + text)
        used += len(text)
    if not lines:
        return ""
    return "【当时的原话 · 用来确认说过和做过什么】\n" + "\n".join(lines)


def build_block(line, query, k=None, day=None, context=""):
    """拼成能直接塞进最后一条玩家消息的一段；没命中返回空串。

    2026-11-06：两路检索——当前用户话 + 最近几轮上下文，合并去重后
    优先取 2 条批次叙事 + 3 条原子事实。问「昨天」这种短句时，
    上下文那一路能把「昨天在哪、和谁、发生了什么」带出来。

    2026-11-07：加原文层。两层分工写进块标题——提炼层说「经过与事实」，
    原文层说「用来确认说过和做过什么」，结尾用法句再点明「正在发生的
    比旧话更重要」，避免演员拿旧对话当台词复述。
    """
    docs = retrieve(line, query, k=k, day=day)
    # 锚点（第一次/初夜/关系确认…）不看距离，直接排最前，见 `_anchor_docs`。
    anchor = _anchor_docs(line)
    if anchor:
        seen = {str(d.get("id") or "") for d in anchor}
        docs = anchor + [d for d in docs if str(d.get("id") or "") not in seen]
    # 最近 2 批无条件带上（2026-11-08 用户拍板）：向量对「昨天吃了什么」这种
    # 泛问经常算不准，最近发生的事却几乎总是相关的。多几百字，换「她记得」。
    recent = _recent_docs(line, batches=2)
    if recent:
        seen = {str(d.get("id") or "") for d in docs}
        docs = [d for d in recent if str(d.get("id") or "") not in seen] + docs
    if context:
        extra = retrieve(line, context, k=k, day=day)
        seen = {str(d.get("id") or "") for d in docs}
        docs += [d for d in extra
                 if str(d.get("id") or "") not in seen]
    parts = []
    fact = _fact_block(docs)
    if fact:
        parts.append(fact)
    raw = _raw_block(retrieve_raw(line, query))
    if raw:
        parts.append(raw)
    if not parts:
        return ""
    parts.append(
        "【用法】用户问起自己/双方/角色的既有事实，或「昨天/上次发生过什么」时，"
        "以此为准。原话是当时确实说过、做过的细节，用来说明「是谁说的、"
        "当时怎么说的」；当前这一拍接着往下演，正在发生的比旧话更重要。"
        "旧事实只能补充，不会被近期闲聊覆盖。")
    return "\n\n".join(parts)


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
        card_name = str((payload or {}).get("card_name") or "角色")
        epoch = int((payload or {}).get("epoch") or 0)
        batch_id = str((payload or {}).get("batch_id") or "")
        # 回退可能已经发生：当前 epoch 变了，这批就是作废的，绝不入库。
        if epoch != _current_epoch(line):
            log.info("记忆批次已作废，跳过入库 %s（epoch=%d）", line, epoch)
            return
        # 原话先入库（2026-11-07）：它不依赖 LLM，所以放在 distill 之前。
        # 提炼连续失败两次会整批丢弃，但原话已经落地，细节不会一起没掉。
        raws = raw_entries(turns)
        if raws:
            rr = client.ingest(coll, raws)
            if not rr.get("ok"):
                log.warning("原话入库失败 %s：%s", coll, rr.get("error"))
        entries = distill(turns, custom, card_name=card_name,
                          epoch=epoch, batch_id=batch_id)
        if entries is None:
            # JSON 截断/写坏：重试一次；再失败才丢弃，并留下 warning。
            if not (payload or {}).get("_retried"):
                payload = dict(payload or {})
                payload["_retried"] = True
                _enqueue("distill", line, payload)
            else:
                log.warning("记忆提炼连续两次失败，丢弃这批 %s", line)
            return
        if not entries:
            return
        # 再检查一次：提炼期间回退也可能已经发生。
        if epoch != _current_epoch(line):
            log.info("记忆批次在提炼期间被回退，丢弃 %s（epoch=%d）", line, epoch)
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
        ids = [str(x) for x in ((payload or {}).get("batch_ids") or []) if x]
        if start and end:
            ids.append("batch-%d-%d" % (start, end))
            ids += ["%d-%d-%d" % (start, end, idx) for idx in range(4)]
            # 原文条目（2026-11-07）：这一批每一轮的原话都要一起撤掉，
            # 否则回退之后演员还能把被撤掉的那句话召回出来。
            for t in range(start, end + 1):
                ids += ["raw-%d-u" % t, "raw-%d-a" % t]
        try:
            for entry_id in ids:
                r = client.delete_entry(coll, entry_id)
                if not r.get("ok"):
                    log.warning("记忆删除失败 %s/%s：%s", coll, entry_id,
                                r.get("error"))
        finally:
            # 回退清理完成，恢复检索；失败也要放开，不能永久锁死这条线。
            with _LOCK:
                _BLOCKED.discard(line)


def _format_dialogue(turns, name="角色"):
    parts = []
    for it in turns:
        t = it.get("turn")
        day = it.get("day")
        u = str(it.get("user") or "").strip()
        a = str(it.get("assistant") or "").strip()
        prefix = ("第%s天 · " % day) if day else ""
        if u:
            parts.append("----- %s第%s次回复 · 用户 -----\n用户：%s"
                         % (prefix, t, u))
        if a:
            parts.append("----- %s第%s次回复 · %s -----\n%s：%s"
                         % (prefix, t, name, name, a))
    return "\n\n".join(parts)


def distill(turns, custom=None, card_name="角色", epoch=0, batch_id=""):
    """调 LLM 提炼一批对话，返回可入库的 entries；失败返回 None。

    返回里第 0 条是 `kind=batch` 的批次叙事（如果模型给了 summary），
    后面是 `kind=fact` 的原子事实。
    """
    turns = [t for t in (turns or []) if isinstance(t, dict)]
    if not turns:
        return []
    start = int(turns[0].get("turn") or 0)
    end = int(turns[-1].get("turn") or 0)
    day = max([int(t.get("day") or 0) for t in turns] or [0])
    phase = ""
    for t in turns:
        if str(t.get("phase") or "").strip():
            phase = str(t.get("phase") or "").strip()[:12]
    prompt = _MEMORY_PROMPT.format(start=start, end=end,
                                   dialogue=_format_dialogue(turns, card_name),
                                   name=card_name or "角色")
    from app import llm
    if custom:
        llm.set_custom_override(custom)
    try:
        # 记忆提炼也属于扮演链路：默认跟扮演模型走；玩家临时指定了自定义模型
        # 时那个优先（`set_custom_override` 后 `get_custom_override` 非空）。
        kw = {}
        if not llm.get_custom_override():
            if PERSONA_PROVIDER:
                kw["provider"] = PERSONA_PROVIDER
            if PERSONA_MODEL:
                kw["model"] = PERSONA_MODEL
        raw = llm.call_llm([{"role": "user", "content": prompt}],
                           temperature=0.2, max_tokens=1600, **kw)
    except Exception:                                    # noqa: BLE001
        log.exception("记忆提炼调用失败 %s-%s", start, end)
        return None
    finally:
        if custom:
            llm.set_custom_override(None)
    parsed = parse_memory_json(raw, start, end, day=day, phase=phase,
                               card_name=card_name, epoch=epoch,
                               batch_id=batch_id)
    if parsed:
        return parsed
    # 空结果要区分「确实没什么可记」和「JSON 被截断/写坏」：
    # 后者必须重试，否则这一批（以及里面「被人看见」这类重要事实）会永久丢。
    text = str(raw or "")
    lo = text.find("{")
    hi = text.rfind("}")
    if lo < 0 or hi <= lo:
        return None
    try:
        json.loads(text[lo:hi + 1])
    except ValueError:
        return None
    return []


def parse_memory_json(raw, start, end, day=0, phase="", card_name="角色",
                      epoch=0, batch_id=""):
    """解析提炼器输出 → Chroma entries。坏 JSON / 空内容一律返回 []。

    2026-11-06 起支持两层：`summary`（批次叙事，kind=batch）+
    `entries`（原子事实，kind=fact）。批次叙事带「第 N 天 · 时段」前缀。
    """
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
    out = []
    summary = " ".join(str(data.get("summary") or "").split())
    if summary:
        # day=0 不写前缀（2026-11-08）：补录归档时不知道历史轮次属于第几天，
        # 强写「【第0天】」比不写更误导。
        prefix = ("【第%d天%s】" % (int(day or 0),
                                 (" · " + str(phase)) if phase else "")
                  if int(day or 0) else "")
        content = prefix + summary[:320]
        out.append({
            "id": str(batch_id or ("batch-%d-%d-%d"
                                  % (start, end, int(epoch or 0)))),
            "content": content,
            "metadata": {
                "kind": "batch",
                "about": "shared",
                "day": int(day or 0),
                "phase": str(phase or "")[:12],
                "turn_start": start,
                "turn_end": end,
                "topics": "",
                "source": _SOURCE,
            },
        })
    # 锚点事实（2026-11-08）：只发生一次的节点，单独存 kind=anchor。
    # 渲染时它们不看向量距离、直接排最前（见 `_anchor_docs`）。
    raw_anchors = data.get("anchors")
    if isinstance(raw_anchors, list):
        for j, a in enumerate(raw_anchors[:4]):
            if not isinstance(a, dict):
                continue
            ac = str(a.get("content") or "").strip()
            if not ac:
                continue
            out.append({
                "id": "anchor-%d-%d-%s" % (start, end,
                                           str(a.get("key") or j)[:24]),
                "content": ac[:200],
                "metadata": {
                    "kind": "anchor",
                    "key": str(a.get("key") or "")[:24],
                    "about": "shared",
                    "day": int(day or 0),
                    "place": str(a.get("place") or "")[:40],
                    "outfit": str(a.get("outfit") or "")[:80],
                    "turn_start": start,
                    "turn_end": end,
                    "source": _SOURCE,
                },
            })
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return out
    for i, e in enumerate(raw_entries[:4]):
        if not isinstance(e, dict):
            continue
        content = str(e.get("content") or "").strip()
        if not content:
            continue
        about = _norm_about(e.get("about"), content, card_name)
        content = _ensure_prefix(about, content, card_name)
        if len(content) > 160:                    # 提示词说 120，留点余量
            content = content[:160]
        topics = e.get("topics") if isinstance(e.get("topics"), list) else []
        opens = e.get("openThreads") if isinstance(e.get("openThreads"), list) else []
        out.append({
            "id": "%d-%d-%d" % (start, end, i),
            "content": content,
            "metadata": {
                "kind": "fact",
                "about": about,
                "day": int(day or 0),
                "turn_start": start,
                "turn_end": end,
                "topics": ",".join(str(x) for x in topics if x)[:120],
                "primary_emotion": str(e.get("primaryEmotion") or "")[:60],
                "open_threads": ",".join(str(x) for x in opens if x)[:160],
                "source": _SOURCE,
            },
        })
    return out


def _norm_about(about, content, card_name="角色"):
    a = str(about or "").strip().lower()
    if a in ("user", "character", "shared"):
        return a
    c = str(content or "")
    if card_name and re.match(r"^%s[：:]" % re.escape(str(card_name)), c):
        return "character"
    if re.match(r"^(角色|她)[：:]", c) or c.startswith("【角色】"):
        return "character"
    if re.match(r"^(双方|我们)[：:]", c) or c.startswith("【双方】"):
        return "shared"
    return "user"


def _ensure_prefix(about, content, card_name="角色"):
    text = str(content or "").strip()
    if re.match(r"^(用户|角色|双方)[：:]", text) or \
            re.match(r"^【(用户|角色|双方)】", text):
        return text
    if about == "character" and card_name and \
            re.match(r"^%s[：:]" % re.escape(str(card_name)), text):
        return text
    return {"user": "用户：", "character": (str(card_name or "角色") + "："),
            "shared": "双方："}.get(about, "用户：") + text

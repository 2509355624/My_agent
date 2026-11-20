"""角色扮演的长期记忆向量库（2026-11-05；2026-11-20 V3 父子块）。

## V3 父子块

每 5 次 AI 回复把最近一批对话交给「记忆提炼器」，产出两类条目，共用一个
`parent_group_id`（一批一个）：

**子块 `subfact`（检索单元）**：一条一事的原子事实，AI 概括重写（不切原文
句子），150–200 字软约束。骨架：`[归属] 第N天·时段 谁 做了什么/说了什么
（关键对象）`。向量召回只认子块；重复实体必须带时段锚点消歧（早/中/晚各是
哪一次）。入库形状：

    {"about": "user|character|shared", "content": "用户：……",
     "topics": "…", "primary_emotion": "…", "open_threads": "…",
     "day": 2, "phase": "中午", "turn_start": 3, "turn_end": 5,
     "parent_group_id": "batch-3-5-0", "source": "batch_summary_v3"}

**父块 `parent`（压缩叙事）**：整批 500–1000 字的概括叙事，按时间顺序写清
谁在何时做了什么、说了什么。**不参与向量召回**，只在子块命中后按
`parent_group_id` 挂载取回；单块超 `_PARENT_MAX_CHARS` 边界时拆 part 1/2，
同编号关联、召回按序拼接。

**锚点 `anchor`**：只发生一次的节点（第一次/关系确认…），规则置顶，不看
向量距离。

归属（about）是关键：用户亲口说的才算用户事实；角色说的不能写成用户事实；
只有双方明确同意的计划才算 shared。

**旧版兼容**：老库残留 `kind=batch`（视同 parent）、`kind=fact`（视同
subfact）、`kind=raw`（原话层，新数据不再写入，检索/注入不再取用）。旧数据
不迁移，检索时兼容读取兜底。

## 隔离

一条会话线（用户 + 角色 + 平台）一个 collection：

    persona_<sha256(会话线)[:16]>

清空存档只删这一条线，别人的向量库不受影响。

## 慢加载

CPU 小模型冷启动十几秒，绝不能在聊天轮里同步等：
  · 提炼入库都在后台线程/队列里做；
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

from app import rerank
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

# ── V3 父子块 kind ──────────────────────────────────────────────
# parent  父块：整批压缩叙事，不参与向量召回，只按子块 parent_group_id 挂载
# subfact 子块：一条一事的检索单元，向量召回只认它
# anchor  锚点：只发生一次的节点，规则置顶，不看距离
# raw     旧版原话层残留；新数据不再写入，检索/注入不再取用
_KIND_PARENT = "parent"
_KIND_SUB = "subfact"
_KIND_ANCHOR = "anchor"
_KIND_RAW = "raw"
# 旧库兼容：batch 视同 parent，fact 视同 subfact（旧数据不迁移，兜底读取）
_LEGACY_PARENT_KINDS = ("batch",)
_LEGACY_SUB_KINDS = ("fact",)

# 子块软约束：prompt 让模型控制在 150–200 字；代码不再做 160 字硬截断兜底，
# 避免把一条完整事实拦腰切断。
_SUB_SOFT_MAX = 200
# 父块单块字符硬边界（对齐 bge-small 512 token）。父块不参与召回，拆 part
# 只为注入预算与历史兼容；超长按句号/分号语义边界切，同 group_id 关联。
_PARENT_MAX_CHARS = 700

# 原文层（2026-11-07）。和事实层并存，不替代它：
#   · 阈值 0.70 比事实的 0.60 宽——原文句子长、复述多，距离天然更远；
#   · 每轮取几条、每条注入时压到多少字、总预算多少。
#
# 2026-11-10 放宽（用户报「跨天记不住细节」）：原来入库截 400 字，而她的
# 回复常态 1000 字，后半段的物件、名字、动作永远进不了库。放宽到 900 字、
# 注入时也别再砍回 260 字。注意 bge-small-zh-v1.5 的 512 token 上限：超过
# 前 512 token 的部分**不参与向量匹配**，但命中后整条 content 都会注入，
# 所以细节至少读得到；真要让后半段也可被检索，得改成分块入库（本轮没动）。
_RAW_SOURCE = "raw_msg_v1"
RAW_MAX_CHARS = 900
RAW_MAX_DISTANCE = 0.78
RAW_BLOCK_ITEMS = 4
# 单条注入上限跟入库上限对齐（2026-11-10）：入库放宽到 900 字之后，
# 注入侧还截 520 字，等于后半段白存了。总预算不动，超预算时按剩余
# 字数截而不是整条丢，优先让第一条拿全文。
RAW_ITEM_CHARS = RAW_MAX_CHARS
RAW_BLOCK_CHARS = 1600
# 回填时一次送多少条去算 embedding：后台线程跑，单批别压到 daemon
# 的 30 秒调用超时；批与批之间也让新的 distill 有机会插队。
_BACKFILL_BATCH = 10
_BACKFILL_QUEUED = set()
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
_MEMORY_PROMPT = """你是角色扮演对话的记忆提炼器。把这一批对话整理成「父块叙事 + 子块事实」两层。

【对话（第 {start}–{end} 次回复）】
{dialogue}

【父块叙事（summary）——给演员补语境】
- 500–1000 字，按时间顺序写清：地点/场合 → 用户做了什么、说了什么 →
  {name}怎么反应、身体/情绪怎么变 → 这一批结束时停在哪。
- 保留「谁在什么时候做了什么、说了什么」：关键对象、地点、承诺、约定状态
  都要留下，不要写成干巴巴的提纲。
- 用「用户」「{name}」称呼，不要写「角色」。
- 时间口径：每个动作带上时段（早晨/上午/中午/下午/傍晚/深夜）；同一对象在
  不同时段重复出现时，写清是哪一次——「中午一起吃了玉子烧」和「晚上回去
  热玉子烧」不能混成一句。
- 不要写「画面/心情/状态」这些格式元数据；不要逐字复述原对话，不要评价文笔。
- 不要写「第 N 天」——调用方会加。
- 往后的安排写成当时记下的一笔：「当时约好第二天早上七点在宿舍楼下见」，
  以后被召回时读起来是过去发生过的事。

【子块事实（facts）——检索单元，一条一事】
- 每条只记一件可长期复用的事：偏好、承诺、关系变化、重要物品/地点、未决线头。
- 写法骨架：`[归属] 第N天·时段 谁 做了什么/说了什么（关键对象）`。
  例如「用户：中午在食堂说讨厌香菜」「{name}：晚上答应周末一起去海边」。
- 150–200 字，一条一事；不要把两件事塞进同一条。
- 归属判据：
  · 只有用户亲口说出的偏好/经历/承诺/关系信息，about="user"；
  · {name}亲口说出的设定、喜好、承诺，about="character"；
  · 只有双方都明确同意的计划，about="shared"。
- 承诺和约定写成过去式的一笔记录，带上「第二天」「那一周」这类锚点。
- 重复出现的对象必须带时段消歧，光写对象名分不清是哪一次。
- 不要记寒暄、重复日常、临场玩笑、出图 tags、穿着和镜头。
- 确实没有值得记的子块 → facts=[]，但 summary 仍要写。

【锚点事实（anchors）】
如果这一批里出现了下面这些**只发生一次**的事，必须单独写一条 anchor：
初次性行为（第一次/破处/初夜）、第一次接吻、第一次口交、第一次后庭、
第一次叫某个称呼、关系确认、第一次吵架/和好、搬家/换工作/被发现。
- 必须写清**在哪、什么时候、当时穿的什么**——这三样是以后被问
  「第一次在哪」时唯一能救场的东西。
- 一条只写一件事，不合并；没有就是空数组。
- `key` 用英文短标识：first_time / first_kiss / first_oral / first_anal /
  relationship / moved / discovered。
- anchor 不占 facts 名额，另算。

⚠️ **判据要严**（2026-11-08 实测教训）：模型曾把「换了新地点/新玩法」也当成
第一次，同一条线写出了三个「破处」。只有对话里**明确出现**「第一次」「头一回」
「从来没」这类说法时才写；只是「这次在浴室」「这次从后面」不算。
拿不准就**不写**——错的锚点比没有更糟，模型会把它当事实反复引用。

【输出 JSON】
{{
  "skip": false,
  "summary": "地点…第N天·时段 用户…{name}…这一批结束时…",
  "facts": [
    {{"about": "user", "content": "用户：中午在食堂说讨厌香菜",
      "topics": ["主题"], "primaryEmotion": "", "openThreads": []}},
    {{"about": "character", "content": "{name}：晚上答应周末一起去海边",
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


def _with_line(entries, line):
    """入库前给每条条目 metadata 补上所属线键（2026-11-20 加固）。

    老库条目没有这个字段，反查归属时只能靠 collection 名猜哈希；新入库
    带上 line，以后串库/清档能直接从条目元数据认出它属于哪条线。
    """
    line = str(line or "").strip()
    if not line:
        return entries
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        md = e.get("metadata")
        if isinstance(md, dict):
            md.setdefault("line", line)
    return entries


# ── 待执行删除任务持久化（2026-11-20 加固）──────────────────────────────
#
# 以前 drop/forget 只丢进内存 queue.Queue，进程在后台消费前重启，任务就
# 丢了——表现为「状态和历史删干净了，向量库留一个空壳/残留库」。这里把
# 待执行的 drop 记进 pending 文件的 `_ops` 列表，worker 成功后摘除；warm
# 启动时回放没执行完的操作。forget 是单批回退、重启丢失影响小，暂不持久化。

def _ops_load(data=None):
    data = data if data is not None else _pending_load()
    ops = data.get("_ops")
    return ops if isinstance(ops, list) else []


def _op_add(op, line):
    with _PENDING_LOCK:
        data = _pending_load()
        ops = _ops_load(data)
        if not any(x.get("op") == op and x.get("line") == line for x in ops):
            ops.append({"op": op, "line": str(line or "")})
            data["_ops"] = ops
            _pending_save(data)


def _op_done(op, line):
    with _PENDING_LOCK:
        data = _pending_load()
        ops = _ops_load(data)
        new = [x for x in ops
               if not (x.get("op") == op and x.get("line") == line)]
        if len(new) != len(ops):
            data["_ops"] = new
            _pending_save(data)



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


def _rows_to_turns(rows):
    """归档 `[{role, content}]` 配对成提炼用的 turns。

    一轮 = 一条 user + 一条 assistant；role 错乱或缺失时就近容错。归档里
    没有 day/phase，留 0/空（解析层 day=0 不写天数前缀）。
    """
    turns = []
    pending_user = None
    n = 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        role = str(r.get("role") or "")
        text = " ".join(str(r.get("content") or "").split())
        if not text:
            continue
        if role == "user":
            pending_user = text
        elif role == "assistant":
            n += 1
            turns.append({"turn": n, "user": pending_user or "",
                          "assistant": text, "day": 0, "phase": ""})
            pending_user = None
    if pending_user:                      # 末尾落单的用户话，补一轮不丢
        n += 1
        turns.append({"turn": n, "user": pending_user, "assistant": "",
                      "day": 0, "phase": ""})
    return turns


def _backfill_distill(client, coll, line, turns, card_name="角色", custom=None):
    """把归档轮次按批走提炼，产出 V3 父子块入库；不写旧原话层。

    回填不走过期 epoch 校验（老线没有在途回退）；batch_id 按轮次区间定，
    重复回填同 id upsert 覆盖，天然幂等。
    """
    every = max(1, int(PERSONA_RAG_INGEST_EVERY or 5))
    for i in range(0, len(turns), every):
        batch = turns[i:i + every]
        start = int(batch[0].get("turn") or 0)
        end = int(batch[-1].get("turn") or 0)
        batch_id = "batch-%d-%d-0" % (start, end)
        entries = distill(batch, custom, card_name=card_name,
                          epoch=0, batch_id=batch_id)
        if entries:
            r = client.ingest(coll, _with_line(entries, line))
            if not r.get("ok"):
                log.warning("归档回填入库失败 %s：%s", coll, r.get("error"))


def schedule_backfill(line, rows_factory):
    """按线触发一次「归档提炼回填」，幂等、后台执行。

    归档历史对话配对成轮次后分批走提炼，产出 V3 父子块入库；不写旧原话
    层。`rows_factory` 用 callable 传，只有真要回填时才读全量归档，不拖
    聊天这一轮。重复回填同 id upsert 覆盖，天然幂等。
    """
    line = str(line or "").strip()
    if not line or not enabled_for(line):
        return
    with _LOCK:
        if line in _BACKFILL_QUEUED:
            return
        _BACKFILL_QUEUED.add(line)
    try:
        rows = rows_factory() if callable(rows_factory) else rows_factory
    except Exception:                                        # noqa: BLE001
        with _LOCK:
            _BACKFILL_QUEUED.discard(line)
        log.exception("读取归档失败，跳过回填 %s", line)
        return
    if not rows:
        return
    _enqueue("backfill", line, {"rows": rows})


def _backfill_raw(client, coll, rows, line=""):
    """把归档里的每条原话补成/覆盖成 900 字全文，返回实际写入条数。

    已有 raw 条目用**它原来的 id** upsert 覆盖（400 字截断的那批原地变长，
    不新增重复条目），缺失的用 `rawb-<轮次>-u/a` 追加。元数据沿用旧条目，
    day/phase/turn 不丢。重跑时内容一致就跳过，天然幂等。
    """
    listed = client.list_entries(coll, timeout=60)
    if not listed.get("ok"):
        log.warning("回填读库失败 %s：%s", coll, listed.get("error"))
        return 0
    existing = {}
    for e in listed.get("entries") or []:
        meta = e.get("metadata") or {}
        if str(meta.get("kind") or "") == "raw":
            existing[str(e.get("id") or "")] = (
                str(e.get("content") or ""), meta)
    docs = [(eid, c) for eid, (c, _) in existing.items()]
    todo, used_ids = [], set()
    for i, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = " ".join(str(row.get("content") or "").split())
        if not text:
            continue
        label = "用户说" if role == "user" else "她"
        tag = "u" if role == "user" else "a"
        turn = i // 2 + 1
        # 太短的句子不能当锚：「好。」会命中一堆条目，覆盖掉别人的记忆。
        # 短文本一律走 rawb- 新增，重跑时靠 id + 内容一致照样幂等。
        anchor = text[:40] if len(text) >= 40 else ""
        hit, meta = None, {}
        if anchor:
            for eid, doc in docs:
                if eid in used_ids or anchor not in doc:
                    continue
                old_role = str(existing[eid][1].get("role") or "")
                if old_role and old_role != role:
                    continue
                hit, meta = eid, existing[eid][1]
                break
        stamp = ""
        if hit:
            m = re.match(r"^(第\d+天(?:·[^\s：]+)?\s+)", existing[hit][0])
            if m:
                stamp = m.group(1)
        content = (stamp + label + "：" + text)[:RAW_MAX_CHARS]
        eid = hit or ("rawb-%d-%s" % (turn, tag))
        if eid in used_ids:
            continue
        if eid in existing and existing[eid][0] == content:
            used_ids.add(eid)
            continue
        md = dict(meta) if meta else {
            "kind": "raw", "role": role, "day": 0, "phase": "",
            "turn": turn, "source": _RAW_SOURCE, "backfill": 1,
        }
        used_ids.add(eid)
        todo.append({"id": eid, "content": content, "metadata": md})
    written = 0
    for s in range(0, len(todo), _BACKFILL_BATCH):
        r = client.ingest(coll, _with_line(todo[s:s + _BACKFILL_BATCH], line))
        if not r.get("ok"):
            log.warning("回填写库失败 %s：%s", coll, r.get("error"))
            # 负数让调用方清掉「已排过」标记，下轮重排；已写入的那几批
            # 因为内容一致会被跳过，等于接着补而不是从头再来。
            return -1
        written += int(r.get("count") or 0)
    if written:
        log.info("归档原文回填 %s：补/覆盖 %d 条", coll, written)
    return written


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
    _op_add("drop", line)          # 持久化：重启后 warm 回放，避免删库任务丢失
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


def _mount_parents(line, sub_docs, extra_lines=()):
    """命中子块 → 按 `parent_group_id` 取回同组父块，按 part 排序。

    父块不参与向量召回，只作为子块命中后的语境挂载。兼容旧库：旧 fact 没有
    `parent_group_id` 时，按 (turn_start, turn_end) 关联到同批旧 batch。
    返回的父块条目可能含多个 part，注入时按 part 顺序拼成完整父块。
    """
    lines = _lines_with(line, extra_lines)
    group_ids, legacy_keys = set(), set()
    for d in sub_docs or []:
        meta = _meta(d)
        gid = str(meta.get("parent_group_id") or "").strip()
        if gid:
            group_ids.add(gid)
            continue
        try:
            legacy_keys.add((int(meta.get("turn_start") or 0),
                             int(meta.get("turn_end") or 0)))
        except (TypeError, ValueError):
            pass
    if not group_ids and not legacy_keys:
        return []
    client = _client()
    if not client.is_ready() or not hasattr(client, "list_entries"):
        return []
    mounted, seen = [], set()
    for ln in lines:
        try:
            r = client.list_entries(collection_name(ln),
                                    timeout=PERSONA_RAG_TIMEOUT)
        except Exception:                                # noqa: BLE001
            log.exception("父块挂载读取失败 %s", ln)
            continue
        for e in r.get("entries") or []:
            if not _is_parent(e):
                continue
            m = _meta(e)
            gid = str(m.get("group_id") or "").strip()
            hit = bool(gid and gid in group_ids)
            if not hit and not gid:
                try:
                    key = (int(m.get("turn_start") or 0),
                           int(m.get("turn_end") or 0))
                except (TypeError, ValueError):
                    key = (0, 0)
                hit = key in legacy_keys
            if not hit:
                continue
            eid = str(e.get("id") or "")
            if eid in seen:
                continue
            seen.add(eid)
            mounted.append(e)

    def _part(e):
        try:
            return int(_meta(e).get("part") or 1)
        except (TypeError, ValueError):
            return 1
    # 同组按 part 排序；组间按 group_id 稳定排列。
    mounted.sort(key=lambda e: (str(_meta(e).get("group_id") or ""), _part(e)))
    return mounted


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


# ── 时效裁决 / 遗忘清理（2026-11-20）──────────────────────────────────
#
# 召回侧：同一主题被后续批次更新后，旧版本不该再端上桌（旧约定、旧状态
# 反复出现会把已经演过去的事又拉回来）。只在原子事实之间做新旧压制，
# 锚点（只发生一次）、批次叙事、原话层都不参与。
#
# 遗忘侧：后台扫描老化/被覆盖的条目。当前 daemon 没有 metadata 更新接口，
# 记不了命中频次，所以只能按「年龄（day/turn）+ 类型 + 同主题覆盖」判，
# 不做命中次数遗忘。默认 dry-run：只记候选日志不真删，阈值确认后再放开。
_STALE_SIM = 0.42                 # 高于此值视为同一主题的新旧两版
_RAW_KEEP_BATCHES = 6             # 原话层保留最近几批（一批约 5 轮）
_FORGET_DRY_RUN = True            # True=只扫描记录候选；False=真删


def _meta(doc):
    m = (doc or {}).get("metadata")
    return m if isinstance(m, dict) else {}


def _age_key(doc):
    """条目新旧排序键：先游戏天数，再批次末轮次；缺失按 0（最老）。"""
    m = _meta(doc)
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return (_int(m.get("day")), _int(m.get("turn_end") or m.get("turn")))


def _kind(doc):
    return str(_meta(doc).get("kind") or "")


def _is_parent(doc):
    """父块：新 parent 或旧 batch（兜底兼容）。"""
    return _kind(doc) == _KIND_PARENT or _kind(doc) in _LEGACY_PARENT_KINDS


def _is_sub(doc):
    """子块：新 subfact、旧 fact，或缺 kind 的老提炼条目（兜底兼容）。"""
    k = _kind(doc)
    return k == "" or k == _KIND_SUB or k in _LEGACY_SUB_KINDS


def _is_fact(doc):
    # 兼容旧名：时效裁决规则对子块（新 subfact / 旧 fact）同样适用。
    return _is_sub(doc)


def _superseded_dedupe(docs, sim=_STALE_SIM):
    """同主题事实只留较新的一条；其余类型原样保留。

    两条 fact 文本相似度达到 `sim` 判为同一主题的新旧两版，按 day/turn
    留新剔旧。相似度闸值比 MMR 的 dup(0.30) 高，宁可漏压也不误删不同侧面。
    """
    if not docs:
        return []
    facts = [d for d in docs if _is_fact(d)]
    kept_facts = []
    for d in facts:
        dup_with = None
        for i, k in enumerate(kept_facts):
            if _text_sim(d.get("content") or d.get("text") or "",
                         k.get("content") or k.get("text") or "") >= sim:
                dup_with = i
                break
        if dup_with is None:
            kept_facts.append(d)
            continue
        k = kept_facts[dup_with]
        # 保留较新的一条；同年龄时保留先召回（相关性更高）的那条。
        if _age_key(d) > _age_key(k):
            kept_facts[dup_with] = d
    kept_ids = {str(d.get("id") or "") for d in kept_facts}
    out = [d for d in docs if not _is_fact(d) or str(d.get("id") or "") in kept_ids]
    return out


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


def _rerank_before_mmr(docs, k, query):
    """重排候选 → 再交给 MMR 去重（2026-11-16）。

    向量召回粗排出来的候选先经 cross-encoder 逐对精算，再按原来的 MMR
    规则挑不重复的 k 条。重排只是增强：模型没就绪时 rerank 原样返回，
    行为与以前完全一致。打日志只为演示页/排查能看到这一层是否生效。
    """
    ranked = rerank.rerank(query, docs)
    if ranked is not docs and ranked != docs:
        log.info("[回忆] cross-encoder 重排 %d 条候选", len(ranked))
    return mmr_select(ranked, k)


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
            out = [d for d in (r.get("docs") or []) if _is_sub(d)]
            if out:
                out = _superseded_dedupe(out)
                log.info("[回忆] %s 按第 %d 天命中 %d 条（query=%r）",
                         line, day_q, len(out), query[:40])
                return _rerank_before_mmr(out, kk_out, query)
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
        # 向量候选只认子块；父块走挂载、锚点走规则置顶、原话层不再取用。
        if not _is_sub(d):
            continue
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
    # 时效裁决：同主题旧版事实被新版覆盖，不重复端上桌。
    out = _superseded_dedupe(out)
    log.info("[回忆] %s 命中 %d 条（query=%r，阈值=%.2f，时间问法=%s）",
             line, len(out), query[:40], limit, time_q)
    return _rerank_before_mmr(out, kk_out, query)


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
    # 原话层重复更凶（同一个场景被记三四遍），重排后 MMR 挑完再给。
    return _rerank_before_mmr(out, kk, query)


# 注入块名额（2026-11-09）：锚点不看相关性、最多 8 条；批次叙事 2 条；
# 按相关性挑出来的旧事实 3 条；recent 那边最多再带 1 条自己的事实。
# 2026-11-10 只把「按相关性召回的旧事实」从 3 放到 4：用户报跨天细节
# 记不住时，被切掉的第 4 条常常正好是那个具体物件或地点。近期批次与
# recent 名额一律不动——它们带的是昨天的批次叙事，多带反而会把已经
# 办完的约定反复端上桌（同一天报的另一个症状）。
_ANCHOR_SLOTS = 8
_BATCH_SLOTS = 2
_FACT_SLOTS = 4
_RECENT_FACT_SLOTS = 1


def _lines_with(line, extra_lines):
    """主线 + 只读附加库，去重保序。

    2026-11-15：展示页要演「知识库 + 个人记忆」两层，母本库只读、访客线
    可写，检索时两路合并。这里只做去重，不改任何检索参数。
    """
    out = []
    for item in (line,) + tuple(extra_lines or ()):
        item = str(item or "").strip()
        if item and item not in out:
            out.append(item)
    return out


def _doc_distance(doc):
    """取距离；缺失按 1.0（最远）算，0.0 是合法值不能当缺失。

    `_recent_docs` 出来的条目没有 distance 字段，用 `or 1.0` 会把真正
    distance=0（同一段文本）也压到最后。
    """
    value = (doc or {}).get("distance")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


def _rank_merged(docs, limit):
    """多库合并：按距离升序、id 去重、截断。

    只在 `extra_lines` 非空时使用。单库路径继续用 `retrieve` 自己的 MMR
    顺序，保证既有角色扮演的表现一点不变。
    """
    seen = set()
    out = []
    for d in sorted(docs, key=_doc_distance):
        key = str((d or {}).get("id") or "")
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(d)
    return out[:max(1, int(limit))]


# RRF（Reciprocal Rank Fusion）融合常数（2026-11-20 纯向量双查询）。
# 标准 k=60：位次越靠前贡献越大，k 压住第一名和后面名次的分差，避免一路
# 的绝对距离尺度压过另一路。
_RRF_K = 60


def _rrf_merge(lists, rrf_k=_RRF_K):
    """多路排名按位次融合，不看各路绝对距离。

    纯向量双查询（消解短语 + 用户原话）各召回一路：两路 embedding 分布
    不同，直接拼接会让某一路的距离尺度压过另一路。RRF 只用各路内部排名，
    score = Σ 1/(k + rank)；同一条在多路都靠前就排前面，只在一路出现也有
    基础分。id 为空的条目用对象身份兜底，保证不会互相覆盖。
    """
    scores, holder = {}, {}
    for lst in lists:
        for rank, d in enumerate(lst or []):
            eid = str((d or {}).get("id") or "") or id(d)
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (rrf_k + rank + 1)
            holder.setdefault(eid, d)
    return [holder[eid] for eid, _ in
            sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def _trace_doc(doc, source):
    """展示页用的只读检索轨迹；不参与排序，也不改变原有注入块。"""
    metadata = (doc or {}).get("metadata") or {}
    # 缺 distance 的条目（`_recent_docs` / `_anchor_docs` 补的）没有真实距离：
    # 报 0.000 会被读成「完美命中」，报 1.000 又像「最不相关」。单独标出来。
    try:
        distance = float((doc or {}).get("distance"))
    except (TypeError, ValueError):
        distance = None
    return {
        "id": str((doc or {}).get("id") or ""),
        "source": source,
        "line": str((doc or {}).get("_line") or ""),
        "kind": str(metadata.get("kind") or ""),
        "content": str((doc or {}).get("content") or "")[:1200],
        "distance": distance,
        "similarity": None if distance is None else max(0.0, 1.0 - distance),
        "no_distance": distance is None,
    }


def _merge_parent_parts(parents):
    """同 `group_id` 的多个 part 按序拼成一条父块；返回去重保序的列表。

    父块超长时拆成 part1/part2 入库，注入时要连成完整一段，不能各带一个
    标题拆开摆。旧 batch 没有 group_id，按自身 id 成组。
    """
    by_group, order = {}, []
    for d in parents:
        m = _meta(d)
        gid = str(m.get("group_id") or d.get("id") or "")
        if gid not in by_group:
            by_group[gid] = []
            order.append(gid)
        by_group[gid].append(d)
    merged = []
    for gid in order:
        parts = by_group[gid]
        try:
            parts.sort(key=lambda e: int(_meta(e).get("part") or 1))
        except (TypeError, ValueError):
            pass
        base = dict(parts[0])
        base["content"] = "".join(str(p.get("content") or "") for p in parts)
        merged.append(base)
    return merged


def _fact_block(docs, recent=(), mounted=(), trace=None):
    """长期记忆 → 一段；没有可用的返回空串。

    分层（锚点置顶）：锚点 → 父块叙事（挂载 + 近期批次）→ 子块事实。
    `docs` 按相关性排好；`recent` 是最近几批无条件带上的语境，只补父块
    名额和一条子块名额，不跟旧子块抢 `_FACT_SLOTS`。`mounted` 是命中子块
    按 group_id 挂载回来的父块。
    """
    def _split(items):
        ps, ss = [], []
        for d in items:
            if _is_parent(d):
                ps.append(d)
            elif _is_sub(d):
                ss.append(d)
            # raw / anchor 不在此分层，忽略
        return ps, ss

    anchors = [d for d in docs if (d.get("metadata") or {}).get("_anchor")]
    _aid = {id(d) for d in anchors}
    p_docs, s_docs = _split(docs)
    p_docs = [d for d in p_docs if id(d) not in _aid]
    s_docs = [d for d in s_docs if id(d) not in _aid]
    r_parents, r_subs = _split(recent)
    m_parents, _ = _split(mounted)

    parents = _merge_parent_parts(r_parents + m_parents + p_docs)[:_BATCH_SLOTS]
    subs = s_docs[:_FACT_SLOTS] + r_subs[:_RECENT_FACT_SLOTS]

    pick = anchors[:_ANCHOR_SLOTS] + parents + subs
    seen = set()
    pick = [d for d in pick
            if id(d) not in seen and not seen.add(id(d))]
    if not pick:
        return ""
    who = {"user": "用户事实", "character": "角色言行", "shared": "双方约定"}
    selected = []
    lines = ["【长期记忆 · 从过去提炼出的经过与事实】\n"
             "下面都是当时记下的话；里面的「明天」「下次」「约定」是那一轮的说法，"
             "不代表现在还欠着。想提旧事照提，用已经发生过的口吻。"]
    for d in pick:
        meta = d.get("metadata") or {}
        if meta.get("_anchor"):
            # 锚点单独标出来（2026-11-08）：以前它们混进 facts，顶着
            # 「[双方约定]」这种标签，读起来像普通的计划，模型不会当回事。
            head = "[锚点·只发生过一次]"
            limit = 200
        elif _is_parent(d):
            head = "[当时的经过]"
            limit = 1200
        else:
            about = who.get(str(meta.get("about") or ""), "记忆")
            head = "[%s]" % about
            limit = _SUB_SOFT_MAX
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
        selected.append(d)
    if trace is not None:
        trace.setdefault("selected", []).extend(
            _trace_doc(d, "distilled") for d in selected)
    return "\n".join(lines) if len(lines) > 1 else ""


def _raw_block(docs, trace=None):
    """原文条目 → 一段；总字数受 RAW_BLOCK_CHARS 限制。

    2026-11-10：单条不再硬截 520 字（见 RAW_ITEM_CHARS）；预算不够时
    按剩余字数截这一条，而不是把整条丢掉——细节往往就在后半段。
    """
    if not docs:
        return ""
    lines, used = [], 0
    selected = []
    for d in docs:
        text = " ".join(str(d.get("content") or "").split())
        if not text:
            continue
        if len(text) > RAW_ITEM_CHARS:
            text = text[:RAW_ITEM_CHARS] + "…"
        left = RAW_BLOCK_CHARS - used
        if lines and left < 120:
            break
        if len(text) > left:
            text = text[:left].rstrip() + "…"
        lines.append("- " + text)
        selected.append(d)
        used += len(text)
    if not lines:
        return ""
    if trace is not None:
        trace.setdefault("selected", []).extend(
            _trace_doc(d, "raw") for d in selected)
    return "【当时的原话 · 用来确认说过和做过什么】\n" + "\n".join(lines)


def build_block(line, query, k=None, day=None, context="", trace=None,
                extra_lines=(), max_hits=None, query_raw=""):
    """拼成能直接塞进最后一条玩家消息的一段；没命中返回空串。

    `extra_lines`（2026-11-15）：额外的只读库。展示页用「母本知识库 +
    访客私有线」两路合并检索；为空时走原来的单库路径，返回值和顺序
    与改动前完全一致。

    `max_hits`（2026-11-16）：召回候选的上限，展示页把它做成可调项
    （5 / 10 / 20）。只截候选，不动阈值——被截掉的是距离更远的那些，
    本来也进不了注入块。锚点与最近批次是规则补入，不受这个上限约束。
    默认 None = 不截断，生产路径一点不变。

    2026-11-06：两路检索——当前用户话 + 最近几轮上下文，合并去重后
    优先取 2 条父块叙事 + 若干条子块事实。问「昨天」这种短句时，
    上下文那一路能把「昨天在哪、和谁、发生了什么」带出来。

    V3 父子块：向量只召回子块；命中后按 group_id 挂载同组父块补语境。
    原话层已取消，新数据不再单独存原文。

    `trace` 是展示页的可选只读输出：传入 dict 时记录 query、召回条目、
    距离与最终入选项；默认 None，原有调用方的行为和返回值完全不变。
    """
    lines = _lines_with(line, extra_lines)
    multi = len(lines) > 1

    def _rec(q):
        if not multi:
            return retrieve(line, q, k=k, day=day)
        pooled = []
        for ln in lines:
            for d in retrieve(ln, q, k=k, day=day):
                d["_line"] = ln
                pooled.append(d)
        return _rank_merged(pooled, k or PERSONA_RAG_K)

    # 纯向量双查询（2026-11-20）：消解短语一路、用户原话一路，RRF 按位次
    # 融合。两路文本相同（memory_query 空退回原话）时只查一路，不重复烧
    # embedding。向量候选只认子块；父块命中后按 group_id 挂载，不参与召回。
    docs_lists = [_rec(query)]
    qr = str(query_raw or "").strip()
    if qr and qr != str(query or "").strip():
        docs_lists.append(_rec(qr))
    docs = _rrf_merge(docs_lists)
    if max_hits:
        docs = docs[:max_hits]
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
        recent = [d for d in recent if str(d.get("id") or "") not in seen]
    if trace is not None:
        trace.clear()
        trace.update({
            "query": str(query or ""),
            "query_raw": qr,
            "dual_query": len(docs_lists) > 1,
            "context_query": str(context or ""),
            "hit_count": len(docs),
            "hits": [_trace_doc(d, "distilled") for d in docs],
            "recent_count": len(recent),
            "selected": [],
            "injected_count": 0,
            "block": "",
        })
    if context:
        extra = _rec(context)
        if max_hits:
            extra = extra[:max_hits]
        seen = {str(d.get("id") or "") for d in docs}
        docs += [d for d in extra
                 if str(d.get("id") or "") not in seen]
        if trace is not None:
            trace["context_hit_count"] = len(extra)
            trace["context_hits"] = [_trace_doc(d, "context") for d in extra]
    # 父块挂载：只从命中子块取 group_id，锚点/近期批次不参与挂载。
    mounted = _mount_parents(line, [d for d in docs if _is_sub(d)],
                             extra_lines)
    if trace is not None:
        trace["mounted_count"] = len(mounted)
    parts = []
    fact = _fact_block(docs, recent, mounted=mounted, trace=trace)
    if fact:
        parts.append(fact)
    if not parts:
        return ""
    parts.append(
        "【用法】用户问起自己/双方/角色的既有事实，或「昨天/上次发生过什么」时，"
        "以此为准。父块是当时整批经过的概括，子块是记下的具体事实；"
        "当前这一拍接着往下演，正在发生的比旧话更重要。"
        "旧事实只能补充，不会被近期闲聊覆盖。"
        "她主动提昨天的事是好的，用已经发生过的口吻提；里面写到「明天」"
        "「下次」的约定都是当时那一轮的说法，不代表今天还欠着。")
    out = "\n\n".join(parts)
    if trace is not None:
        selected = trace.get("selected") or []
        trace["injected_count"] = len({
            (item.get("source"), item.get("id")) for item in selected})
        trace["block"] = out
    return out


def _recover_persisted_ops(client):
    """重启后回放没执行完的 drop；库已不存在就直接摘除待办。"""
    with _PENDING_LOCK:
        ops = list(_ops_load())
    for op in ops:
        if op.get("op") != "drop":
            continue
        ln = str(op.get("line") or "")
        if not ln:
            continue
        coll = collection_name(ln)
        listed = client.list_kb().get("collections") or []
        if coll not in listed:
            _op_done("drop", ln)
            continue
        r = client.delete_kb(coll)
        if r.get("ok"):
            with _LOCK:
                _BLOCKED.discard(ln)
            _op_done("drop", ln)
        else:
            log.warning("重启回放清库失败 %s", coll)


def _purge_shells(client):
    """删掉 count=0 的空壳 collection（2026-11-20 清理加固）。"""
    r = client.list_kb()
    if not r.get("ok"):
        return
    counts = r.get("counts") or {}
    for name in r.get("collections") or []:
        try:
            if int(counts.get(name) or 0) == 0:
                client.delete_kb(name)
        except Exception:                                    # noqa: BLE001
            log.warning("空壳清理失败 %s", name)


def _stale_fact_ids(facts, sim=_STALE_SIM):
    """被更新版本覆盖的旧 fact id 列表（与 `_superseded_dedupe` 同源规则）。"""
    kept = []
    stale = []
    for d in facts:
        hit = None
        for i, k in enumerate(kept):
            if _text_sim(d.get("content") or d.get("text") or "",
                         k.get("content") or k.get("text") or "") >= sim:
                hit = i
                break
        if hit is None:
            kept.append(d)
            continue
        k = kept[hit]
        if _age_key(d) > _age_key(k):
            stale.append(str(k.get("id") or ""))
            kept[hit] = d
        else:
            stale.append(str(d.get("id") or ""))
    return [x for x in stale if x]


def _scan_stale(client, coll, line, dry_run=None):
    """扫描一条线库里的老化/被覆盖条目；返回删除条数（dry-run 返回候选数）。

    规则（保守，宁可漏删不误删）：
      · anchor / batch 永不自动删——锚点是只发生一次的节点，批次叙事是经过存档；
      · raw 原话：只保留最近 `_RAW_KEEP_BATCHES` 批（一批 INGEST_EVERY 轮），
        更早的细节入候选；
      · fact：与更新 fact 同主题重复的旧版入候选（偏好/承诺不按年龄单独删）。

    `dry_run` 默认取模块级 `_FORGET_DRY_RUN`：True 只记候选日志不真删；
    False 才走 `delete_entries`。当前 daemon 没有 metadata 更新接口，记不了
    命中频次，所以这一轮遗忘只按年龄 + 覆盖判，不做命中次数淘汰。
    """
    if dry_run is None:
        dry_run = _FORGET_DRY_RUN
    try:
        r = client.list_entries(coll, timeout=PERSONA_RAG_TIMEOUT)
    except Exception:                                        # noqa: BLE001
        log.exception("遗忘扫描读取失败 %s", coll)
        return 0
    entries = r.get("entries") or []
    if not entries:
        return 0
    facts, raws = [], []
    for e in entries:
        if _is_sub(e):
            facts.append(e)
        elif _kind(e) == _KIND_RAW:
            raws.append(e)
    stale_facts = _stale_fact_ids(facts)
    # 原话老化：按轮次排序，保留最近 N 批（一批 INGEST_EVERY 轮），更早的入候选。
    def _turn(e):
        try:
            return int(_meta(e).get("turn") or 0)
        except (TypeError, ValueError):
            return 0
    keep_n = _RAW_KEEP_BATCHES * max(1, int(PERSONA_RAG_INGEST_EVERY or 5))
    keep_turns = sorted({_turn(e) for e in raws})[-keep_n:]
    keep_set = set(keep_turns)
    stale_raws = []
    for e in raws:
        if _turn(e) not in keep_set:
            eid = str(e.get("id") or "")
            if eid:
                stale_raws.append(eid)
    stale_ids = [x for x in dict.fromkeys(stale_facts + stale_raws) if x]
    if not stale_ids:
        return 0
    if dry_run:
        log.info("[遗忘·dry-run] %s 候选 %d 条（fact 覆盖 %d，raw 老化 %d）",
                 line, len(stale_ids), len(stale_facts), len(stale_raws))
        return len(stale_ids)
    try:
        r = client.delete_entries(coll, stale_ids)
        n = r.get("deleted") if isinstance(r, dict) else r
        log.info("[遗忘] %s 删除 %d 条老化/被覆盖条目", line, n or 0)
        return int(n or 0)
    except Exception:                                        # noqa: BLE001
        log.exception("遗忘清理失败 %s", coll)
        return 0


def _scan_all_stale(client):
    """warm 时遍历待提炼缓冲里出现过的真人线，逐库跑遗忘扫描。

    母本知识库不经过 remember、不在 pending 键里，天然不会被扫到；扫描器
    内部也只认本库 source。周期化定期扫描留待后续，本轮只在启动时扫一轮。
    """
    with _PENDING_LOCK:
        data = _pending_load()
    lines = [k for k in data if k != "_ops" and isinstance(k, str)]
    for ln in lines:
        try:
            _scan_stale(client, collection_name(ln), ln)
        except Exception:                                    # noqa: BLE001
            log.warning("遗忘扫描失败 %s", ln)


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
        # 回填任务被静默丢掉的话，本进程的「已排过」标记会让它永不重试。
        if kind == "backfill":
            with _LOCK:
                _BACKFILL_QUEUED.discard(line)
        return
    client = _client()
    if not client.is_ready():
        if not client.start():
            _FAIL_UNTIL = time.time() + 300
            log.warning("角色扮演 RAG 启动失败，5 分钟内不再重试")
            if kind == "backfill":
                with _LOCK:
                    _BACKFILL_QUEUED.discard(line)
            return
    if kind == "warm":
        _recover_persisted_ops(client)
        _purge_shells(client)
        _scan_all_stale(client)
        return
    coll = collection_name(line)
    if kind == "backfill":
        # V3：归档回填不再写旧原话层，配对成轮次后走提炼，产出父子块。
        # 失败不阻塞后续队列；无论成败都清掉「已排过」标记，允许下次重排。
        try:
            turns = _rows_to_turns((payload or {}).get("rows") or [])
            if turns:
                _backfill_distill(client, coll, line, turns,
                                  card_name=str((payload or {}).get(
                                      "card_name") or "角色"),
                                  custom=(payload or {}).get("custom"))
        except Exception:                                    # noqa: BLE001
            log.exception("归档回填失败 %s", coll)
        finally:
            with _LOCK:
                _BACKFILL_QUEUED.discard(line)
        return
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
        # V3：不再单独入库原话层。原话属于压缩记忆的一部分，由父块叙事
        # 概括保留；新数据只走提炼后的父子块。旧库残留 raw 由遗忘扫描自然清理。
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
        r = client.ingest(coll, _with_line(entries, line))
        if not r.get("ok"):
            log.warning("记忆入库失败 %s：%s", coll, r.get("error"))
        return
    if kind == "drop":
        r = client.delete_kb(coll)
        if r.get("ok"):
            with _LOCK:
                _BLOCKED.discard(line)
            _op_done("drop", line)
        else:
            log.warning("记忆清库失败 %s：%s", coll, r.get("error"))
        return
    if kind == "forget":
        start = int((payload or {}).get("start") or 0)
        end = int((payload or {}).get("end") or 0)
        ids = [str(x) for x in ((payload or {}).get("batch_ids") or []) if x]
        if start and end:
            # 父块若拆过 part，part2/part3 的 id 是 `<group_id>-p2/-p3`。
            ids += ["%s-p%d" % (g, p) for g in ids for p in (2, 3)]
            # 兜底按轮次范围读出本批全部条目（子块/锚点/父块/旧原话）整批
            # 撤掉——V3 子块不再限 4 条，靠拼 id 会漏；读全量最稳。
            try:
                r = client.list_entries(coll, timeout=PERSONA_RAG_TIMEOUT)
                for e in r.get("entries") or []:
                    m = _meta(e)
                    try:
                        ts = int(m.get("turn_start") or m.get("turn") or 0)
                        te = int(m.get("turn_end") or m.get("turn") or 0)
                    except (TypeError, ValueError):
                        continue
                    if ts == start and te == end:
                        eid = str(e.get("id") or "")
                        if eid:
                            ids.append(eid)
            except Exception:                            # noqa: BLE001
                log.exception("回退清理读取批次失败 %s", coll)
            # 旧原话 id 兜底（list_entries 不可用时也能撤掉残留）。
            for t in range(start, end + 1):
                ids += ["raw-%d-u" % t, "raw-%d-a" % t]
        ids = [x for x in dict.fromkeys(ids) if x]
        try:
            if hasattr(client, "delete_entries") and len(ids) > 1:
                client.delete_entries(coll, ids)
            else:
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

    返回里父块是 `kind=parent` 的整批叙事（如果模型给了 summary），
    后面是 `kind=subfact` 的子块事实，另有 `kind=anchor` 锚点。
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
    # 2026-11-09：后台线程要重建**同一条**扮演降级链。空列表也要挂上
    # （`[]` ≠ None）——没挂就会退回全局链，中转站闭站时提炼照样烧官方的钱。
    if custom is not None:
        llm.set_custom_override(custom)
    try:
        # 链头照样是公用中转站 V4 Pro，玩家的自定义模型只是第二段。
        kw = {}
        if PERSONA_PROVIDER:
            kw["provider"] = PERSONA_PROVIDER
        if PERSONA_MODEL:
            kw["model"] = PERSONA_MODEL
        raw = llm.call_llm([{"role": "user", "content": prompt}],
                           temperature=0.2, max_tokens=2600, **kw)
    except Exception:                                    # noqa: BLE001
        log.exception("记忆提炼调用失败 %s-%s", start, end)
        return None
    finally:
        if custom is not None:
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


def _split_parent(text, max_chars=_PARENT_MAX_CHARS):
    """父块超长时按句读边界切成多段；返回非空 list。

    父块不参与向量召回，拆 part 只为注入预算与历史兼容。尽量在句号/问号/
    分号处收段，不拦腰切断一句话；实在切不开才退回硬边界。
    """
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts, buf = [], ""
    for s in re.split(r"(?<=[。！？!?；;])", text):
        if not s:
            continue
        # 单句本身就超长：先按硬边界切开，避免整段压成一块。
        while len(s) > max_chars:
            if buf:
                parts.append(buf.rstrip())
                buf = ""
            parts.append(s[:max_chars])
            s = s[max_chars:]
        if buf and len(buf) + len(s) > max_chars:
            parts.append(buf.rstrip())
            buf = s
        else:
            buf += s
    if buf.strip():
        parts.append(buf.rstrip())
    return parts or [text[:max_chars]]


def parse_memory_json(raw, start, end, day=0, phase="", card_name="角色",
                      epoch=0, batch_id=""):
    """解析提炼器输出 → Chroma entries。坏 JSON / 空内容一律返回 []。

    V3 父子块：`summary` → 父块 `kind=parent`（同 `group_id`，超长拆 part）；
    `facts`（兼容旧 `entries`）→ 子块 `kind=subfact`，挂 `parent_group_id`。
    父块带「第 N 天 · 时段」前缀。
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
    group_id = str(batch_id or ("batch-%d-%d-%d"
                                % (start, end, int(epoch or 0))))
    summary = " ".join(str(data.get("summary") or "").split())
    if summary:
        # day=0 不写前缀（2026-11-08）：补录归档时不知道历史轮次属于第几天，
        # 强写「【第0天】」比不写更误导。
        prefix = ("【第%d天%s】" % (int(day or 0),
                                 (" · " + str(phase)) if phase else "")
                  if int(day or 0) else "")
        parts = _split_parent(prefix + summary)
        total = len(parts)
        for pi, seg in enumerate(parts, start=1):
            pid = group_id if total == 1 else "%s-p%d" % (group_id, pi)
            out.append({
                "id": pid,
                "content": seg,
                "metadata": {
                    "kind": _KIND_PARENT,
                    "group_id": group_id,
                    "part": pi,
                    "part_total": total,
                    "about": "shared",
                    "day": int(day or 0),
                    "phase": str(phase or "")[:12],
                    "turn_start": start,
                    "turn_end": end,
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
                    "kind": _KIND_ANCHOR,
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
    raw_facts = data.get("facts")
    if not isinstance(raw_facts, list):
        raw_facts = data.get("entries")     # 兼容旧提炼输出字段名
    if isinstance(raw_facts, list):
        for i, e in enumerate(raw_facts):
            if not isinstance(e, dict):
                continue
            content = str(e.get("content") or "").strip()
            if not content:
                continue
            about = _norm_about(e.get("about"), content, card_name)
            content = _ensure_prefix(about, content, card_name)
            # V3：去掉 4 条硬上限与 160 字硬截断——一条一事由 prompt 约束，
            # 代码不再拦腰切断完整事实。
            topics = e.get("topics") if isinstance(e.get("topics"), list) else []
            opens = e.get("openThreads") if isinstance(e.get("openThreads"), list) else []
            out.append({
                "id": "%d-%d-%d" % (start, end, i),
                "content": content,
                "metadata": {
                    "kind": _KIND_SUB,
                    "about": about,
                    "day": int(day or 0),
                    "phase": str(phase or "")[:12],
                    "turn_start": start,
                    "turn_end": end,
                    "parent_group_id": group_id,
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

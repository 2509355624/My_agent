# -*- coding: utf-8 -*-
"""长期记忆的渲染层（2026-11-18）。

`rag.py` 只负责检索/入库/回填；本文件负责把检索结果挑成注入块：
  · `_fact_block` —— 锚点 / 批次 / 原子事实的标签与名额；
  · `_raw_block`  —— 原文层的预算；
  · `build_block` —— 两路检索 + 三态（detail / summary）拼装；
    detail 可传 `lean=True`，按问法只取锚点或原文中真正需要的那一层。

所有检索都通过 `rag` 模块对象调用，不直接 import 函数——这样测试和
展示页对 `persona_rag.retrieve` / `_anchor_docs` 的 patch 仍然生效，
也避免两个模块互相 import 形成环。
"""
from app.features.persona import rag as store
from app.features.persona import rag_modes


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


def _fact_block(docs, recent=(), trace=None, mode="detail"):
    """提炼事实 → 一段；没有可用的返回空串。

    `docs` 是按相关性排好的（锚点在最前）。`recent` 单独传：它是「最近 2 批
    无条件带上」（2026-11-08 用户拍板），但它只补批次名额和一条事实名额，
    不跟旧事实抢 `_FACT_SLOTS`——以前 recent 直接拼在 docs 最前面，三条事实
    名额全被近期占掉，检索明明召回了旧事也端不上桌（2026-11-09 母本副本
    实测：14 个窗口外考点，可召回 9 个，进注入块只有 4 个）。

    `mode`（2026-11-18）：detail 保持上面的全量名额；summary 走
    `rag_modes.pick_summary()`，批次优先、只留少量事实，不碰锚点。
    """
    summary = rag_modes.is_summary(mode)
    if summary:
        pick = rag_modes.pick_summary(docs, recent)
    else:
        def _split(items):
            bs, fs = [], []
            for d in items:
                kind = str((d.get("metadata") or {}).get("kind") or "")
                if kind == "raw":
                    # 原话条目走 `_raw_block`；混进事实层会被当成「原子事实」读。
                    continue
                (bs if kind == "batch" else fs).append(d)
            return bs, fs

        batches, facts = _split(docs)
        anchors = [d for d in docs if (d.get("metadata") or {}).get("_anchor")]
        _aid = {id(d) for d in anchors}
        batches = [d for d in batches if id(d) not in _aid]
        facts = [d for d in facts if id(d) not in _aid]
        r_batches, r_facts = _split(recent)
        pick = (anchors[:store._ANCHOR_SLOTS]
                + (r_batches + batches)[:store._BATCH_SLOTS]
                + facts[:store._FACT_SLOTS]
                + r_facts[:store._RECENT_FACT_SLOTS])
    seen = set()
    pick = [d for d in pick
            if id(d) not in seen and not seen.add(id(d))]
    if not pick:
        return ""
    who = {"user": "用户事实", "character": "角色言行", "shared": "双方约定"}
    selected = []
    if summary:
        lines = ["【长期记忆 · 当时的经过（批次摘要）】\n"
                 "下面按批次记的是当时发生过的经过；里面的「明天」「下次」"
                 "是那一轮的说法，不代表现在还欠着。"]
    else:
        lines = ["【长期记忆 · 从过去提炼出的经过与事实】\n"
                 "下面都是当时记下的话；里面的「明天」「下次」「约定」是那一轮的说法，"
                 "不代表现在还欠着。想提旧事照提，用已经发生过的口吻。"]
    for d in pick:
        meta = d.get("metadata") or {}
        if summary:
            if str(meta.get("kind") or "") == "batch":
                head = "[当时的经过]"
                limit = 320
            else:
                head = "[%s]" % who.get(str(meta.get("about") or ""), "记忆")
                limit = 160
        elif meta.get("_anchor"):
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
        if len(text) > store.RAW_ITEM_CHARS:
            text = text[:store.RAW_ITEM_CHARS] + "…"
        left = store.RAW_BLOCK_CHARS - used
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
                extra_lines=(), max_hits=None, mode="detail", lean=False):
    """拼成能直接塞进最后一条玩家消息的一段；没命中返回空串。

    `mode`（2026-11-18）：编剧三态路由的落点。
      · detail —— 锚点 + 批次 + 事实 + 原文；
      · summary —— 批次优先，只留少量事实，不拿原文和锚点；
      · none —— 调用方直接跳过本函数（这里也按 detail 处理，防止误传）。
    旧调用方不传 mode，默认 detail，返回值仍按老行为。

    `lean`（2026-11-18）：只对 detail 生效。泛问「上次」时不再无条件
    拖上 1,600 字原话和十几条锚点；只有问法里出现「第一次/初夜」才取
    锚点，出现「原话/当时说/细节」才取原文。summary 本来就两样都不拿。

    `extra_lines`（2026-11-15）：额外的只读库。展示页用「母本知识库 +
    访客私有线」两路合并检索；为空时走原来的单库路径，返回值和顺序
    与改动前完全一致。

    `max_hits`（2026-11-16）：召回候选的上限，展示页把它做成可调项
    （5 / 10 / 20）。只截候选，不动阈值——被截掉的是距离更远的那些，
    本来也进不了注入块。锚点与最近批次是规则补入，不受这个上限约束。
    默认 None = 不截断，生产路径一点不变。

    2026-11-06：两路检索——当前用户话 + 最近几轮上下文，合并去重后
    优先取 2 条批次叙事 + 3 条原子事实。问「昨天」这种短句时，
    上下文那一路能把「昨天在哪、和谁、发生了什么」带出来。

    2026-11-07：加原文层。两层分工写进块标题——提炼层说「经过与事实」，
    原文层说「用来确认说过和做过什么」，结尾用法句再点明「正在发生的
    比旧话更重要」，避免演员拿旧对话当台词复述。

    `trace` 是展示页的可选只读输出：传入 dict 时记录 query、召回条目、
    距离与最终入选项；默认 None，原有调用方的行为和返回值完全不变。
    """
    summary = rag_modes.is_summary(mode)
    lines = _lines_with(line, extra_lines)
    multi = len(lines) > 1

    def _rec(q):
        if not multi:
            return store.retrieve(line, q, k=k, day=day)
        pooled = []
        for ln in lines:
            for d in store.retrieve(ln, q, k=k, day=day):
                d["_line"] = ln
                pooled.append(d)
        return _rank_merged(pooled, k or store.PERSONA_RAG_K)

    def _rec_raw(q):
        if not multi:
            return store.retrieve_raw(line, q)
        pooled = []
        for ln in lines:
            for d in store.retrieve_raw(ln, q):
                d["_line"] = ln
                pooled.append(d)
        return _rank_merged(pooled, store.RAW_BLOCK_ITEMS)

    docs = _rec(query)
    if max_hits:
        docs = docs[:max_hits]
    # 锚点（第一次/初夜/关系确认…）不看距离，直接排最前，见 `_anchor_docs`。
    # summary 不拿锚点；detail + lean 也只在问法真的指向「第一次」时才拿，
    # 避免泛问「上次」时把十几条只发生一次的事实全塞进去。
    need_anchor = not summary and (not lean or rag_modes.needs_anchor(query))
    if not need_anchor:
        docs = [d for d in docs if not rag_modes.is_anchor(d)]
    if need_anchor:
        anchor = store._anchor_docs(line)
        if anchor:
            seen = {str(d.get("id") or "") for d in anchor}
            docs = anchor + [d for d in docs if str(d.get("id") or "") not in seen]
    # 最近 2 批无条件带上（2026-11-08 用户拍板）：向量对「昨天吃了什么」这种
    # 泛问经常算不准，最近发生的事却几乎总是相关的。多几百字，换「她记得」。
    recent = store._recent_docs(line, batches=2)
    if recent:
        seen = {str(d.get("id") or "") for d in docs}
        recent = [d for d in recent if str(d.get("id") or "") not in seen]
    if trace is not None:
        trace.clear()
        trace.update({
            "mode": rag_modes.normalize(mode),
            "lean": bool(lean),
            "query": str(query or ""),
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
    parts = []
    fact = _fact_block(docs, recent, trace=trace, mode=mode)
    if fact:
        parts.append(fact)
    raw = ""
    if not summary and (not lean or rag_modes.needs_quote(query)):
        raw = _raw_block(_rec_raw(query), trace=trace)
        if raw:
            parts.append(raw)
    if not parts:
        return ""
    if summary:
        parts.append(
            "【用法】用户问起「昨天/上次/之前」发生过什么时，以上面批次摘要为准；"
            "当前这一拍接着往下演，正在发生的比旧事更重要。"
            "旧事只能补充，不会被近期闲聊覆盖；里面写到「明天」「下次」的约定"
            "都是当时那一轮的说法，不代表今天还欠着。")
    else:
        raw_note = ("原话是当时确实说过、做过的细节，用来说明「是谁说的、"
                    "当时怎么说的」；" if raw else "")
        parts.append(
            "【用法】用户问起自己/双方/角色的既有事实，或「昨天/上次发生过什么」时，"
            "以此为准。" + raw_note +
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

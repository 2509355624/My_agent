# -*- coding: utf-8 -*-
"""RAG 三态的纯策略层（2026-11-18）。

只回答「这一轮该拿哪几条」，不碰向量库、不拼字符串：
  · detail  —— 锚点 + 最近批次 + 检索事实 + 原文，原行为不动；
  · summary —— 批次摘要优先，只留少量稳定事实，不拿原文和锚点；
  · none    —— 调用方直接跳过检索，不进入这里。

`rag.py` 负责检索和渲染，本文件只做取舍，避免两条路径的阈值/名额
散落在同一个大函数里。
"""

MODE_NONE = "none"
MODE_SUMMARY = "summary"
MODE_DETAIL = "detail"

# summary 的名额（2026-11-18 用户拍板）：
# 批次是「上次发生过什么」的主答案；原子事实只留稳定信息，原文和锚点
# 交给 detail。4 批 × 320 字 + 2 条事实 × 160 字，仍明显低于全量注入。
SUMMARY_BATCH_SLOTS = 4
SUMMARY_FACT_SLOTS = 2
SUMMARY_RECENT_FACT_SLOTS = 1


def normalize(mode):
    """只认三个合法档位；不认识的一律按 detail（旧调用方的默认）。"""
    value = str(mode or "").strip().lower()
    return value if value in (MODE_NONE, MODE_SUMMARY, MODE_DETAIL) else MODE_DETAIL


def is_summary(mode):
    return normalize(mode) == MODE_SUMMARY


def _kind(doc):
    return str(((doc or {}).get("metadata") or {}).get("kind") or "")


def _is_anchor(doc):
    meta = (doc or {}).get("metadata") or {}
    return bool(meta.get("_anchor")) or _kind(doc) == "anchor"


def _dedup(docs):
    seen = set()
    out = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        key = str(doc.get("id") or "")
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(doc)
    return out


def pick_summary(docs, recent=()):
    """summary 档位的取舍：最近批次在前，再补检索批次；事实只留少量。

    原文（raw）和锚点（anchor）不进入 summary —— 它们是 detail 的职责。
    这样既能把「上次发生了什么」说清楚，又不会把 1,600 字原话和十几条
    只发生一次的事实每轮都塞进去。
    """
    docs = list(docs or ())
    recent = list(recent or ())

    def split(items):
        batches, facts = [], []
        for doc in items:
            kind = _kind(doc)
            if kind == "raw" or _is_anchor(doc):
                continue
            (batches if kind == "batch" else facts).append(doc)
        return batches, facts

    batches, facts = split(docs)
    r_batches, r_facts = split(recent)
    picked = ((r_batches + batches)[:SUMMARY_BATCH_SLOTS]
              + facts[:SUMMARY_FACT_SLOTS]
              + r_facts[:SUMMARY_RECENT_FACT_SLOTS])
    return _dedup(picked)

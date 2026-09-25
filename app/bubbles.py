# -*- coding: utf-8 -*-
"""把一段 AI 回复拆成几条像真人手打的聊天气泡。

真人聊天不会一次甩一整面墙的字，而是连发几条短消息。业界做法（ChatLab、
SleekFlow 等产品的 Human-Like Messaging）是一致的：按句子拆条、条与条之间
留打字间隔、超过条数上限就合并——本模块只负责「拆条」，间隔在发送处加。

三级切分：段落（换行）→ 句子（。！？…~ 等收尾）→ 分句（，,；;）。
只在气泡边缘修剪标点，正文一个字不丢。切分是确定性的，方便测试。
"""

import re

# 气泡条数上限。超过就反复合并最短的相邻气泡——长回答也不能变成通知洪水。
DEFAULT_MAX_BUBBLES = 4

# 一段超过这个长度还全是逗号时，才动用分句级切分。短语一口气说完更像人。
_CLAUSE_THRESHOLD = 32

_SENTENCE_RE = re.compile(r"[^。！？!?…~\n]+[。！？!?…~]*[」』””）)]*")
_CLAUSE_RE = re.compile(r"[^，,；;\n]+[，,；;]*")
_URL_RE = re.compile(r"https?://")


def _sentences(paragraph):
    out = []
    for m in _SENTENCE_RE.finditer(paragraph):
        seg = m.group(0).strip()
        if seg:
            out.append(seg)
    return out


def _clauses(sentence):
    out = []
    for m in _CLAUSE_RE.finditer(sentence):
        seg = m.group(0).strip("，,；;、 ")
        if seg:
            out.append(seg)
    return out


def _merge_shortest(bubbles, limit):
    """超过条数上限时，反复把最短的相邻对合并。"""
    while len(bubbles) > limit:
        idx = min(range(len(bubbles) - 1),
                  key=lambda i: len(bubbles[i]) + len(bubbles[i + 1]))
        left, right = bubbles[idx], bubbles[idx + 1]
        # 左条已带句读就直接接上，别出现「一。，二。」这种撞标点
        sep = "" if re.search(r"[。！？!?…~，,；;]$", left) else "，"
        bubbles[idx:idx + 2] = [left + sep + right]
    return bubbles


def split_bubbles(text, max_bubbles=DEFAULT_MAX_BUBBLES):
    """把回复切成最多 max_bubbles 条气泡。

    短回复原样返回单条；含 URL 的段落整条保留（URL 里拆出半个链接就废了）。
    返回的列表拼起来 ≈ 原文（只少了气泡边缘的句读）。
    """
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not any(_URL_RE.search(p) for p in paragraphs):
        bubbles = []
        for p in paragraphs:
            for s in _sentences(p):
                if len(s) > _CLAUSE_THRESHOLD:
                    bubbles.extend(_clauses(s))
                else:
                    bubbles.append(s)
        if 1 < len(bubbles) <= max_bubbles:
            return bubbles
        if len(bubbles) > max_bubbles:
            return _merge_shortest(bubbles, max_bubbles)
    # 走到这：单段短句、或含 URL——整段一条
    merged = []
    for p in paragraphs:
        merged.append("，".join(_clauses(p)) if len(p) > _CLAUSE_THRESHOLD
                      and not _URL_RE.search(p) else p)
    return ["\n".join(merged)]

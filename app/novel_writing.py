# -*- coding: utf-8 -*-
"""小说模式的写作规范注入（2026-11-12）。

**和 `writing_skills.py` 的区别，一句话：那边看轴，这边不看。**

角色扮演面向「她」，规范按情感轴挑——`excite >= 55` 才给露骨层，
这种挑法在那边成立，因为代码知道她此刻到哪一步了。

小说模式面向**用户自己**。没有状态条、没有轴、没有「亲密程度」可以判，
用户写什么就是什么。所以这里**不做任何条件判断**：常驻层每轮全给，
成人技法也常驻——用户自己控制走向，我们保证「写得好」。

用户的定位（2026-11-12 原话）：
    「小说模式完全就是用户他自己去操控的一个世界……他只是记录当前做了
      什么东西。我们关键其实就是说怎么去把它写好，让用户看得爽就行了。」
    「不需要说什么常驻亲密程度……完全交给用户来自己去搞。就是用户他想
      怎么写，我就把他扩写就行了，就这么简单，我只要记住上下文，
      保证我的一个连贯，保证我的一个输出是好的。」

所以这个模块只做一件事：**把 30 万字技法库里「怎么写好」的部分，
按固定配方切出来拼成一段**，交给 `_writer`。

预算：~3000 字/轮（用户定的口径是「跟角色扮演差不多，一轮 3 万 token」）。
角色扮演那边规范约 2600 字，这边给到 3000 是留了余量——
小说正文本身要写 1000~3000 字，规范再长会开始挤历史。
"""

import logging
import os
import re

log = logging.getLogger("novel.writing")

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skills", "writing")

# 整段预算。超过就按顺序丢尾巴——**先丢的是最"锦上添花"的那几块**，
# 见 `_PLAN` 里的 order。
MAX_CHARS = 3000

# 单片上限。切片按行截，不把一句话砍成半截。
SLICE_MAX = 900


# ── 切片 ─────────────────────────────────────────────

def _path(rel):
    return os.path.join(_ROOT, *rel.split("/"))


def _drop_notes(body):
    """砍掉规范库里的**作者笔记**。

    原话（`writing_skills._drop_notes` 的注释）：规范库是给人看的，有些段尾
    挂着「用户裁决」「生成前内部规划」。原样塞进提示词，模型会把它当指令照做
    ——实测会把「用户裁决删除『逼』」读成一条禁令。

    所以这里做一次清洗，笔记不进正文。
    """
    if not body:
        return ""
    keep = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            keep.append(line)
            continue
        # 作者笔记的三种长相
        if s.startswith("**用户裁决**") or s.startswith("用户裁决"):
            break
        if "生成前内部规划" in s:
            break
        if re.match(r"^[（(【\[]?\s*(注|说明|备注)[:：]", s):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def _slice(rel, start="", end="", limit=SLICE_MAX):
    """切一段规范原文。**找不到就返回空串**，不抛——少一段规范不该让正文写不出来。"""
    try:
        with open(_path(rel), encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        log.warning("写作规范读取失败：%s", rel)
        return ""
    body = txt
    if start:
        i = txt.find(start)
        if i < 0:
            log.warning("写作规范切片标记缺失：%s → %r", rel, start)
            return ""
        body = txt[i:]
        if end:
            j = body.find(end, len(start))
            if j > 0:
                body = body[:j]
    body = _drop_notes(body)
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n…"
    return body.strip()


# ── 配方 ─────────────────────────────────────────────
#
# 每一块：(标题, 文件, 起始标记, 结束标记, 字数上限, 权重)
#
# 权重决定**预算不够时先丢谁**：数字小的先丢。
# 判断依据是「这一块没了，正文会不会明显变差」——
#   禁用词 / 人味技法 / 句式纪律  没了就明显变差 → 权重高
#   成人技法                      用户没写到就浪费 → 权重中
#   风格书骨架                    锦上添花 → 权重低

_PLAN = (
    # ① 去 AI 味：最直接见效的一块。禁用词 + 模板句式。
    ("禁用词与 AI 腔",
     "04-liveness/refs-打磨/banned-words.md",
     "## 一、", "## 使用说明", 900, 1),

    # ② 人味技法：甲乙两部是「怎么写得像人」，丙部是「怎么写得有画面」。
    ("人味技法",
     "04-liveness/refs-打磨/techniques.md",
     "## 甲部", "## 节奏法则", 1000, 1),

    # ③ 节奏法则：句长、停顿、断句——决定读起来像不像小说。
    ("节奏法则",
     "04-liveness/refs-打磨/techniques.md",
     "## 节奏法则", "", 500, 2),

    # ④ 句式纪律：冷峻克制流那套（短句、句号重锤、情绪→身体信号）。
    ("句式纪律",
     "05-style/style-book.md",
     "## 二、风格 DNA", "## 三、可套用骨架", 700, 2),

    # ⑤ 成人技法：常驻。用户自己在小说里写什么就演什么，
    #    不再按「亲密程度」挑——小说没有那个东西可判。
    ("成人场景的写法",
     "02-h-tech/h-writing.md",
     "## 【用词纪律】", "## 深度范文库", 700, 3),

    # ⑥ 内容质量：感官、场景、情感六模块。
    ("感官与场景",
     "03-content/成人情色写作指导.md",
     "", "", 600, 4),
)


def _build():
    """按配方切齐，预算不够就丢权重低的。返回 `(正文, 用了哪几块)`。"""
    parts = []
    for title, rel, start, end, limit, weight in _PLAN:
        body = _slice(rel, start, end, limit)
        if not body:
            continue
        parts.append((weight, title, body))

    # 权重高的先上，装进预算为止
    parts.sort(key=lambda x: x[0])
    out, used, total = [], [], 0
    for weight, title, body in parts:
        block = "### %s\n%s" % (title, body)
        if total + len(block) > MAX_CHARS and out:
            # 装不下就**截断到剩余空间**，而不是整块丢掉。
            # 2026-11-12 实测：整块 skip 的话，前面几块就把预算吃满，
            # 后面权重低的块（成人技法）一条都进不来——3000 的预算
            # 实际只用了 1357。截断至少能带上一半。
            room = MAX_CHARS - total - len(block) + len(body)
            if room < 200:          # 剩这么点不如不要，半句技法没意义
                continue
            block = "### %s\n%s\n…" % (
                title, body[:room - 40].rsplit("\n", 1)[0])
        out.append(block)
        used.append(title)
        total += len(block)
    return "\n\n".join(out), used


_CACHE = {"mtime": 0.0, "text": "", "used": []}


def _sig():
    """所有素材文件的最新 mtime。改了规范就重切，不用重启。"""
    latest = 0.0
    for _t, rel, _s, _e, _l, _w in _PLAN:
        try:
            latest = max(latest, os.path.getmtime(_path(rel)))
        except OSError:
            continue
    return latest


def guide():
    """小说正文每轮要拼的写作规范。拿不到就返回空串（正文照写）。"""
    try:
        sig = _sig()
        if _CACHE["text"] and _CACHE["mtime"] == sig:
            return _CACHE["text"]
        text, used = _build()
        _CACHE["text"] = text
        _CACHE["used"] = used
        _CACHE["mtime"] = sig
        log.info("小说写作规范已切：%d 字，含 %s", len(text), "、".join(used))
        return text
    except Exception:                                     # noqa: BLE001
        log.warning("小说写作规范注入失败，这一轮只靠契约", exc_info=True)
        return ""


def block():
    """拼成可直接拼进提示词的一段（带抬头）。没内容就返回空串。"""
    body = guide()
    if not body:
        return ""
    return ("## 怎么写（这一轮的笔法，不是剧情）\n\n"
            "下面这些是写正文时用的手感：句式、节奏、感官、哪些词不要用。"
            "它们不改变剧情走向——剧情由玩家定，你只负责把它写成小说。\n\n"
            + body + "\n")

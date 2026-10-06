"""search_tags —— anima-tags 标签库的结构化检索（一次调用拿回候选）。

和 `grep_file` 的分工：`grep_file` 是「按行正则搜文件」的通用工具，查一次要
整表扫一遍（实测 **77 ms**，且返回的是原始行）；本工具是**为标签库定制的
索引查询**，把 32.8 万行预先建成内存索引，查一次 **3~5 µs**（快约 2 万倍），
而且返回的是**结构化的候选列表**，不是行文本。

为什么需要它（2026-10-06 实测）：中文名到 tag 的映射**歧义严重**——
83,756 个中文主名里 91% 只有 1 个候选，但长尾很吓人：

    初音未来 → 111 个 cat=4 候选    爱丽丝 → 84    莉莉 → 57
    胡桃     → 18                   艾米莉亚 → 10   银狼 → 3

而且**朴素取第一个必错**：`初音未来[0]` 是 `expo_miku_(2019_taiwan)`（2019
台湾展），`胡桃[0]` 是 `kurumi_(ikach)`，`银狼[0]` 是 `ginro_(dr._stone)`。
所以本工具的定位是**召回**（把候选捞全），**挑选交给 LLM**——这正是搜索
agent 存在的理由。代码单干一定选错。

候选 >8 的键只有 **424 个（0.51%）**，所以绝大多数查询直接返回全量候选；
只有这 0.51% 需要截断，截断时用**「无括号基础 tag 优先」**（54.9% 的键存在
这种基础 tag）。⚠️ 这是**命名结构**判断（`hatsune_miku_(swimwear)` 是
`hatsune_miku` 的变体），**不是热度排序**——`skills/anima-tags/SKILL.md`
铁律第 8 条钉死了「所有词条同级：不按热度排序、不筛选、不推荐、不展示热度
数字」，上游 danbooru 的 post count 列是用户**故意砍掉**的。基础 tag 优先
只保证「别把基础名截掉」，不引入任何热度信号。

⚠️ 铁律第 3、4 条要求「歧义必问、库外必问」。本工具**只召回、不替用户决定**；
自主挑选发生在搜索 agent 那一层，与铁律的冲突由上层策略解决（要么降级成回问，
要么明确放开），不在本工具里做取舍。

索引懒加载（首次调用才建，约 **0.5 s / +97 MB**），按 mtime 自动失效。
三个索引：`by_tag`（全类别 tag 名，精确/前缀）、`win`（角色类中文名，滑窗）、
`zh_gen`（通用+作品类中文名，**只做整串精确查、不滑窗**）。
"""

import os
import re
import threading

from app.tools.sandbox import resolve_in_skills, to_rel

DEFAULT_INDEX = "anima-tags/data/tags.tsv"

# 中文滑窗最长匹配的窗口上限（字符）。库内最长中文角色名约 12 字。
MAX_WINDOW = 16
# 滑窗只收「角色」类，避免「一个女孩在海边」撞到 girl_(anime_expo) 这种噪音。
# 类别码：0=general 1=artist 3=copyright 4=character 5=meta
WINDOW_CATS = ("4",)
# 通用/作品类的中文名另建一个**只做精确查**的索引（不滑窗）。
# 为什么不能并进 WINDOW_CATS：那会让「一个女孩在海边」撞出 girl_(anime_expo)。
# 为什么不收 artist（1）和 meta（5）：15.2 万画师名会把索引撑大一倍还全是噪音。
GEN_CATS = ("0", "3")

DEFAULT_MAX_PER_HIT = 8
HARD_MAX_PER_HIT = 40
DEFAULT_MAX_HITS = 5
HARD_MAX_HITS = 20

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# 中文列形如「银狼（崩坏：星穹铁道）」→ 主名 = 剥掉尾部的（作品名）
_SERIES = re.compile(r"（[^）]*）\s*$")

_INDEX_LOCK = threading.Lock()
# {绝对路径: (mtime, size, by_tag, win)}
_INDEX_CACHE = {}


def _build(path):
    """扫一遍 tsv 建三个索引。

    by_tag: tag 名 -> (中文, 类别码)          —— 精确/前缀查（全类别，32.8 万条）
    win:    中文主名 -> [(tag, 中文, 类别)]   —— 滑窗查（仅 WINDOW_CATS=角色）
    zh_gen: 中文主名 -> [tag]                 —— 整串精确查（仅 GEN_CATS=通用/作品）

    中文主名要剥掉「（作品名）」再进 win，同时**整串也进**——用户可能连作品名
    一起说（「银狼（崩坏：星穹铁道）」）。这是前面踩过的坑：不剥的话
    「\\t银狼\\t」精确匹配永远零命中。

    zh_gen 只存 tag 名（cat 和中文从 by_tag 反查），并且**不参与滑窗**。
    实测总占用 +97 MB（其中 by_tag 是大头，win 约 34 MB 那份的基础，
    zh_gen 约 +63 MB），建索引 0.5 s。
    """
    by_tag = {}
    win = {}
    zh_gen = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").rstrip("\r").split("\t")
            if len(parts) < 3:
                continue
            tag, zh, cat = parts[0], parts[1], parts[2]
            by_tag[tag] = (zh, cat)
            full = zh.strip()
            main = _SERIES.sub("", full).strip()
            if cat in WINDOW_CATS:
                for key in {main, full}:
                    if key:
                        win.setdefault(key, []).append((tag, full, cat))
            elif cat in GEN_CATS and main:
                zh_gen.setdefault(main, []).append(tag)
    return by_tag, win, zh_gen


def _index(path):
    """按 mtime+size 缓存索引；文件没动就直接复用。"""
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        stamp = None
    with _INDEX_LOCK:
        hit = _INDEX_CACHE.get(path)
        if hit and hit[0] == stamp:
            return hit[1], hit[2], hit[3]
    by_tag, win, zh_gen = _build(path)
    with _INDEX_LOCK:
        _INDEX_CACHE[path] = (stamp, by_tag, win, zh_gen)
    return by_tag, win, zh_gen


def _base_first(rows):
    """无括号的基础 tag 排前面，其余按 tag 名。**不是热度排序**（见模块说明）。"""
    return sorted(rows, key=lambda r: ("(" in r[0], r[0]))


def _scan_zh(query, win):
    """滑窗取**最长匹配**，命中后跳过已覆盖长度。

    朴素的两层循环会碎成一堆子串（「帮我画一个艾米莉亚」→ 米莉/艾米/艾米莉…），
    所以从窗口上限往下试，命中就整段吃掉。
    """
    hits, L, i = [], len(query), 0
    while i < L:
        matched = None
        for j in range(min(i + MAX_WINDOW, L), i + 1, -1):
            key = query[i:j]
            if key in win:
                matched = key
                break
        if matched:
            hits.append((matched, win[matched]))
            i += len(matched)
        else:
            i += 1
    return hits


def _fmt_hit(key, rows, max_per_hit, out):
    rows = _base_first(rows)
    shown = rows[:max_per_hit]
    out.append("- %s → %d 个候选" % (key, len(rows)))
    for tag, zh, cat in shown:
        out.append("    %s\t%s\tcat=%s" % (tag, zh, cat))
    if len(rows) > len(shown):
        out.append("    …另有 %d 个变体未列出（候选太多，用更具体的中文名或"
                   "直接搜 tag 名收敛）" % (len(rows) - len(shown)))


def _fmt_general(key, tags, by_tag, max_per_hit, out):
    """通用/作品类命中：只认整串精确相等，所以不排序（也无从截断出偏好）。"""
    out.append("- %s → %d 个候选" % (key, len(tags)))
    for tag in tags[:max_per_hit]:
        zh, cat = by_tag.get(tag, ("", "?"))
        out.append("    %s\t%s\tcat=%s" % (tag, zh, cat))
    if len(tags) > max_per_hit:
        out.append("    …另有 %d 个未列出" % (len(tags) - max_per_hit))


def search_tags(query, max_per_hit=DEFAULT_MAX_PER_HIT, max_hits=DEFAULT_MAX_HITS):
    """在 anima-tags 标签库里查一个中文名或 tag 名，返回候选列表。

    参数:
      - query: 要查的词或**整句话**。中文名走滑窗（「帮我画个银狼在打游戏」能
               捞出「银狼」）；ASCII 的 tag 名走精确/前缀查。两种都会跑，结果合并。
      - max_per_hit: 每个命中最多列几个候选，默认 8（硬上限 40）。超出时先给
               无括号的基础 tag，再提示还剩多少个变体。
      - max_hits: 最多列几个命中，默认 5（硬上限 20）。整句话里的通用词会撞出
               一堆命中，靠它收敛。
    """
    q = (query or "").strip()
    if not q:
        return "错误: query 不能为空。给一个中文名（银狼）或 tag 名（silver_wolf）。"

    path, err = resolve_in_skills(DEFAULT_INDEX, allow_absolute=True)
    if err:
        return "错误: " + err
    if not os.path.exists(path):
        return "错误: 标签库不存在: " + DEFAULT_INDEX
    try:
        by_tag, win, zh_gen = _index(path)
    except Exception as e:  # 建索引失败不能让调用方炸掉
        return "读取标签库失败: " + str(e)

    try:
        per = int(max_per_hit)
    except (TypeError, ValueError):
        per = DEFAULT_MAX_PER_HIT
    per = max(1, min(HARD_MAX_PER_HIT, per))
    try:
        hits_cap = int(max_hits)
    except (TypeError, ValueError):
        hits_cap = DEFAULT_MAX_HITS
    hits_cap = max(1, min(HARD_MAX_HITS, hits_cap))

    sections = []

    # ① 整串精确命中 tag（用户直接给了 tag 名，或给了「银狼（崩坏：星穹铁道）」）
    exact = by_tag.get(q) or by_tag.get(q.lower())
    if exact:
        sections.append("【精确命中 tag】%s\t%s\tcat=%s" % (q, exact[0], exact[1]))

    # ② 中文滑窗（只在 query 含 CJK 时才跑，纯 ASCII 的 tag 名不必）
    if _CJK.search(q):
        hits = _scan_zh(q, win)
        if hits:
            out = ["【中文名命中】"]
            for key, rows in hits[:hits_cap]:
                _fmt_hit(key, rows, per, out)
            if len(hits) > hits_cap:
                out.append("- …另有 %d 个中文名命中未列出" % (len(hits) - hits_cap))
            sections.append("\n".join(out))

    # ③ tag 名前缀查（用户写了一半，如 silver_wolf / hatsune）
    if not _CJK.search(q):
        prefix = sorted(t for t in by_tag if t.startswith(q))[:per]
        if prefix:
            out = ["【tag 前缀命中】"]
            for t in prefix:
                zh, cat = by_tag[t]
                out.append("    %s\t%s\tcat=%s" % (t, zh, cat))
            total = sum(1 for t in by_tag if t.startswith(q))
            if total > len(prefix):
                out.append("    …另有 %d 个同前缀未列出" % (total - len(prefix)))
            sections.append("\n".join(out))

    # ④ 通用标签 / 作品名的中文精确查。
    #    只认**整串精确相等**——「双马尾」→ twintails、「原神」→ genshin_impact。
    #    不做滑窗，否则「一个女孩在海边」又要把 girl_(anime_expo) 捞回来。
    #    ⚠️ 这一条**不能只在「前面全落空」时跑**：真实库里「双马尾」本身也是一个
    #    角色名（twintelle_(arms)），滑窗会先命中，把真正的通用标签吞掉。
    gen = zh_gen.get(q) if _CJK.search(q) else None
    if gen:
        out = ["【通用/作品标签精确命中】"]
        _fmt_general(q, gen, by_tag, per, out)
        sections.append("\n".join(out))

    if not sections:
        return ("标签库里没有命中: " + q
                + "\n（换一个说法再查；角色名试中文，标签试英文。"
                  "库里确实没有的，不要编造——按外貌特征写。）")

    head = "标签库检索: " + q + "\n文件: " + to_rel(path)
    return head + "\n\n" + "\n\n".join(sections)


tool = {
    "name": "search_tags",
    "description": "在 anima-tags 标签库（32.8 万条 Danbooru 标签，含中文对照）里"
                   "检索候选。中文名或整句话走滑窗最长匹配，英文 tag 名走精确/前缀查，"
                   "一次调用把候选捞全。**只负责召回，不替你挑**——同一个中文名可能对应"
                   "几十上百个候选（初音未来有 111 个），必须结合用户意图自己判断选哪个；"
                   "拿不准就按外貌特征写，别编造库里没有的角色名。",
    "function": search_tags,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "要查的中文名（银狼、初音未来）或英文 tag 名（silver_wolf），也可以直接给用户整句话"},
            "max_per_hit": {"type": "integer",
                            "description": "每个命中最多列几个候选，默认 8（硬上限 40）"},
            "max_hits": {"type": "integer",
                         "description": "最多列几个命中，默认 5（硬上限 20）"}
        },
        "required": ["query"]
    }
}

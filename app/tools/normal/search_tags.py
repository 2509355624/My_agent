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

索引懒加载（首次调用才建，约 **0.5 s / +110 MB**），按 mtime 自动失效。
五个索引（一个 dict）：

    by_tag   全类别 tag 名 → (中文, 类别)   精确 / 前缀查，32.8 万条
    win      角色类中文名 → [候选]          滑窗（长匹配）
    zh_gen   通用+作品类中文名 → [tag]      **只做整串精确查**，不滑窗
    gen_win  通用类中文名 → [候选]          滑窗（2026-10-06 新增，见下）
    by_cat   类别码 → [tag]                 随机抽样用

## 2026-10-06 新增：`extract()` 与随机抽样

用户的目标是「AI 不要犯错」，而且明确说过「我唯一的目的其实就是 AI 不要犯错…
你现在给我的什么成本为 0 的方案它都没有意义」。所以这里的取向是**拿全数据**，
不是省 token。但省 token 的副产品是好的：**代码定点抽取本身 0 token**。

`extract(text)` —— 拿用户原话直接扫索引，把**字面出现**且库里真实存在的词
全部定成 tag。不调 LLM、微秒级、**没有任何编造空间**。实测：

    画一个穿连衣裙在雨中撑伞的女孩，微笑看着镜头，教室背景
      → 连衣裙 dress ／ 撑伞 holding_umbrella ／ 微笑 smile ／ 教室 classroom
    一个女仆在咖啡厅端托盘，白色围裙，下午茶
      → 女仆 maid ／ 咖啡 coffee ／ 托盘 tray ／ 白色围裙 white_apron（最长匹配生效）

为什么原来做不到：`zh_gen` 只认**整串精确相等**，所以「穿连衣裙的女孩」里那个
「连衣裙」它看不见；而通用类恰恰是中文覆盖最好的一档（52,515 条里 **98% 有中文**，
去重 49,658，一对多只有 1,795）。「服饰 / 动作 / 表情 / 场景」这些用户张口就来的
词，库里几乎都有。

⚠️ 滑窗只给 `extract()` 用，**不并进 `search_tags()`**——那会让
「一个双马尾的女孩」捞出 twintails，破坏「通用索引只做精确查」的既有契约
（`tests/test_search_tags.py::test_general_lookup_is_exact_only` 钉着这条）。

随机抽样（用户 2026-10-06 要求「我要一个随机的画师出来」）：
`search_tags(random=True, cat=..., pattern=...)` 或 `extract()` 自己识别
「随机」口令。⚠️ **通用类（cat=0）必须带 pattern**——实测均匀抽 cat=0 会抽出
`porsche_997`（保时捷）、`bicycle_rack`（自行车架）、`fn_model_1910`（手枪），
和 `app/random_tags.py` 里那句「裸抽会抓出猎奇词」是同一回事。均匀抽 cat=1
画师反而干净（实测 30 条：shiruhino / sorandia / izobe 矶部 / gyaza / vivicat …）。

**均匀抽 = 最符合铁律第 8 条的抽样方式**：不排序、不筛选、不推荐，纯随机，
没有任何热度信号。所以随机这一路和 SKILL.md 不冲突。
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

# 通用类中文名的**滑窗**索引（只给 extract() 用，见模块说明）。
# 和 GEN_CATS 的区别就是「整串精确」vs「滑窗」——两个都留着，各管各的。
GEN_WINDOW_CATS = ("0",)
# 滑窗命中的中文键最短长度。1 个字（库里 147 条）必然乱撞，直接不收。
MIN_GEN_KEY = 2

# 随机抽样开放的类别。0=通用 1=画师 4=角色。
# ⚠️ cat=0 必须带 pattern（实测裸抽会出手枪和自行车架）。
RANDOM_CATS = ("0", "1", "4")
# 一次最多抽几条。抽多了就是拿噪音灌提示词。
HARD_MAX_RANDOM = 20
DEFAULT_RANDOM_COUNT = 3

# 同族标签（`dress` → `white_dress` / `floral_print_dress`）一次给几条。
# 这是把资料包撑到用户要求的 500~1000 字的主力：光靠用户原话里那几个词
# 只有 200~300 字，补上同族才够「参考这个写法」的量。
SIBLING_LIMIT = 4
# 尾词太短就不找同族（`_a` / `_x` 这种会撞出一堆无关的）。
MIN_SIBLING_HEAD = 3

DEFAULT_MAX_PER_HIT = 8
HARD_MAX_PER_HIT = 40
DEFAULT_MAX_HITS = 5
HARD_MAX_HITS = 20

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# 中文列形如「银狼（崩坏：星穹铁道）」→ 主名 = 剥掉尾部的（作品名）
_SERIES = re.compile(r"（[^）]*）\s*$")
# tag 名形如 `hatsune_miku_(cosplay)` → 基础名 = 剥掉尾部括号
_TRAILING_PAREN = re.compile(r"\([^)]*\)\s*$")

_INDEX_LOCK = threading.Lock()
# {绝对路径: (mtime, size, 索引 dict)}。索引 dict 的键见 _build 的 docstring。
_INDEX_CACHE = {}


def _build(path):
    """扫一遍 tsv 建五个索引，返回一个 dict。

    by_tag:  tag 名 -> (中文, 类别码)          —— 精确/前缀查（全类别，32.8 万条）
    win:     中文主名 -> [(tag, 中文, 类别)]   —— 滑窗查（仅 WINDOW_CATS=角色）
    zh_gen:  中文主名 -> [tag]                 —— 整串精确查（仅 GEN_CATS=通用/作品）
    gen_win: 中文主名 -> [(tag, 中文, 类别)]   —— 滑窗查（仅 GEN_WINDOW_CATS=通用）
    by_cat:  类别码 -> [tag]                   —— 随机抽样（O(1) 取一条）

    中文主名要剥掉「（作品名）」再进 win，同时**整串也进**——用户可能连作品名
    一起说（「银狼（崩坏：星穹铁道）」）。这是前面踩过的坑：不剥的话
    「\\t银狼\\t」精确匹配永远零命中。

    zh_gen 只存 tag 名（cat 和中文从 by_tag 反查），并且**不参与滑窗**。
    实测总占用 +110 MB（by_tag 是大头，gen_win 约 +12 MB，by_cat 只存引用
    约 +3 MB），建索引 0.5 s。

    ⚠️ 返回 dict 而不是 tuple：索引从 3 个长到 5 个，解包元组已经很容易
    数错位置了（`hit[1], hit[2], hit[3]` 那种）。键名自带文档。
    """
    by_tag = {}
    win = {}
    zh_gen = {}
    gen_win = {}
    by_cat = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").rstrip("\r").split("\t")
            if len(parts) < 3:
                continue
            tag, zh, cat = parts[0], parts[1], parts[2]
            by_tag[tag] = (zh, cat)
            by_cat.setdefault(cat, []).append(tag)
            full = zh.strip()
            main = _SERIES.sub("", full).strip()
            if cat in WINDOW_CATS:
                for key in {main, full}:
                    if key:
                        win.setdefault(key, []).append((tag, full, cat))
            elif cat in GEN_CATS and main:
                zh_gen.setdefault(main, []).append(tag)
            if cat in GEN_WINDOW_CATS:
                for key in {main, full}:
                    if len(key) >= MIN_GEN_KEY:
                        gen_win.setdefault(key, []).append((tag, full, cat))
    return {"by_tag": by_tag, "win": win, "zh_gen": zh_gen,
            "gen_win": gen_win, "by_cat": by_cat}


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
            return hit[1]
    idx = _build(path)
    with _INDEX_LOCK:
        _INDEX_CACHE[path] = (stamp, idx)
    return idx


def _base_first(rows):
    """无括号的基础 tag 排前面，其余按 tag 名。**不是热度排序**（见模块说明）。"""
    return sorted(rows, key=lambda r: ("(" in r[0], r[0]))


def _scan_zh(query, index):
    """滑窗取**最长匹配**，命中后跳过已覆盖长度。

    朴素的两层循环会碎成一堆子串（「帮我画一个艾米莉亚」→ 米莉/艾米/艾米莉…），
    所以从窗口上限往下试，命中就整段吃掉。

    `index` 是中文主名 → 候选的映射，传 `win`（角色）或 `gen_win`（通用）都行。
    """
    hits, L, i = [], len(query), 0
    while i < L:
        matched = None
        for j in range(min(i + MAX_WINDOW, L), i + 1, -1):
            key = query[i:j]
            if key in index:
                matched = key
                break
        if matched:
            hits.append((matched, index[matched]))
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


def _head(tag):
    """取 tag 的「尾词」（主体词）。danbooru tag 是 `修饰_修饰_主体` 结构。

    ⚠️ 必须**先剥掉尾部的括号**再取尾词。实测踩过：`hatsune_miku_(cosplay)`
    不剥的话尾词是 `(cosplay)`，同族就变成了 `2b_(nier:automata)_(cosplay)`、
    `2k-tan_(cosplay)` 这一堆——全是「带 (cosplay) 的任意角色」，毫无意义。

    ⚠️ 先用 `endswith(")")` 短路：这个函数要在一整趟 32.8 万条上跑，而带括号的
    只是少数。不短路的话每一条都要走一次正则，实测占了 `extract()` 大半耗时。
    """
    if tag.endswith(")"):
        tag = _TRAILING_PAREN.sub("", tag).strip("_")
    return tag.rsplit("_", 1)[-1]


def _siblings_many(tags, by_tag, limit=SIBLING_LIMIT):
    """给一批 tag 各找几个「同族」标签：`dress` → `white_dress` / `floral_print_dress`。

    为什么需要：用户原话里就那么几个词，光靠它们只有 200~300 字，撑不到
    用户要求的 500~1000 字。同族标签是**真实存在的库内条目**，给出去等于
    把「这个主体在库里还能怎么修饰」摊开给生图 API 看——这正是用户说的
    「让它去可以参考这个写法」。

    为什么按尾词而不是子串：`dress` 的同族是 `white_dress`、`frilled_dress`，
    不该是 `dresser`。尾词相等就排除了这类误伤。

    为什么不用额外索引：只在**命中项**上算，整个请求扫一遍 by_tag（32.8 万条
    约 8 ms），比再养一张 60 MB 的倒排表划算得多。搜索是生图链路的前置，
    8 ms 相对后面几十秒的 LLM 调用可以忽略。

    ⚠️ 同族**只收同类**。不收的话 `maid`（cat=0 通用）的同族会变成
    `maid_(disgaea)`（cat=4 角色）——那是「魔界战记里的女仆」，不是
    「女仆的另一种写法」，混进来就是误导（实测踩过）。

    返回 {原 tag: [同族…]}。找不到同族的 tag 不出现在结果里。
    """
    heads = {}
    for t in tags:
        h = _head(t)
        if len(h) >= MIN_SIBLING_HEAD:
            heads.setdefault(h, []).append(t)
    if not heads:
        return {}
    head_cat = {h: by_tag.get(owners[0], ("", ""))[1]
                for h, owners in heads.items()}
    found = {}
    for t in by_tag:
        h = _head(t)
        if h in heads and by_tag[t][1] == head_cat[h]:
            found.setdefault(h, []).append(t)
    out = {}
    for h, owners in heads.items():
        pool = found.get(h) or []
        sibs = sorted(x for x in pool if x not in owners)
        if sibs:
            out[owners[0]] = sibs[:limit]
    return out


def _char_bases(rows):
    """角色候选的基础 tag 名：`twintelle_(arms)` → `twintelle`。

    太短的基础名不收（`a_(x)` 这种会误伤一大片）。
    """
    out = set()
    for tag, _zh, _cat in rows:
        base = tag.split("(", 1)[0].rstrip("_")
        if len(base) >= 4:
            out.add(base)
    return out


def _is_char_variant(tag, bases):
    """`hatsune_miku_(cosplay)` / `hatsune_miku_wonderland_figure` 是不是角色变体。

    真实踩过：cat=0 里混着大量「角色派生」条目（初音未来名下就有
    `hatsune_miku_(cosplay)`、`hatsune_miku_(voltage!!)`、
    `hatsune_miku_wonderland_figure`…）。它们不是通用标签，会把通用段
    整个污染掉，还会让「初音未来」被误判成「已查清」，导致真正的
    111 个 cat=4 候选永远查不出来。
    """
    for b in bases:
        if tag == b or tag.startswith(b + "_") or tag.startswith(b + "("):
            return True
    return False


def _is_derived_name(tag, by_tag):
    """`noir_(armor)` / `sing_(pokemon)` 这种「借了个普通词当名字」的条目。

    判据：带括号，且括号前的基础名**本身不是通用标签**。

        `smile_(e.g.o)`  基础名 `smile` 是通用标签 → 保留（它是 smile 的一个变体）
        `cafe_(blue_archive)` 基础名 `cafe` 是通用标签 → 保留
        `noir_(armor)`   基础名 `noir` 不是通用标签 → 丢掉（那是「叫 Noir 的装甲角色」）
        `sing_(pokemon)` 基础名 `sing` 不是通用标签 → 丢掉

    实测踩过：`黑色 → noir_(armor)`、`唱歌 → sing_(pokemon)`——用户说「穿黑色
    外套」却拿到一个叫 Noir 的角色，正是「AI 犯错」的典型来源。

    ⚠️ 只用在**通用段**。角色段的括号是消歧信息（`银狼（崩坏：星穹铁道）`），
    那边靠 `_base_first` 处理，不能套这条。
    """
    if "(" not in tag:
        return False
    base = tag.split("(", 1)[0].rstrip("_")
    return by_tag.get(base, ("", ""))[1] != "0"


def random_tags(cat="1", count=DEFAULT_RANDOM_COUNT, pattern=None, rng=None):
    """从库里**均匀**随机抽 N 条真 tag，返回可直接拼进资料的文本。

    cat: "1"=画师、"4"=角色、"0"=通用。⚠️ cat=0 **必须带 pattern**
    （见模块说明：裸抽通用类会出保时捷和手枪）。
    pattern: 只对 cat=0 有意义，按**tag 名**做正则过滤，如 "dress|skirt"。
    rng: 传 random.Random 可复现（测试用）；默认用模块级 random。

    均匀抽是最符合 SKILL.md 铁律第 8 条的抽样方式——不排序、不筛选、不推荐，
    纯随机，不引入任何热度信号。
    """
    import random as _random
    rng = rng or _random
    cat = str(cat or "1").strip()
    if cat not in RANDOM_CATS:
        return "错误: 只能随机 %s，收到 %s。" % ("/".join(RANDOM_CATS), cat)
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = DEFAULT_RANDOM_COUNT
    n = max(1, min(HARD_MAX_RANDOM, n))

    path, err = resolve_in_skills(DEFAULT_INDEX, allow_absolute=True)
    if err:
        return "错误: " + err
    if not os.path.exists(path):
        return "错误: 标签库不存在: " + DEFAULT_INDEX
    try:
        idx = _index(path)
    except Exception as e:
        return "读取标签库失败: " + str(e)
    by_tag, by_cat = idx["by_tag"], idx["by_cat"]

    pool = by_cat.get(cat) or []
    pat = None
    if pattern:
        try:
            pat = re.compile(str(pattern))
        except re.error as e:
            return "错误: pattern 不是合法正则（%s）。" % e
    if cat == "0" and pat is None:
        return ("错误: 随机通用标签必须带 pattern（如 \"dress|skirt|uniform\"）——"
                "不加过滤会抽出保时捷、自行车架、手枪这类和画面无关的词。")
    if pat is not None:
        pool = [t for t in pool if pat.search(t)]
    if not pool:
        return "错误: 没有可随机的条目（cat=%s pattern=%s）。" % (cat, pattern)

    picked = rng.sample(pool, min(n, len(pool)))
    label = {"1": "画师", "4": "角色", "0": "通用标签"}.get(cat, cat)
    out = ["【随机 %s】（库里均匀抽 %d 条，都是真条目）" % (label, len(picked))]
    for t in picked:
        zh = by_tag.get(t, ("", ""))[0]
        out.append("    %s\t%s" % (t, zh))
    if cat == "1":
        out.append("    （画师 tag 在提示词里要加 @ 前缀）")
    return "\n".join(out)


# 「随机」口令 → 抽哪一类。顺序有意义：先匹配更具体的词。
# ⚠️ 通用类的 pattern 只收**服饰/动作/表情/场景**四类干净子集——
# 实测规模：服饰 2,318 / 动作 1,018 / 表情 457 / 场景 244。
_RANDOM_RULES = (
    (("画师", "画风", "artist"), "1", None),
    (("角色", "人物", "女主", "男主"), "4", None),
    (("服饰", "服装", "衣服", "穿搭", "上衣", "裙子", "内衣", "泳装", "制服"),
     "0", r"(_dress|_skirt|uniform|shirt|_bra|kimono|swimsuit|bikini|_socks|"
           r"thighhighs|_vest|_jacket|_coat|apron|_sweater|_hoodie)"),
    (("动作", "姿势", "pose"), "0",
     r"(^holding_|_holding|^sitting|^standing|^lying|^walking|^running|"
     r"^kneeling|^squatting)"),
    (("表情", "神情"), "0",
     r"(smile|_eyes|expression|blush|crying|_face)"),
    (("场景", "背景", "风景"), "0",
     r"(outdoors|indoor|background|_room|_street|_field|_forest|_sky)"),
)
# ⚠️ 刻意**没有** `roll`：它是英文常见词，用户写 `rolled_sleeves` 这种 tag 名
# 会被误判成「要随机」。只认中文口令。
_RANDOM_WORD = re.compile(r"随机|随便来|随便抽|来一个随机的")

# 这些中文词在库里**确实挂着角色条目**，但它们是泛指，不该当成用户点名的角色。
# 实测：「画一个穿连衣裙的女孩」里的「女孩」会命中 `the_girl_(resident_evil)`
# （生化危机）——把一个无关角色塞进资料包，正好是「AI 犯错」的典型来源。
#
# ⚠️ 名单刻意收得**很窄**：只放颜色词和亲属称谓这种绝无可能当人名的词。
# 更多的情况（女仆 / 镜头 / 双马尾）交给 `_is_generic_key` 用**库里的数据**判，
# 不靠硬编码——硬编码名单永远列不全，库里一加条目就失效。
_GENERIC_ZH = frozenset((
    "女孩", "男孩", "少女", "少年", "男人", "女人", "人物", "角色",
    "白色", "黑色", "红色", "蓝色", "绿色", "黄色", "粉色", "紫色",
    "橙色", "灰色", "金色", "银色", "棕色", "褐色", "青色",
))


def _is_generic_key(key, zh_gen, bases):
    """这个中文词是**通用词**（而不是用户点名的角色）吗？

    判据只有一条：通用类里存在一个**无括号**的 tag，且它**正好等于该词的角色候选
    基础名**。成立就是通用词：

        女仆 → zh_gen 有 `maid`，角色候选 `maid_(.flow)` 的基础名也是 `maid` ✓
        镜头 → zh_gen 有 `lens`，角色候选 `lens_(arknights)` 的基础名也是 `lens` ✓
        初音未来 → 角色基础名是 `hatsune_miku`，而通用类里只有
                   `hatsune_miku_(cosplay)`（有括号）和
                   `hatsune_miku_wonderland_figure`（≠ 基础名）→ 不是通用词 ✓

    ⚠️ 这条规则的设计取向是**宁可漏挡、绝不误杀**。第一版只判「有没有无括号的
    通用 tag」，结果 `初音未来` 被 `hatsune_miku_wonderland_figure` 误判成通用词，
    直接从资料里消失——**搜索层整个失效**。误杀的代价远大于漏挡（漏挡只是多一行
    噪音，误杀是丢掉真答案）。所以判据收紧到「必须正好等于角色基础名」。

    已知漏挡（能接受）：`双马尾` 的角色候选是 `twintelle_(arms)`、通用 tag 是
    `twintails`，两者不等 → 判不出来，`twintelle_(arms)` 会留在角色段当噪音。
    这和 `search_tags()` 里「双马尾同时命中角色和通用」的既有行为一致。
    """
    for tag in zh_gen.get(key) or ():
        if "(" not in tag and tag in bases:
            return True
    return False


def detect_random(text):
    """认出「随机画师 tag」这类口令，返回 (cat, pattern) 或 None。

    只在用户**明说随机**时才认——没提随机就不该乱抽，那会覆盖用户的具体要求。
    """
    t = text or ""
    if not _RANDOM_WORD.search(t):
        return None
    for words, cat, pat in _RANDOM_RULES:
        if any(w in t for w in words):
            return cat, pat
    return None


def extract(text, max_keys=20, budget=600, rng=None):
    """从用户原话里**定点**抽出库里真实存在的标签。不调 LLM、0 token。

    这是「AI 不要犯错」的主路径：用户说过的词，库里有没有、对应哪个 tag，
    全部由索引精确决定，**没有任何编造空间**。搜索 agent 那一层只负责补它
    没覆盖到的部分（同义词桥接、库外名词、联网）。

    返回 dict：
      doc       格式化好的资料段（可直接拼进生图模板），查不到就是 ""
      hint      给搜索 agent 的提示：哪些词已经查过（别重复查）、哪些有歧义
      confirmed 已唯一确定的词
      ambiguous 命中多个候选、需要判断的词

    ⚠️ 这里**不判歧义、不替用户挑**（铁律第 3、8 条）。歧义词把候选全列出来，
    交给下游结合上下文挑。

    ⚠️ 段落顺序 = 价值顺序：**角色/专有名词排第一**。第一版把通用段排前面，
    结果 520 字的预算全被通用段吃掉，初音未来那 111 个候选一个都没露出来
    （实测 doc 被从尾巴截断，正好截掉整个角色段）。

    ⚠️ `confirmed` / `ambiguous` 只统计**真正写进 doc 的词**。第一版先统计再
    卡预算，结果提示词说「舞台、唱歌已经查过了」，而 doc 里根本没有这两行
    ——搜索 agent 会因此跳过它们，等于凭空丢资料。
    """
    empty = {"doc": "", "hint": "", "confirmed": [], "ambiguous": []}
    q = (text or "").strip()
    if not q:
        return empty

    path, err = resolve_in_skills(DEFAULT_INDEX, allow_absolute=True)
    if err or not os.path.exists(path):
        return empty
    try:
        idx = _index(path)
    except Exception:
        return empty
    by_tag, zh_gen = idx["by_tag"], idx["zh_gen"]
    if not _CJK.search(q):
        return empty

    chr_hits = _scan_zh(q, idx["win"])
    gen_hits = _scan_zh(q, idx["gen_win"])
    # 命中角色窗口的词，在通用段里要拿它的候选基础名去查重（见 _is_char_variant）
    chr_bases = {key: _char_bases(rows) for key, rows in chr_hits}

    # 每条是 (词, 文本行, 是否歧义)，预算裁完再回头统计 confirmed/ambiguous
    generic_keys = set()
    chr_entries = []
    for key, rows in chr_hits[:max_keys]:
        bases = chr_bases.get(key) or set()
        if key in _GENERIC_ZH or _is_generic_key(key, zh_gen, bases):
            # 通用词：不进角色段，且**它的通用条目也不能被当成角色变体滤掉**。
            # 漏了这句的话 `女仆 → maid` 会被 `maid == 角色基础名` 判成变体，
            # 两头都落空，doc 直接变空（实测踩过）。
            generic_keys.add(key)
            continue
        rows = _base_first(rows)
        parts = []
        for tag, zh, _cat in rows[:5]:
            # 中文列形如「银狼（崩坏：星穹铁道）」，剥掉主名剩下的括号就是消歧信息
            tail = zh[len(key):] if zh.startswith(key) else zh
            parts.append("%s%s" % (tag, tail))
        if len(rows) == 1:
            chr_entries.append((key, "- %s → %s" % (key, parts[0]), False))
        else:
            more = "…共 %d 个" % len(rows) if len(rows) > len(parts) else ""
            chr_entries.append((key, "- %s → %s %s（有多个候选，按用户意图挑）"
                                % (key, " ｜ ".join(parts), more), True))

    # 通用标签：先滤掉角色变体和「借名条目」，再一次性找同族。
    kept = []
    for key, rows in gen_hits[:max_keys]:
        bases = set() if key in generic_keys else (chr_bases.get(key) or set())
        tags = [r[0] for r in _base_first(rows)
                if not _is_char_variant(r[0], bases)
                and not _is_derived_name(r[0], by_tag)]
        if tags:
            kept.append((key, tags))
    sibs = _siblings_many([t for _k, tags in kept for t in tags[:2]],
                          by_tag) if kept else {}
    gen_entries = []
    for key, tags in kept:
        line = "- %s → %s" % (key, " / ".join(tags[:3]))
        fam = [t for t in (sibs.get(tags[0]) or ())
               if not _is_derived_name(t, by_tag)]
        if fam:
            line += " ｜同族：%s" % ", ".join(fam)
        gen_entries.append((key, line, False))

    rand = detect_random(q)
    rand_block = random_tags(cat=rand[0], pattern=rand[1], rng=rng) if rand else ""

    # 按价值顺序拼，**逐行**卡预算——绝不从中间切断某一行。
    ordered = (("【原话里提到的角色/专有名词】（库里真实存在的候选）", chr_entries),
               ("【原话里能直接认出的通用标签】（库里真实存在，直接用）", gen_entries))
    blocks, kept_keys, used = [], [], 0
    for title, entries in ordered:
        if not entries:
            continue
        room = budget - used - len(title) - 1
        if room <= 0:
            continue
        picked, n = [], 0
        for key, ln, _amb in entries:
            if n + len(ln) + 1 > room:
                break
            picked.append((key, ln))
            n += len(ln) + 1
        if picked:
            blocks.append(title + "\n" + "\n".join(ln for _k, ln in picked))
            used += n + len(title) + 1
            kept_keys.extend(picked)
    if rand_block and used + len(rand_block) + 2 <= budget + 200:
        # 随机段是用户明说要的，预算上给它一点特权（超出 200 字以内也留）
        blocks.append(rand_block)

    kept_set = {k for k, _ln in kept_keys}
    confirmed, ambiguous = [], []
    for key, _ln, amb in chr_entries:
        if key not in kept_set:
            continue
        (ambiguous if amb else confirmed).append(key)
    for key, _ln, _amb in gen_entries:
        if key in kept_set and key not in confirmed and key not in ambiguous:
            confirmed.append(key)

    doc = "\n\n".join(blocks)
    if confirmed or ambiguous:
        hint = ("【代码已经查过的词，别重复查】"
                + ("、".join(confirmed) if confirmed else "（无）")
                + ("\n【有多个候选、需要你确认或补全的】" + "、".join(ambiguous)
                   if ambiguous else ""))
    else:
        hint = ""
    return {"doc": doc, "hint": hint, "confirmed": confirmed,
            "ambiguous": ambiguous}


def search_tags(query="", max_per_hit=DEFAULT_MAX_PER_HIT,
                max_hits=DEFAULT_MAX_HITS, random=False, cat=None,
                count=DEFAULT_RANDOM_COUNT, pattern=None):
    """在 anima-tags 标签库里查一个中文名或 tag 名，返回候选列表。

    两种用法（二选一）：

    **① 查词**（默认）——给 query，返回候选列表。
      - query: 要查的词或**整句话**。中文名走滑窗（「帮我画个银狼在打游戏」能
               捞出「银狼」）；ASCII 的 tag 名走精确/前缀查。两种都会跑，结果合并。
      - max_per_hit: 每个命中最多列几个候选，默认 8（硬上限 40）。超出时先给
               无括号的基础 tag，再提示还剩多少个变体。
      - max_hits: 最多列几个命中，默认 5（硬上限 20）。整句话里的通用词会撞出
               一堆命中，靠它收敛。

    **② 随机**（用户说「随机画师 tag」这类口令时）——给 random=True。
      - cat: "1"=画师（默认）、"4"=角色、"0"=通用。
      - count: 抽几条，默认 3（硬上限 20）。
      - pattern: 只对 cat="0" 有意义且**必填**，按 tag 名正则过滤，
        如 "dress|skirt|uniform"。
    """
    if random:
        return random_tags(cat=cat or "1", count=count, pattern=pattern)

    q = (query or "").strip()
    if not q:
        return ("错误: query 不能为空。给一个中文名（银狼）或 tag 名（silver_wolf）；"
                "要随机抽就传 random=true。")

    path, err = resolve_in_skills(DEFAULT_INDEX, allow_absolute=True)
    if err:
        return "错误: " + err
    if not os.path.exists(path):
        return "错误: 标签库不存在: " + DEFAULT_INDEX
    try:
        idx = _index(path)
        by_tag, win, zh_gen = idx["by_tag"], idx["win"], idx["zh_gen"]
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
                   "拿不准就按外貌特征写，别编造库里没有的角色名。"
                   "用户说「随机」时改用 random=true 从库里抽真条目。",
    "function": search_tags,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "要查的中文名（银狼、初音未来）或英文 tag 名（silver_wolf），也可以直接给用户整句话。random 模式下不用填"},
            "max_per_hit": {"type": "integer",
                            "description": "每个命中最多列几个候选，默认 8（硬上限 40）"},
            "max_hits": {"type": "integer",
                         "description": "最多列几个命中，默认 5（硬上限 20）"},
            "random": {"type": "boolean",
                       "description": "true = 从库里均匀随机抽真条目（用户说「随机画师 tag」这类口令时用），此时 query 不用填"},
            "cat": {"type": "string",
                    "description": "随机抽的类别：1=画师（默认）、4=角色、0=通用标签"},
            "count": {"type": "integer",
                      "description": "随机抽几条，默认 3（硬上限 20）"},
            "pattern": {"type": "string",
                        "description": "只对 cat=0 有效且必填，按 tag 名正则过滤，如 \"dress|skirt|uniform\""}
        },
        "required": []
    }
}

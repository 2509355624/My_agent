"""comic_agent —— 编剧 Agent：查库写剧本，每个标签都必须真实存在。

## 为什么要有这一层（2026-10-11）

`comic_writer.write_script` 是**单次 API 调用**，模型只能凭记忆写标签。实测
它稳定犯两类错：

1. **写成自然语言**——`girl stepping off the bus with one foot on the ground`、
   `male hand grabbing her wrist from behind`。渲染器的分词器把这些句子切碎，
   位置关系全丢 → 错位。
2. **编造标签**——`male hand grabbing her wrist from behind` 切出来是
   `male` + `hand` + `grabbing`：`male` 直接触发「一个男性角色」、`hand` 触发
   「手」、`grabbing` 触发「抓握」，却**没有任何一个词在约束「只出手、不出
   身体」**，模型于是补出一整个男人、甚至多只手。库里其实有正解
   （`disembodied hand` / `holding another's wrist`），但不查库就永远不知道。

## 2026-10-11 追加：换了标签还不够，**人数标签是另一半**

用户拿三张实测图来问「手为什么总是画错」。查标签库 + danbooru 官方 wiki
复核后，根因不止「标签选错」：

`solo` 的含义是「画面里只有一个角色」，而 danbooru 官方角色计数规则
（`tag_group:character_count`）明确写着「Disembodied hands and other body
parts are **counted**」——**一只手也算一个角色**。于是 `1girl, solo` 配
「抓手腕」是**自相矛盾的指令**，模型只能三选一妥协，三种都被实测到了：

    01 格  把手画成她自己的（落进 holding own wrist / own hands clasped 的语义）
    02 格  干脆放弃抓握动作，保住 solo
    03 格  硬画第二只手，但没有词约束「只出手、不出身体」→ 一团畸形的手

正解要三条一起改，缺一条都不成立：

    ① 人数：出现第二方身体部位 → `1girl, solo focus, 1other`（**绝不再写 solo**）
    ② 抓握：`holding another's wrist`（不是模糊的 `hand around wrist`）
    ③ 来源：`pov` + `pov hands` —— 声明那只手属于镜头外的视角方

用户按这套改后实测通过（`Anima_00083_.png`：视角方的手从画面左下伸入、
解剖正确、攥住她的手臂）。所以下面提示词的规则 5 / 6 是**必须**的，别删。

第一类靠提示词能治，第二类**只能靠查库**——32.8 万条标签库和检索工具
（`search_tags`）都是现成的，缺的只是「让编剧真的去查」这一步。

## 三道防线

    ① 提示词：规范全文 + 「只能写标签」的硬规则 + 「先查完再写」的工作方式
    ② Agent 循环：模型自己调 search_tags / read_file / web_search 查证
    ③ 代码判据：产出后把每格 `画面:` 拆成标签**逐个查库**，查不到的当不合格
       打回重写（最多 tries 次，最后一次容忍并记警告）

③ 是必须的——项目一贯的做法是「模型做不到的规则，由代码判」。①②是软约束，
模型仍可能蒙一个库里没有的 tag 出来，只有③能兜住。

## 2026-10-11 追加：查库会失控，**轮次预算必须由代码管**

上线当天用户实测就撞上了：日志里第 12 轮的 6 行**全是** `search_tags`，上限一到
返回空正文，下游报「只解析到 0 格」，界面上是一个空白剧本。两稿 24 轮，一次正文
都没交。

根因是**没人管预算**。提示词要求「每个要用的标签都先查一次」，而 3 格 ≈ 60 个
标签，模型一轮只发 6 个左右的查询——光查库就要十轮，它却在专心查库，不知道自
己快没机会动笔了。所以补两条：

    ① 轮次分两段：查库段（工具可用）+ **强制动笔段**（工具关闭，只要正文，
       见 WRITE_RESERVE）。到点由代码锁工具，不靠模型自觉。
    ② 每轮回灌工具结果时**报剩余轮数**，让它自己会算「还剩几轮、该批量查了」。

配套两个 bug 一起修了：

- **兜底是死代码**。`last_text` 原来只在「无工具调用」分支里赋值，而那个分支必
  然 `return`——于是「达到上限返回最后一稿」永远返回空串，模块文档承诺的兜底从
  来没生效过。现在改成单独的 `fallback`，中间轮的半截稿也留一份。
- **空剧本不能静默落地**。`write_script` 原来对空正文也走「最后一次容忍」，
  返回空串后 `comic_api` 照样建任务目录、写一个空 `script.md`，前端一片空白。
  现在空正文单独识别、单独回炉，最后仍空则**抛异常**（`comic_api` 转 502 报错）
  ——「标签没验过但稿子在」能容忍，「什么都没有」容忍不了。

## 两条容易踩的实现约束

- **备选记录必须抽走**。规范要求「歧义必问」，但自动流程没人可问（用户
  2026-10-11 拍板：自选 + 记备选）。agent 在格末写 `备选:` 行，代码用
  `_extract_notes` 抽出来统一挪到剧本**头部**。不抽不行——`comic.parse_script_text`
  对未知行是按「续行」处理的（`cur[field] += line`），留在格里会被拼进
  `画面:` 或 `标签:`，直接污染提示词。
- **`spec.md` 里没有任何花括号**（已核对），但仍用 `str.replace` 而不是
  `str.format` 拼模板：规范是用户随时会改的外部文件，将来写进一个 `{}`
  就会让 format 抛异常，replace 不会。

## 2026-10-11 追加：把编剧从 comic_writer 换过来时，**规范掉了**

用户拿一话「抓包」来问「感觉 AI 没有遵守那份 skill 的规范」。查下来不是模型
不听话，是**规范压根没喂**：

- 《连续剧情提示词生成规范》（`app/comic_spec.md`）只有 `comic_writer` 在读，
  而网页端的编剧早就换成本模块了 —— 于是那份规范只剩旧 CLI
  （`python -m app.comic write`）在用，Agent 这边一个字都没看到。
  实跑核对过：`_system_prompt(3)` 里 `'连续剧情' in text` 是 **False**。
- 现在和 anima-tags 规范一起全文注入（见 `_story_spec_text`）。它的「输出格式」
  一节仍**不执行** —— 本流程的输出是剧本契约（`## NN` / `画面:` / `气泡:`），
  只取它的内容规则。

## 2026-10-11 追加：**剧情不成立**是比标签更难治的一类错

同一话里用户指出的真问题：01 `气泡: 别跟着我……`（她已经知道有人跟着）、
02 `气泡: 谁——！`（又变成刚发现）—— 前后自相矛盾；而且三格都是同一个瞬间的
三个切面，剧情零推进。根因是提示词只要求「每格有新信息」，**没要求整话是一条
故事线**。现在：

- 新增【整话必须是一条故事线】段落，把上面那个真实反例原样写进去；
- 旁白从「一条可选」改成**多条必写**（`标签1:` `标签2:` `标签3:`）——
  这一栏负责讲剧情（前情 + 这一格推进了什么），台词只负责演情绪；
- 每条气泡必须带说话人前缀 `（女）` / `（我）`，它决定气泡尾巴指向谁，
  写不清 qwen 会把镜头外「我」说的话挂到女生头上（见 comic._tail_note）。

## 2026-10-11 追加：**泛类必须具体**与**镜头人称不许跳**

用户拿第二组实测图又指出两条：

1. **`backpack` / `sailor collar` 这类只有类别名、没有颜色的写法还在出现。**
   规则 7 / 12 早就写了「不许只写 `backpack`」，模型却交出
   `backpack, backpack on one shoulder`——它以为「带了姿态就算具体了」。
   于是背包本身仍然没有颜色，三格画面三个色（用户原话：「所有泛类必须明确
   的加上具体的描述，是什么颜色的呀，什么形状的呀」）。
   现在加两条硬判据：`bare_wearables`（衣物 / 随身物件没写颜色）+
   `duplicate_wearables`（同一件东西写两遍，模型会画两个包）。
2. **03 格凭空补出一个男生。** 01/02 格明明只用 `pov hands` 把「我」写成
   一只手，03 格却写了 `1girl, 1boy` + `black hoodie, grey pants`。用户原话：
   「我图三看到个男的很难受……我这个角色的镜头视角抛出镜头，结果第三个场景
   就直接出问题」。
   根因是**规则 6 里我自己写的一句**：「那只手明显是男性手臂时可写 `1boy`
   代替 `1other`」——正是它教模型把视角方实体化。现已删除，改由
   `pov_conflicts` 判：**整话只要有 `pov` / `pov hands`，任何格都不许写
   `1boy`**，并在规则 14 里把「人称定下就不许变」写死。

## 模型

`provider` / `model` 由调用方传入（漫画页的下拉），不传就走全局默认——
与 `comic_writer` 的 `llm.call_llm` 同一个口径。
"""

import logging
import os
import re

from app import comic, comic_writer, llm
from app.agent import parse_tool_calls, _strip_tool_blocks
from app.tools.sandbox import resolve_in_skills
from app.tools.normal.search_tags import search_tags, _index, DEFAULT_INDEX
from app.tools.normal.web_search import tool as _web_search_tool
from app.tools.normal.read_file import tool as _read_file_tool

log = logging.getLogger("comic.agent")

# agent 循环轮数上限。编剧要查的词比「只做搜索」的 search_agent 多得多
# （发型/瞳色/服装/动作/手部/场景/构图，一格就七八个，十格要几十个），
# 但也不能无界——12 轮 × 单轮几十秒正好落在「等得住」的范围里。
MAX_ROUNDS = 12

# 最后几轮留给「强制动笔」：这几轮**不执行工具**，只要正文。
#
# 留 2 轮而不是 1 轮，是因为第一轮锁工具时模型可能仍在输出 `[[TOOL:...]]`
# （实测它在查库节奏里刹不住），那时只能再催一轮。查到 12 轮上限却一个字没
# 写出来，比多烧一轮贵得多（用户拿到的是空白剧本）。
WRITE_RESERVE = 2

# 转入强制动笔段时插的这一条。措辞要「命令式 + 说明工具已关闭」：
# 只说「请写剧本」模型会继续查库，必须明确告诉它那些行**不会被受理**。
_FORCE_WRITE = (
    "【查库时间到】从这一轮起**工具已经关闭**——你再输出 `[[TOOL:...]]` 我也只"
    "会当废文本丢掉，不会执行、不会给你结果。现在**立刻**输出完整剧本：每一格"
    "都要有 `画面:` 和 `气泡:`，只输出剧本本身，不要解释、不要围栏。\n"
    "标签只用你已经查到的那些；没查过的词就用语义最近的已知标签，"
    "**绝对不许新造库里可能没有的词**。"
)
# 单次 LLM 调用超时。比 search_agent 的 30s 长：编剧最后一轮要一口气吐出
# 整份剧本（十格），输出量大。
CALL_TIMEOUT = 90
# 单轮工具结果上限。工具结果要一直留在 messages 里，不封顶会把上下文撑爆。
#
# ⚠️ 2026-10-11 之前是 2000，**这是我自己埋的浪费源**。实测（22:05 那次运行，
# usage/ + logs/ 可查）：模型每轮被提示词催着「一轮写 15~25 行都行」，实际索要
# 2,232~4,346 字符，而上限 2000 把其中 **10%~53% 砍掉**；截断只保留前 2000 字符，
# 所以批次里排在**后面**的查询**必然白查**。模型看不到结果 → 下一轮重查：223 次
# 查库里有 76 次是跨轮重复（`pov hands` / `backlighting` 各 5 遍，轮内重复 0 次），
# 于是 8 次运行**每一次都查满 10 轮**才被动笔。
# 6000 装得下一轮 25 个查询的典型返回（均 150~250 字符），两头就不用互相打架了。
MAX_TOOL_CHARS = 6000

# 规范文件（相对 skills/）。用户改它不用重启——每次现读。
_SPEC_REL = "anima-tags/references/spec.md"

_TOOLS = {
    "search_tags": search_tags,
    "read_file": _read_file_tool["function"],
    "web_search": _web_search_tool["function"],
}

# ─── 代码判据：标签存在性 ───────────────────────────────
#
# 官方前缀标签**不在 booru 词库里**（anima-tags 规范第 2 节），查库必落空，
# 所以先豁免掉。它们本身写法固定，写错的风险很低。
_PREFIX_TAGS = frozenset((
    "masterpiece", "best quality", "good quality", "normal quality",
    "worst quality", "low quality",
    "safe", "sensitive", "nsfw", "explicit",
    "newest", "recent", "mid", "early", "old",
    "ye-pop", "deviantart",
))
_YEAR_RE = re.compile(r"^year \d{4}$")
_SCORE_RE = re.compile(r"^score_\d+$")
# 权重语法：整段长成 `(tag:1.2)`。只认「整段」——`reverse:1999` 这种带冒号
# 但不带括号的系列名不会被误伤（碰巧它也不该被当成权重）。
_WEIGHT_RE = re.compile(r"^\((?P<tag>.+?):(?P<w>\d+(?:\.\d+)?)\)$")
_PANEL_LINE_RE = re.compile(r"^\s*##\s+\d{1,3}\s*$")


def _norm_tag(part):
    """把剧本里的一段标签文本归一成**带空格的规范形态**。

    处理：去权重括号 → 拆转义括号 → 去 artist 的 @ 前缀 → 小写 → 空格归一。

    ⚠️ **刻意不转下划线**。官方前缀标签是带空格的（`best quality`、
    `year 2025`），若在这里就转成 `best_quality`，下面的豁免名单（按空格写）
    会全部失配，每个前缀标签都被当成「库里没有」→ 剧本永远不合格。
    2026-10-11 由 test_prefix_tags_exempt 逮到，别改回去。
    """
    t = (part or "").strip().lower()
    m = _WEIGHT_RE.match(t)
    if m:
        t = m.group("tag").strip()
    t = t.replace("\\(", "(").replace("\\)", ")")
    t = t.lstrip("@")                       # artist tag 在库里不带 @
    return re.sub(r"\s+", " ", t).strip()


def _db_key(tag):
    """规范形态 → 库里存的下划线形态（`hand around wrist` → `hand_around_wrist`）。"""
    return tag.replace(" ", "_")


def _by_tag():
    """标签库索引（库内 tag 名 → 中文/类别）。取不到就返回 None。

    复用 search_tags 的 `_index`（带 mtime 失效的内存缓存）而不是自己再读
    一遍 12MB 的 tsv——同一份索引只该建一次。
    """
    path, err = resolve_in_skills(DEFAULT_INDEX, allow_absolute=True)
    if err or not path:
        return None
    try:
        return _index(path)["by_tag"]
    except Exception:                        # 索引坏了不能带崩生图链路
        log.warning("标签库索引构建失败，本轮跳过标签校验", exc_info=True)
        return None


def missing_tags(text):
    """把每格 `画面:` 拆成标签逐个查库，返回 [(格号, 库里没有的标签), ...]。

    索引拿不到时返回空表——校验是增强，不是主链路。
    """
    idx = _by_tag()
    if idx is None:
        return []
    _, panels = comic.parse_script_text(text)
    out = []
    for p in panels:
        for part in (p["scene"] or "").split(","):
            tag = _norm_tag(part)
            if not tag:
                continue
            # 豁免判定用**带空格**的形态（前缀标签就是带空格的写法）
            if tag in _PREFIX_TAGS or _YEAR_RE.match(tag) or _SCORE_RE.match(tag):
                continue
            key = _db_key(tag)
            if key not in idx:
                out.append((p["no"], key))
    return out


# ─── 代码判据：库里存在但语义错的禁用标签 ────────────────
#
# 这是一类**特别危险**的标签：它在库里真实存在，所以 missing_tags 的存在性
# 校验会放行；但它的语义不是我们要的，写进去必然出问题。
#
# 典型是 `hand around wrist`——没写明「是谁的手」，实测被渲染成「她自己抓自己
# 手腕」（库里有 holding own wrist 正好对上这个语义）。2026-10-11 用户连续
# 三张图 + 一稿剧本都在踩它，提示词里明令换成 `holding another's wrist` 也没用
# ——模型对 `hand around wrist` 的熟悉度远高于那个长写法。
#
# 结论和 solo 那条一样：**库里存在的错标签，存在性校验一个都拦不住，只能专门盯。**
_BANNED_TAGS = frozenset(("hand_around_wrist",))


def banned_tags(text):
    """找出用了禁用标签（库里存在但语义错）的格，返回 [(格号, 库里形态), ...]。"""
    _, panels = comic.parse_script_text(text)
    out = []
    for p in panels:
        for part in (p["scene"] or "").split(","):
            key = _db_key(_norm_tag(part))
            if key in _BANNED_TAGS:
                out.append((p["no"], key))
    return out


# ─── 代码判据：人数标签与「第二方身体部位」互斥 ──────────
#
# `solo` 的含义是「画面里只有一个角色」，而 danbooru 官方角色计数规则
# （tag_group:character_count）明确「一只手也算一个角色」。所以画面里一旦要求
# 出现第二方的身体部位，`1girl, solo` 就成了自相矛盾的指令——实测模型会三选一
# 妥协：把手画成她自己的（落进 holding own wrist 的语义）、放弃抓握动作、或画
# 出一团畸形的手（2026-10-11 用户三张实测图各中一种）。
#
# 必须用代码判：这些标签**库里全都存在**，missing_tags 的存在性校验拦不住，
# 只能查语义冲突。而 `solo` 在模型的训练记忆里太常见，光靠提示词叮嘱没用。
# 除身体部位外，`1boy` / `1other` 本身也在**声明「画面上还有第二个人」**——
# 一格写了 `solo`（画面只有一个人）又写 `1boy`，是同一个自相矛盾。
# 2026-10-11 追加：用户拿 03 格 `1girl, 1boy` 来问「为什么凭空多出一个男的」。
_SECOND_PARTY_EXACT = frozenset(("pov_hands", "1boy", "1other"))


def _has_second_party(tag):
    """这一段标签是否声明了「第二方的身体部位入画」。

    三种写法都算：视角方的手（`pov hands`）、没有身体的手（`disembodied *`）、
    以及 `another` 家族（`holding another's wrist` / `grabbing another's arm` …）。
    按词根 `another` 判而不是 `another's`——实测模型会漏撇号写成
    `holding another wrist`，那样精确匹配就漏检了。
    反过来 `holding own wrist`（抓自己手腕）不含 another，不算——那种格写
    `solo` 是对的，不能误伤。
    """
    key = _db_key(tag)
    if key in _SECOND_PARTY_EXACT:
        return True
    if key.startswith("disembodied_"):
        return True
    return "another" in key


def solo_conflicts(text):
    """找出「写了 `solo`、却又要求第二方身体部位入画」的格。

    返回 [(格号, 库里形态的标签), ...]。纯文本判断，不依赖标签库索引。
    """
    _, panels = comic.parse_script_text(text)
    out = []
    for p in panels:
        parts = [t for t in (_norm_tag(x) for x in (p["scene"] or "").split(",")) if t]
        # 只认精确的 `solo`——`solo focus` 是允许的写法，不能误伤
        if "solo" not in parts:
            continue
        for t in parts:
            if _has_second_party(t):
                out.append((p["no"], _db_key(t)))
                break
    return out


# ─── 代码判据：衣物 / 随身物件必须写明具体描述 ──────────
#
# 用户 2026-10-11 定调：**所有泛类都必须加上具体的描述**——「是什么颜色的呀，
# 什么形状的呀」。规则 7 / 12 早就写了「不许只写 `backpack`」，实测仍然失守：
# 模型交出 `backpack, backpack on one shoulder`，它以为「带了姿态就算是具体
# 了」，而背包本身仍然没有颜色，三格画面三个色。
#
# 判据只认**角色穿戴 / 随身**的类别词。`door` / `handrail` / `seat` 这类场景
# 物件不需要指定颜色（要求了也没意义），把它们拉进来只会误伤。
#
# 「颜色」必须真的写出来：`backpack on one shoulder` 里的姿态、`sailor collar`
# 里的款式、`pleated skirt` 里的款式，**都不算**。这正是用户反复抱怨的那条
# （裙子一次深藏青、一次浅蓝）。
_COLOR_WORDS = frozenset((
    "white", "black", "grey", "gray", "silver", "gold", "golden", "blonde",
    "brown", "red", "crimson", "scarlet", "pink", "blue", "navy", "indigo",
    "green", "purple", "violet", "orange", "yellow", "beige", "cream",
    "ivory", "khaki", "teal", "cyan", "turquoise", "magenta", "maroon",
    "olive", "lavender", "mint", "wine", "amber", "dark", "light", "pale",
    "bright", "deep", "colorful", "multicolored", "two-tone", "transparent",
))

_WEARABLE_WORDS = frozenset((
    # 随身
    "backpack", "bag", "satchel", "handbag", "purse", "briefcase",
    "umbrella", "watch", "necklace", "earrings", "choker", "brooch",
    "glasses", "sunglasses", "earphones", "headphones", "headset",
    "scarf", "neckerchief", "tie", "bowtie", "belt", "ribbon", "hairband",
    # 上装
    "shirt", "blouse", "sweater", "cardigan", "vest", "jacket", "coat",
    "hoodie", "uniform", "clothes", "outfit", "dress", "sailor",
    # 下装
    "skirt", "pants", "trousers", "jeans", "shorts", "leggings",
    # 鞋袜 / 内衣 / 其他
    "socks", "stockings", "shoes", "boots", "sneakers", "sandals",
    "gloves", "mittens", "bra", "panties", "underwear", "pajamas",
    "swimsuit", "apron", "collar",
))


def _scene_tags(p):
    """一格 `画面:` 拆成规范形态的标签列表（去空、去重、保序）。"""
    out, seen = [], set()
    for part in (p["scene"] or "").split(","):
        tag = _norm_tag(part)
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _fmt_by_panel(pairs, limit=4):
    """[(格号, 名字), ...] → 「格 01 的 `a`、`b`；格 02 的 `c`」，**按格聚合**。

    一条一格是读不顺的：三格各错三处就是九条「格 0X 的 …」，模型只看到
    重复噪音。按格聚合后一眼能看出「这一格错了哪几个」。
    """
    by = {}
    for no, name in pairs:
        by.setdefault(no, []).append(name)
    return "；".join(
        "格 %s 的 %s" % (no, "、".join("`%s`" % n for n in names))
        for no, names in list(by.items())[:limit])


def bare_wearables(text):
    """找出「衣物 / 随身物件没写颜色」的格，返回 [(格号, 库里形态的 tag), ...]。

    `pleated skirt`（有款式没颜色）、`sailor collar`、`backpack on one shoulder`
    都算不合格，要写成 `white pleated skirt` / `white sailor collar` /
    `white backpack on one shoulder`。
    """
    _, panels = comic.parse_script_text(text)
    out = []
    for p in panels:
        for tag in _scene_tags(p):
            toks = set(tag.split())
            if not (toks & _WEARABLE_WORDS):
                continue
            if toks & _COLOR_WORDS:
                continue
            out.append((p["no"], _db_key(tag)))
    return out


def duplicate_wearables(text):
    """同一格里同一件东西被写了两次，返回 [(格号, 类别词), ...]。

    典型是 `backpack` + `backpack on one shoulder`：模型想表达「单肩背」，
    却把类别名又抄了一遍。两个标签会让模型画**两个包**，而且第一个还没颜色。
    """
    _, panels = comic.parse_script_text(text)
    out = []
    for p in panels:
        seen = {}
        for tag in _scene_tags(p):
            for cat in sorted(set(tag.split()) & _WEARABLE_WORDS):
                if cat in seen:
                    out.append((p["no"], cat))
                else:
                    seen[cat] = tag
    return list(dict.fromkeys(out))


def pov_conflicts(text):
    """POV 剧本里出现完整男性角色（`1boy`）的格，返回 [(格号, "1boy"), ...]。

    2026-10-11 用户实测：01/02 格用 `pov hands` 把「我」写成一只手，03 格却写了
    `1girl, 1boy` + `black hoodie, grey pants`，画面里凭空多出一个男生。用户
    原话：「我图三看到个男的很难受……我这个角色的镜头视角抛出镜头，结果第三个
    场景就直接出问题」。

    判据是**跨格**的：只要整话里有任何一格声明了 `pov` / `pov hands`，这一话
    就是第一人称视角，「我」只能以手或手臂入画 → 任何格都不许写 `1boy`。
    反过来，整话没有 pov 的双人剧本（第三人称）写 `1boy` 是合法的，不拦。
    """
    _, panels = comic.parse_script_text(text)
    by_panel = [(p["no"], _scene_tags(p)) for p in panels]
    if not any({"pov", "pov_hands"} & set(ts) for _, ts in by_panel):
        return []
    return [(no, "1boy") for no, ts in by_panel if "1boy" in ts]


# ─── 备选记录的抽取与归位 ───────────────────────────────

_NOTE_RE = re.compile(r"^\s*(?:备选|备选记录|候选)\s*[:：]\s*(.+?)\s*$")


def _extract_notes(text):
    """抽走所有 `备选:` 行，返回 (干净的剧本文本, 备选行列表)。

    必须在 parse / 校验 / 落盘之前调用（见模块说明：留下会被当续行吃掉）。
    """
    kept, notes = [], []
    for line in (text or "").splitlines():
        m = _NOTE_RE.match(line)
        if m:
            notes.append(m.group(1).strip())
        else:
            kept.append(line)
    return "\n".join(kept).strip(), notes


def _place_notes(text, notes):
    """把备选行插到「第一个 `## NN` 之前」。

    只能放这个位置：`parse_script_text` 在头部（还没遇到第一格时）只认标题和
    任务号，其余行**直接忽略**——格内任何位置都会被当续行拼进上一个字段。
    """
    if not notes:
        return text
    # 同一格会把同样的备选写两遍（实测常见），保序去重
    notes = list(dict.fromkeys(notes))
    lines = (text or "").splitlines()
    # 已经自带 [xxx] 前缀的（如 [警告]）原样用，其余按备选记录编号——
    # 不判断就会叠成 `[备选] [警告] …`（实测踩过）。
    block = [(n if n.startswith("[") else "[备选] " + n) for n in notes] + [""]
    at = next((i for i, ln in enumerate(lines) if _PANEL_LINE_RE.match(ln)), None)
    if at is None:                            # 没有格子（理论上校验会先拦下）
        return "\n".join(lines + [""] + block).strip()
    head = lines[:at]
    while head and not head[-1].strip():
        head.pop()
    return "\n".join(head + [""] + block + lines[at:]).strip()


_PREAMBLE_HEAD_RE = re.compile(r"^\s*#")


def _strip_preamble(text):
    """砍掉剧本开头的碎碎念，从第一行 `#`/`##` 起算。

    模型交卷前爱先来一句自言自语，实测原话：
    「剧本已足够：用 `grabbing another's arm`（抓他人手臂…），配 `from side`…」
    这句留在 script.md 里很脏，而它在第一个 `## 01` 之前本来就会被解析器忽略
    ——所以剥掉它不影响契约，只是让交出去的文件干净。
    """
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if _PREAMBLE_HEAD_RE.match(ln):
            return "\n".join(lines[i:]).strip()
    return (text or "").strip()


# ─── 提示词 ─────────────────────────────────────────────

def _spec_text():
    """Anima 提示词规范全文（每次现读，改文件不用重启）。"""
    path, err = resolve_in_skills(_SPEC_REL, allow_absolute=True)
    if err or not path or not os.path.isfile(path):
        log.warning("规范文件读不到（%s）：%s", _SPEC_REL, err or "文件不存在")
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _story_spec_text():
    """《连续剧情提示词生成规范》全文（`app/comic_spec.md`，每次现读）。

    复用 `comic_writer._spec_text()`：同一份文件只该有一处读取逻辑，用户在
    comic_writer 那边改了路径/编码，这里跟着走。读不到不能让编剧挂掉——规范
    是增强，硬规则段还在。
    """
    try:
        return comic_writer._spec_text()
    except OSError:
        log.warning("连续剧情规范读不到（%s）", comic_writer._SPEC_PATH)
        return ""


_SYSTEM_TMPL = """你是条漫分镜编剧，同时是 Anima 提示词工程师。

你写的每一格 `画面:` 会**直接**喂给 Anima 生图工作流——它是**唯一的生图提示词**
（`标签:` / `气泡:` 只是最后画到图上的文字，生图那一步看不到它们）。所以
`画面:` 必须写得**清楚、具体、看得见**，每个词都得是画面模型认得的英文。
Danbooru 标签库（32.8 万条）是**参考**，不是白名单：库里有真实标签就优先用它，
库里查不到但你能确定模型看得懂的英文短语也**照写**。写错一个词，画面就会错位、
多手、甚至凭空多出一个人。

【整话必须是一条故事线——这是最容易不合格的一处】
- 动笔前先在心里定一条线：这一话从哪开始、中间发生什么、最后一格落在哪。
  多格就是「起 → 承 → 转」逐格推进，**不是同一个瞬间的三个切面**。
- **旁白负责讲剧情，台词负责演情绪。** 别让每一格都只写「当下的表情」——
  剧情要靠 `标签1:` `标签2:` 往前推。
- **后一格不许推翻前一格已经建立的事实。** 反例（真实被打回过，别重犯）：
    01 `气泡: （女）别跟着我……`   ← 她**已经知道**有人跟着
    02 `气泡: （女）谁——！`      ← 又变成刚发现，前后矛盾
  写台词前先问自己：她**此刻已经知道什么**？上一格已经告诉她的信息，
  这一格不能装不知道。
- 交稿前把每格的 `标签` + `气泡` 按顺序连起来读一遍，像读一条真的漫画：
  **读得通才交稿**；读不通就重写那几格，不许硬交。

【你能用的三个工具，以及**怎么调用**】
调用必须写成下面这样一行（可以一次写多行，我会一次性全执行完再回给你）：

[[TOOL:search_tags]]{"query": "抓手腕"}

- **search_tags** —— 查 Danbooru 标签库（32.8 万条，带中文对照）。query 给
  中文名（抓手腕 / 眼泪 / 百褶裙）或英文 tag 名（hand_grab / tears），也可以
  直接给一句话。返回候选列表，每行是「tag 中文名 cat=类别」（4=角色 3=作品
  0=通用 1=画师）。随机抽用
  [[TOOL:search_tags]]{"random": true, "cat": "1", "count": 3}
- **read_file** —— 读 skills/ 里的文件：
  [[TOOL:read_file]]{"path": "anima-tags/references/examples.md"}
- **web_search** —— 联网查角色出处，只在标签库查不到时用：
  [[TOOL:web_search]]{"query": "银狼 出自哪部作品"}

⚠️ **查库只能写成上面的 `[[TOOL:...]]{...}` 格式**。写成「search_tags: 抓手腕」
这样的普通文字，系统看不见、也不会执行——你会白等一整轮（2026-10-11 实测
就是这么错的，29 个查询全成了废文本）。

【第一步永远是查库——先把这一话要用的标签查完，再动笔写剧本】
⚠️ **你有轮次预算，超了系统会强制你动笔（那时工具就关了）。**
- 查库阶段大约 10 轮，之后系统会发一条「查库时间到」并**关闭工具**，逼你用
  手上已经查到的标签动笔。所以：
  - **一轮要批量查**：一次回复里可以写 15~25 行工具调用，别一轮只查 5 个。
  - **同一个词不要查第二遍**：查过的结果就在对话里，回头看一眼就行。
  - **把预算花在不确定的词上**：动作、手部、构图、光照、道具这些最容易踩坑；
    只要有一点点不确定就先查。
- 每个要用的标签都先调 search_tags 查一次。查中文（「抓手腕」「哭泣」）可以，
  直接查英文 tag（hand_grab、tears）也可以；不确定英文叫什么就查中文。
- 库里命中 → 用返回的 tag 名（把下划线换成空格写进剧本）。
- 一个中文名出来几十个候选 → 自己挑最贴合画面的那一个，把**其他候选**记进
  `备选:` 行（见下面「歧义怎么办」）。
- 查不到 → 换个说法再查；换两三次仍查不到，就用语义最近的库里标签，
  **绝不要自己造一个库里没有的 tag**。
- 需要时用 read_file 读完整规范（anima-tags/references/spec.md）与示例
  （anima-tags/references/examples.md）；用 web_search 查某个角色的作品出处。

【画面字段的硬规则】（违反任何一条，这一格会被代码打回重写）
1. **只能写逗号分隔的英文标签，绝不许出现句子。**
   错：girl with watery eyes about to cry
   对：tears, crying, looking down
   错：the girl is standing at the bus door
   对：standing, bus interior, automatic door
   （⚠️ 别把"正在发生的动作"拆成姿势词组——`stepping off` / `one foot on ground`
   这类是黑名单，见规则 8 和规则 11。）
2. **顺序**：安全级 → 人数 → 主体特征（发型/瞳色/配饰）→ 服装 → 动作姿势 →
   场景 → 构图镜头。同类标签聚在一起。
3. **不要写质量词和画师 tag**（masterpiece / best quality / ultra-detailed /
   absurdres / 画师名）——工作流模板已经钉好了，重复写只会稀释权重。
4. **安全级必须有且只有一个**，只能从 safe / sensitive / nsfw / explicit 里选。
   注意 **不是 `sfw`** —— `sfw` 不在 Anima 规范里，写了会被打回。
5. **视角方（「我」）的手只在剧情真的需要时才入画——不要每格都加一只手。**
   不需要时，那格就写 `1girl, solo`，别硬塞手部标签（硬塞的后果见规则 6）。
   决定要入画时，抓别人的手腕只能写 `holding another's wrist`，不许写
   `hand around wrist`，更不许把动作写成句子。
   - 攥住/握住别人的手腕 → `holding another's wrist`
     （这是**唯一**可用的写法。网上流传的别名 `grabbing another's wrist`、
     `wrist grab` 在本库里**都不存在**，实测查过，别用。）
   - 手只是搭在别人手腕上（没用力） → `hand on another's wrist`
   - 镜头就是「我」的眼睛、那只手是「我」的 → 再加 `pov` + `pov hands`
   - 画面里只有手、看不到它的身体 → 再加 `disembodied hand` / `disembodied arm`
   错：male hand grabbing her wrist from behind ← `male` 会画出整个人 + 多只手
   错：hand around wrist ← 没写是谁的手，实测被画成「她自己抓自己手腕」
   对：pov, pov hands, holding another's wrist ← 视角方的手攥住她的手腕
   对：disembodied hand, holding another's wrist ← 只让一只手入画
   查「手」「抓」「手腕」「牵着」把这些标签找齐再用，别查一个词就落笔。
6. **人数标签是本流程最容易错的一条，按情形分别写，不许一律写 `solo`：**
   - 画面里只有一个人、第二方**任何**身体部位都不入画 → `1girl, solo`
   - 画面里出现了视角方（哥哥/「我」）的手或手臂 → `1girl, solo focus, 1other`
     （**绝对不能再写 `solo`**）
   - **任何情况下都不许写 `1boy`**（见规则 14）——`1boy` 会让模型补出一个
     **完整的人**：脸、身体、衣服全都有。视角方只出手，不出人。
   为什么：Danbooru 官方角色计数规则里「一只手也算一个角色」（原话
   Disembodied hands and other body parts are counted）。写了 `solo` 又要画面
   出现第二只手，两条指令自相矛盾——实测后果有三种：手变成她自己的、抓握
   动作整个消失、或画出一团畸形的手。
   视角方的**脸、身体、衣服一律不许出现**，只允许手或手臂入画，画面仍以
   `1girl` 开头。给「我」写 `black hoodie` / `grey pants` 这类服饰，等于在说
   「画一个穿这些的人」，模型就会把整个人画出来（2026-10-11 实测：03 格就是
   这么凭空多出一个男生的）。
   ⚠️ 写了 `1other` 就必须**真的**让第二方身体部位入画（手/手臂），并配上对应
   的手部标签；这一格若根本没有第二方入画，就别写 `1other`，退回 `1girl, solo`。
   实测坑：旁白写着「我攥住了她的手腕」，画面却只写 `solo focus, 1other` 而
   不带任何手部标签——模型会把 `1other` 理解成「补一整个人进来」。
7. 服饰颜色款式、灯光氛围每格写全并保持一致（见规范）。**物件走同一条规则**：
   画面上任何看得见颜色的东西都要写颜色——`white in-ear earphones`，不许只写
   `in-ear earphones`；`black backpack`，不许只写 `backpack`。跟服饰一样，每格
   重复写、跨格保持一致，颜色第一次定下来就别再改。
   ⚠️ **「写了款式 / 姿态」不等于「写了颜色」**——`backpack on one shoulder`、
   `sailor collar`、`pleated skirt` 都还是没写颜色的裸写法，必须写成
   `white backpack on one shoulder`、`white sailor collar`、`white pleated skirt`。
   **每一件衣物、每一个随身物件都要带颜色**（衣领、裙子、背包、耳机、袜子……
   一个都不能漏），代码会逐个查，漏了就打回重写。
   ⚠️ **同一件东西只写一个标签**：`backpack` 和 `backpack on one shoulder` 是
   同一件，两个都写模型会画**两个包**——合并成 `white backpack on one shoulder`。
8. **下面这些词一律不许写**（逐条实测过的黑名单——不只是"库里没有"这么简单，
   写下去一定会出问题；反过来，别拿这张表去类推别的词，也别因为某个词"库里
   没有"就不敢写，见规则 12）：
   `sfw`（用 safe）、
   `arm grab`、`male hand`（用 pov hands 或 disembodied hand / disembodied arm）、
   `medium shot`（用 upper body / cowboy shot / close-up）、
   `soft lighting`、`cinematic lighting`（用 backlighting / sidelighting / dim lighting）、
   `stepping off` / `getting off`、`one foot on ground`。
   ⚠️ 「上车 / 下车」这类**瞬间动作**库里没有对应标签，但**不许**硬造一个姿势去顶替。
   2026-10-11 实跑翻过车：这里原来写的是「用 `standing on one leg` 之类」，结果
   旁白「她一只脚刚踏下车门」被逐字翻成了 `standing on one leg`，画出来是单脚
   独立的芭蕾姿，跟下车毫无关系。正确做法见规则 11。

   还有一个**反例**要专门记住：`hand around wrist` 库里**有**，但没写明是谁的手，
   实测会被渲染成「她自己抓自己手腕」。要抓别人的手腕，只有
   `holding another's wrist` 一条路（见规则 5）。
9. **每格必须带来新信息，不许两格只是换表情演同一个动作。**
   错：02 格和 03 格都是「攥着手腕、皱眉」，只是一个写着 surprised、一个写着
   angry —— 观众看到的是同一格，剧情零推进。
   对：每格都在往前推——她先挣，再回头看，然后说出一句让两人关系变化的话。
   `气泡:` 同样要成线：三句话连起来应该是一个小故事（拒绝 → 动摇 → 让步），
   而不是三句同义的情绪宣泄。
   **交稿前自己回看一遍**：把每格的 `画面:` + `气泡:` 摆在一起过一遍，凡是有
   两格在讲同一件事，就重写其中一格（换动作、换空间、换关系状态），再交稿。
10. **`标签1:`（可续到 `标签2:` `标签3:`）每一格都要写**（这是剧情的骨架，不是
   可选项）；**每条气泡都要带说话人**（`（女）` 或 `（我）`）。两者格式见下面
   「输出格式」。
   **这类"差不多的写法"是本次最容易踩的坑**——凡是不确定的，先查库再写。

11. **只有 `画面:` 是生图提示词。** `标签:` 和 `气泡:` **都不进生图提示词**——
   它们是最后画到图上的**文字**（右上角旁白条 / 气泡），生图那一步根本看不到
   它们。所以三者是有关联的，但**不是**「标签直接变成生图标签」这种关系：
   - `画面:` 是**画面**——这一刻镜头里静止可见的样子：姿态、位置、服饰、表情、
     景别、背景。**只有它进生图提示词。**
   - `气泡:` 是**语言**——展示当下这个场景里她或「我」说了什么
   - `标签:` 是**描述**——让观众知道发生了什么、剧情推进到哪

   推论：`标签:` 里写的事件，生图阶段**完全不知道**。你不能指望「旁白写了一只
   脚刚踏下车门，画面就自动画出来」——要让画面里出现什么，就必须写进 `画面:`，
   而且只能写**看得见的样子**。
   反例（2026-10-11 实跑）：旁白「她一只脚刚踏下车门」被逐字翻成画面
   `standing on one leg`——那是单脚站立的芭蕾姿，跟下车毫无关系。正确写法：
   画面写她站在车门处的状态（`standing, automatic_door, bus_interior`），
   「一只脚刚踏下车门」这句话留在 `标签:` 讲。
   同理，画面是**静态的一张图**：旁白「她猛回头」→ 画面写已经转过来的样子
   `looking back`，不是正在转的 `turning head`。**凡是"正在发生的那一瞬"，
   都不要塞进 `画面:`。**

12. **《连续剧情提示词生成规范》高于 anima-tags 标签库，两者冲突时一律以规范
   为准。** 这不是空话——**标签库里绝大多数"衣物 / 物件"标签本身就不带颜色**
   （`pleated skirt`、`earphones`、`backpack` 全都是），照抄它们就等于违反规范
   的「颜色+款式必须同时出现」。所以：
   - 衣物：写 `navy pleated skirt`，**不能**只写 `pleated skirt`
   - 物件：写 `white in-ear earphones`，**不能**只写 `in-ear earphones`
   - `white uniform` 这种"整套"词要顺带把裙子写清：
     `white uniform, navy pleated skirt`
   - **同一件东西，每一格都要写同一个颜色。** 2026-10-11 实测：`pleated skirt`
     没写颜色，三格画面里裙子一次深藏青、一次浅蓝——观众一眼就看出不是同一条
     裙子。颜色在第一格定下来，后面每格照抄。

13. **一格只能有一个明确地点，同一个地点的格子写同一组地点标签。** 不许把
   `bus interior`（车厢内）和 `bus stop` / `outdoors` / `city`（站台外景）写进
   同一格——模型会把两个空间混着画。2026-10-11 实测：三格都同时写了
   `bus interior` 和 `bus stop / outdoors / city`，结果同一个公交场景三格各不相同。
   先想清楚这一格的镜头在哪：在车厢里就只写 `bus interior`（配 `handrail`、`door`），
   在站台就只写 `bus stop`（配 `outdoors`、`city`），然后**这一场景内保持不动**。

14. **整话的镜头人称（视角）在第一格就定下来，之后不许变。** 这一话只要有任何
   一格写了 `pov` / `pov hands`，它就是「我」的**第一人称视角**，那么：
   - **全篇每一格都不许写 `1boy`**（写了模型就补出一个完整男人：脸、身体、
     衣服全都有）；
   - 「我」永远只以**手或手臂**入画，脸和身体一律不出现；
   - 需要第二方在场：`1girl, solo focus, 1other` + `pov hands` +
     `holding another's wrist`；
   - **不许给「我」写服饰**（`black hoodie`、`grey pants` 这类词等于在说
     「画一个穿这些的人」）。
   2026-10-11 实测（用户原话「我图三看到个男的很难受」）：01/02 格用
   `pov hands` 把「我」写成一只手，03 格却写了 `1girl, 1boy` + `black hoodie,
   grey pants`，画面里凭空多出一个和前两格完全接不上的男生。
   **视角跳了，观众立刻出戏**——这一条代码会跨格查，跳了就打回重写。

【Anima 提示词规范（全文，必须遵守）】
写法：**以标签为主，可以用简短的英语短语补充**——短词和英语短语都能写。
查库（search_tags）是**找更准的说法**用的参考，**不是白名单**：库里查不到不等于
不能写，只要你能确定画面模型看得懂就照写（例：`white in-ear earphones`、
`black backpack` 库里都没有，但都是对的写法）。反过来，库里**有**的标签也不
一定对（见规则 8 的反例 `hand around wrist`）。

======== 规范全文开始 ========
__SPEC__
======== 规范全文结束 ========

【《连续剧情提示词生成规范》（全文，内容规则必须遵守）】
这份规范管的是每一格画面**内容**该怎么写：场景、服饰颜色、灯光氛围、物体移动
的位置副词、局部动作与表情互斥、以及跨格的一致性。它的「输出格式」一节
（固定部分 / `---` 分隔 / 全英文整句）**不执行**——本流程的输出格式用后面那份
剧本契约（下游是程序按契约解析）。

它的示例是**自然语言**写法（`girl wearing black lace bra`），本流程**只取它的
规则、不取它的写法**：`画面:` 仍然写逗号分隔的英文标签，服饰写成
`black lace bra, pink frilled shorts` 这样的标签，不要写 `girl wearing …` 整句。

======== 连续剧情规范全文开始 ========
__STORY_SPEC__
======== 连续剧情规范全文结束 ========

【歧义怎么办】
遇到多个候选、或库里查不到：**自己选最合理的一个，不要停下来问**，
然后在这一格最后补一行：

备选: 选了 <你的选择>，另有 <候选2> / <候选3>

没有歧义就不写这行。（这行是留给你自己核对用的记录，代码会把它挪到剧本顶部。）

【输出格式（剧本契约，下游程序按它解析）】
只输出剧本本身，不要解释、不要前言后记、不要代码块围栏。

# 标题：<中文标题，12 字以内>

## 01
画面: <英文标签串>
气泡: （女）<中文台词>
气泡2: （女）<可选，第二句台词；没有就整行删掉>
标签1: <第一人称剧情旁白，必写，十几到 __MAX_LABEL__ 字>
标签2: <可选，接着上一条继续讲；最多写到 标签3>

## 02
...

硬性要求：
1. 每格以 `## NN` 开头，NN 为两位数字、从 01 起连续；**格数必须正好 __PANELS__ 格**。
2. `画面:` 全是英文标签，**绝对不许出现中文**。
3. **每条气泡都必须带说话人**，写成 `气泡: （女）台词`：
   `（女）` = 画面里的女生说的；`（我）` = 镜头外的「我」说的（这一格画面里
   只有他的手）。这个前缀**不会**画到图上，它只决定气泡尾巴指向谁——
   写错了气泡就挂到错的人头上，所以每条都要写。`气泡2:` 同样要带。
   台词口语化、有活人感（可用省略号、语气词、话说一半），禁止书面腔；
   表达此刻的情绪，不复述画面。`气泡:` 不超过 __MAX_BUBBLE__ 字；
   话多就再写一行 `气泡2:`（同样不超过 __MAX_BUBBLE__ 字），不是把一条写长。
4. `标签1:` `标签2:` `标签3:` 是画面右上角的**剧情旁白条**，
   **每一格至少要有一条**（别整行删掉），每条十几到 __MAX_LABEL__ 字，
   最多 __MAX_LABELS__ 条。用第一人称「我」写——镜头就是「我」的眼睛。
   **这一栏负责讲剧情**：第一条可以交代前情（例：`妹妹生闷气离家出走`），
   后面几条写这一格推进了什么（例：`一只脚已经踩上车门`）。
   **不要写成时间地点**（「傍晚」「公交车站」这种没有剧情信息的不要写），
   也不要复述画面里已经看得见的东西。
   它和 `气泡:` 是两条互补的线：气泡是**她**说的，标签是**「我」**这边的推进。
   ⚠️ `标签:` 是**旁白，不是台词**：不要带 `（女）`/`（我）` 前缀，也不要写成
   角色的口语。想加**第二句台词**就写 `气泡2:`，**不要写 `标签2:`**——写成
   `标签2:` 会让这句台词被当成旁白、`气泡2` 整条丢掉（代码会判不合格打回）。
5. 剧情节奏：__PACING__
6. 不要写 `任务号:` 这一行。
"""


def _system_prompt(panels):
    text = _SYSTEM_TMPL
    text = text.replace("__SPEC__", _spec_text() or "（规范文件读取失败，按上面的硬规则执行）")
    text = text.replace("__STORY_SPEC__",
                        _story_spec_text() or "（规范文件读取失败，按上面的硬规则执行）")
    text = text.replace("__MAX_BUBBLE__", str(comic_writer.MAX_BUBBLE))
    text = text.replace("__MAX_LABEL__", str(comic_writer.MAX_LABEL))
    text = text.replace("__MAX_LABELS__", str(comic_writer.MAX_LABELS))
    text = text.replace("__PANELS__", str(panels))
    text = text.replace("__PACING__", comic_writer._pacing(panels))
    return text


# ─── 工具执行 ───────────────────────────────────────────

def _parse_args(raw):
    """工具参数是模型吐的 JSON 串，容错解析（失败返回 {}）。"""
    import json
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def _dedup_key(name, args):
    """算「这个调用是不是查过的东西」——返回去重键，None 表示不该去重。

    同一稿里查过的词不再查第二次。实测（2026-10-11，22:05 那次运行）：
    223 次 search_tags 里 **76 次是跨轮重复**（`pov hands` / `backlighting`
    各查了 5 遍），**轮内重复 0 次**——所以缓存挂在整次 run 上，不是挂在一轮里。

    模型为什么重查：它要的结果被 MAX_TOOL_CHARS 砍掉了（见那个常量的注释）。
    两处一起治——上限放宽让它一次看全，确有重复时直接告知「已查过」，那条
    就不占本轮的字符预算了。`random` 抽标签刻意不去重：那是有意多抽几次，
    同样的参数本来也该给出不同结果。查询词先 lower + 去空格再比，
    免得 `Tears` / ` tears ` 当成两个词各查一遍。
    """
    if name == "search_tags":
        if args.get("random"):
            return None
        q = str(args.get("query") or "").strip().lower()
        return ("search_tags", re.sub(r"\s+", " ", q)) if q else None
    if name in ("read_file", "web_search"):
        for k in ("path", "query"):
            v = str(args.get(k) or "").strip()
            if v:
                return (name, re.sub(r"\s+", " ", v))
    return None


def _run_tool(call):
    """执行一次工具调用，返回给模型看的结果文本。"""
    name = (call.get("name") or "").strip()
    if name not in _TOOLS:
        return ("错误: 没有这个工具。你只有 search_tags（查标签库）、"
                "read_file（读 skills/ 内的文件）、web_search（联网查角色出处）。")
    args = _parse_args(call.get("args"))
    query = str(args.get("query") or "").strip()
    try:
        if name == "search_tags":
            if args.get("random"):
                return search_tags(random=True, cat=args.get("cat"),
                                   count=args.get("count") or 3,
                                   pattern=args.get("pattern"))
            if not query:
                return "错误: 缺少 query（要随机抽就把 random 设为 true）。"
            return search_tags(query)
        if name == "read_file":
            path = str(args.get("path") or "").strip()
            if not path:
                return "错误: 缺少 path（相对 skills/ 的路径）。"
            return _TOOLS["read_file"](path)
        # web_search：search_agent 那边也是按位置传 (query, 条数)，同口径
        if not query:
            return "错误: 缺少 query。"
        try:
            n = max(1, min(int(args.get("max_results") or 5), 10))
        except Exception:
            n = 5
        return _TOOLS["web_search"](query, n)
    except Exception as e:                    # 工具炸了不能带崩编剧
        log.exception("[comic-agent] %s 执行失败", name)
        return "工具执行失败: " + str(e)


def _strip(text):
    """剥掉模型混进来的工具调用块（复用 agent 里那份，两族都过）。"""
    return _strip_tool_blocks(text or "").strip()


def _emit(progress, msg):
    """进度回调只用于界面展示，出任何问题都不该影响主链路。"""
    if not progress:
        return
    try:
        progress(msg)
    except Exception:
        pass


# ─── agent 循环 ─────────────────────────────────────────

def run_rounds(messages, provider=None, model=None, max_rounds=MAX_ROUNDS,
               progress=None):
    """跑 agent 循环，返回模型最终交出的正文（可能为空串）。

    轮次分两段：

        1 .. max_rounds - WRITE_RESERVE   查库段：工具调用会被执行
        之后 WRITE_RESERVE 轮              强制动笔段：**工具关闭**，只要正文

    为什么必须有强制段（2026-10-11 用户实测）：第一版把「查够了就动笔」交给
    模型自己判断，结果两稿 24 轮**全程在查库**——日志里第 12 轮的 6 行全是
    search_tags，上限一到就返回空正文，下游报「只解析到 0 格」。提示词要求
    「每个标签都先查一次」，3 格 ≈ 60 个标签，模型一轮只发 6 个查询，光查库
    就得十轮，而它不知道自己在烧最后的机会。所以预算改由代码管。

    返回空串由调用方 `write_script` 负责回炉/报错——本函数只管跑轮到有正文为止。
    """
    fallback = ""             # 过程稿兜底：只在最终也没交出正文时才用
    nudged = False            # 第一轮没查库时只打回一次，防死循环
    seen = {}                 # 已查过的 (工具, 参数) → 首次查它的轮号（跨轮去重）
    search_rounds = max(1, max_rounds - WRITE_RESERVE)

    for round_no in range(1, max_rounds + 1):
        forced = round_no > search_rounds
        if round_no == search_rounds + 1:
            log.info("[comic-agent] 查库段用完（%d 轮），转入强制动笔", search_rounds)
            _emit(progress, "查库结束，开始写剧本")
            messages.append({"role": "user", "content": _FORCE_WRITE})

        try:
            reply = llm.call_llm(messages, timeout=CALL_TIMEOUT,
                                 provider=provider, model=model)
        except Exception as e:
            log.warning("[comic-agent] 第 %d 轮调用失败：%s", round_no, e)
            return fallback
        if not reply or not reply.strip():
            log.warning("[comic-agent] 第 %d 轮空回复", round_no)
            return fallback

        # 强制动笔段：工具已关闭，只认正文。模型在这里输出工具调用只能再催一轮，
        # 那些行不执行——一执行就把这一轮又变回查库轮，永远收敛不了。
        if forced:
            doc = _strip(reply)
            if doc:
                log.info("[comic-agent] 第 %d 轮（强制）交出正文 %d 字",
                         round_no, len(doc))
                return doc
            log.warning("[comic-agent] 第 %d 轮（强制）仍只输出工具调用", round_no)
            _emit(progress, "模型还在查库，继续催稿")
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content":
                             "你又输出了工具调用——**工具已经关闭**，那几行等于"
                             "什么都没写。现在只输出剧本本身（每一格都要有 "
                             "`画面:` 和 `气泡:`），标签用你已经查到的，不要新造。"})
            continue

        calls = parse_tool_calls(reply)
        if not calls:
            # 第一轮直接交正文 = 凭记忆编标签，正是本模块要治的毛病。
            # 实测（2026-10-11）：系统提示里写「第一步永远是查库」**挡不住**——
            # 模型觉得自己知道那些标签，两稿都是一轮就交卷，写出 `pouting` /
            # `grey_lighting` / `hand_out` 这些库里没有的词。所以这里用代码
            # 打回一次，逼它先查。只打回一次：第二轮仍不查就认了（认的是
            # 「模型不肯配合」，不是「标签没问题」——下游还有出口校验兜底）。
            if round_no == 1 and not nudged:
                nudged = True
                log.info("[comic-agent] 第 1 轮没查库，打回要求先查")
                _emit(progress, "模型想直接写，已打回要求先查库")
                fb = _strip(reply)
                if fb:
                    # 留作兜底：这份稿子标签没验过，但万一模型被打了回之后就
                    # 再也不交稿（实测就是这样），有它总比空手强。
                    fallback = fb
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content":
                                 "停——你还没有查过库就直接写剧本了。剧本里每一个"
                                 "标签都必须查证过才能用。请**先**输出工具调用，"
                                 '格式必须是这样一行：[[TOOL:search_tags]]{"query": "抓手腕"}'
                                 "（一次可以写多行，把这一话要用的标签一次查全），"
                                 "等我把结果给你之后再写剧本。"})
                continue
            doc = _strip(reply)
            log.info("[comic-agent] %d 轮结束，交出正文 %d 字", round_no, len(doc))
            return doc or fallback

        # 过程稿兜底：模型边查边写时那半截不算成品（收尾轮的完整剧本才算），
        # 但真到最后什么都没交出来时，有半截比空手强。
        # ⚠️ 原版这里**不留**过程稿，而唯一给 last_text 赋值的地方就是上面那个
        # 「无工具调用」分支——那个分支必然 return，于是「达到上限则返回最后一稿」
        # 永远返回空串，模块承诺的兜底从来没生效过（2026-10-11 实测「0 格」）。
        doc = _strip(reply)
        if doc:
            fallback = doc

        results = []
        for c in calls:
            cname = (c.get("name") or "?").strip()
            a = _parse_args(c.get("args"))
            if a.get("random"):
                what = "随机 cat=%s" % a.get("cat")
            else:
                what = a.get("query") or a.get("path") or ""
            key = _dedup_key(cname, a)
            if key and key in seen:
                # 已经查过就**不再执行**，只回一句。内容就在上面的对话里，
                # 重复贴一遍既挤占本轮预算、又鼓励它下轮接着重复。
                r = ("（`%s` 第 %d 轮已经查过，结果就在上面的对话里，不再重复"
                     "贴一遍——**同一个词不要反复查**）" % (what, seen[key]))
                log.info("[comic-agent] 第 %d 轮 %s(%s) → 跳过（第 %d 轮已查）",
                         round_no, cname, what, seen[key])
                _emit(progress, "%s(%s) 已查过" % (cname, what))
            else:
                r = _run_tool(c)
                if key:
                    seen[key] = round_no
                log.info("[comic-agent] 第 %d 轮 %s(%s) → %d 字",
                         round_no, cname, what, len(r))
                _emit(progress, "%s(%s)" % (cname, what))
            results.append("【%s】\n%s" % (cname, r))
        joined = "\n\n".join(results)
        if len(joined) > MAX_TOOL_CHARS:
            joined = joined[:MAX_TOOL_CHARS] + "\n…（结果已截断）"

        left = search_rounds - round_no
        if left > 0:
            tail = ("\n\n还剩 %d 轮查库机会，用完后系统会**强制**你动笔（届时工具"
                    "关闭）。要查的词请一次性批量发出来（一轮写 15~25 行都行，"
                    "比一轮只查 5 个快得多）；**已经查过的词不要重复查**。"
                    % left)
        else:
            tail = "\n\n这是最后一轮查库机会，下一轮必须动笔输出完整剧本。"

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content":
                         "【工具结果 %d/%d 轮】\n%s%s"
                         % (round_no, search_rounds, joined, tail)})

    log.warning("[comic-agent] 达到 %d 轮上限仍未交出正文（兜底稿 %d 字）",
                max_rounds, len(fallback))
    return fallback


# ─── 校验 ───────────────────────────────────────────────

def check(text, panels):
    """按契约 + 标签库校验剧本，返回 (是否合格, 问题描述)。

    结构性问题（格数不对、字段缺）先短路——那种情况下列标签问题没有意义。
    其余判据**一次全报**：原来的写法是命中一条就返回，模型一轮只看得到
    一条错，改完下一轮又冒出另一条。2026-10-11 实跑逮到：一稿里 `hand around
    wrist`（禁用）和 `solo` 配第二方身体部位（冲突）同时存在，模型只被告知了
    后者——前者一路活到交稿。两稿预算下，一条一条报根本改不完。

    **只判语义，不判存在性**：`hand around wrist` 语义错、`solo` 配第二方身体
    部位互斥、衣物 / 随身物件没写颜色（`bare_wearables`）、同一件东西写两遍
    （`duplicate_wearables`）、POV 剧本里冒出 `1boy`（`pov_conflicts`），这几类
    判不合格；而「库里查不到」不算错（`white_earphones`、`black_backpack` 都不
    在库，但写法是对的），只写日志。库是参考，不是白名单。
    """
    ok, why = comic_writer._validate(text, panels)
    if not ok:
        return False, why

    problems = []
    conf = solo_conflicts(text)
    if conf:
        items = "、".join("格 %s 的 `%s`" % (no, tg) for no, tg in conf[:6])
        problems.append(
            "这些格既写了 `solo`（画面里只有一个人），又要求第二个人或第二方"
            "身体部位（%s）入画：Danbooru 官方计数规则里「一只手也算一个角色」，"
            "两条指令互斥。把 `solo` 换成 `solo focus`，并补上 `1other`。"
            "⚠️ **不要写 `1boy`**——它会补出一个完整的人（见下一条）。"
            % items)
    banned = banned_tags(text)
    if banned:
        items = "、".join("格 %s 的 `%s`" % (no, tg) for no, tg in banned[:6])
        problems.append(
            "这些格用了语义错误的标签：%s。`hand around wrist` 库里"
            "确实存在，但它没写明是谁的手，实测会被画成「她自己抓自己"
            "手腕」。要抓别人的手腕只能写 `holding another's wrist`"
            "（视角方的手再加 `pov` + `pov hands`），不用抓的格就整段"
            "删掉、只写 `1girl, solo`。" % items)
    bare = bare_wearables(text)
    if bare:
        items = _fmt_by_panel(bare)
        problems.append(
            "这些衣物 / 随身物件**只有类别名、没写颜色**：%s。画面模型没有记忆，"
            "同一件东西不写颜色，每一格都会换个色。把颜色写进这个标签里："
            "`backpack` → `white backpack on one shoulder`、`sailor collar` →"
            " `white sailor collar`、`pleated skirt` → `white pleated skirt`。"
            "款式和姿态（`on one shoulder`）**不等于**颜色。" % items)
    dup = duplicate_wearables(text)
    if dup:
        items = _fmt_by_panel(dup)
        problems.append(
            "同一格里同一件东西写了两次（%s）：`backpack` 和 `backpack on one "
            "shoulder` 是**同一件**，两个都写模型会画两个包。合并成一个标签，"
            "颜色写在里面（`white backpack on one shoulder`）。" % items)
    pov = pov_conflicts(text)
    if pov:
        items = "、".join("格 %s" % no for no, _ in pov[:6])
        problems.append(
            "这一话是第一人称视角（已经有 `pov` / `pov hands` 的格），%s 却写了 "
            "`1boy`：`1boy` 会补出**一个完整的人**（脸、身体、衣服全都有），"
            "和只出手的前几格完全接不上。把 `1boy` 删掉——「我」只以手或手臂"
            "入画，第二方用 `1other` 表示，手部标签照旧（`pov hands` + "
            "`holding another's wrist`）；也**不要给「我」写服饰**"
            "（`black hoodie` / `grey pants` 都会把整个人画出来）。" % items)
    # 库存在性**判不合格会反噬**（2026-10-11 由用户定调：tags 库只是参考，
    # AI 要有自我判断）。原来这里把「库里查不到」当硬回炉，后果是**惩罚写具体**：
    # 库里没有 `white_earphones`、`black_backpack`，模型老老实实写「白色耳机」
    # 就被打回，只能退回泛化的 `earphones`——正好和规范「物件也要写颜色」对撞。
    # 所以现在只在日志里留一条参考，不进回炉消息。
    # ⚠️ 保留 `banned_tags` / `solo_conflicts` 做硬判据：那两条是**语义错**
    # （库里存在但画出来是错的 / 两条指令互斥），不是「库里没有」。
    miss = missing_tags(text)
    if miss:
        log.info("[标签参考] %d 个标签库里查不到（仅供参考，不拦）：%s",
                 len(miss),
                 "、".join("格 %s 的 `%s`" % (no, tg) for no, tg in miss[:8]))
    if problems:
        return False, "\n".join(problems)
    return True, ""


# ─── 对外入口 ───────────────────────────────────────────

def write_script(brief, panels=3, character="", provider=None, model=None,
                 max_rounds=MAX_ROUNDS, tries=2, progress=None):
    """查库写剧本。返回剧本文本（markdown，已含头部备选记录）。

    参数：
      - provider / model：这一轮用哪家模型（漫画页下拉），不传走全局默认。
      - tries：不合格时的回炉次数。**最后一次容忍**——把校验警告写进日志后
        原样返回，不抛异常。理由：硬卡死会让整个漫画流程停下来，而一份带
        「标签没验过」的剧本用户在确认环节还能自己改；抛异常则什么都拿不到。
        例外是**一稿都没写出正文**（模型全程在查库）：那时空手返回等于把一个
        空剧本落盘，抛 RuntimeError 更有用（调用方转成可见的报错）。
      - progress：可选回调，每执行一次工具调一次，用来在界面上显示
        「正在查库 arm_grab」这类实时进度。
    """
    if not (brief or "").strip():
        raise ValueError("设定不能为空")

    # provider 留空 = 用「当前全局生效的那家」（.env 的 LLM_PROVIDER，网页端
    # 切过就是切后的那家）。**不能真的把 None 传下去**——llm.call_llm 在
    # provider 为空时压根不看全局默认，而是直接从降级链的链头开跑（实测是
    # volc），于是 `.env` 里写着的 LLM_PROVIDER=deepseek 被整个绕过。
    # 2026-10-11 实跑逮到：日志是 `volc / deepseek-v4-flash-ga` 而不是 deepseek。
    provider = (provider or "").strip() or llm.CURRENT_PROVIDER or None

    base = [
        {"role": "system", "content": _system_prompt(panels)},
        {"role": "user", "content": comic_writer._user_prompt(brief, panels, character)},
    ]
    # 回炉时只带「上次交出的剧本 + 错因」，不带工具中间结果——那些加起来几万字，
    # 带进下一轮会把上下文顶爆，而它们对改稿没有帮助（错因已经说清了）。
    feedback = []
    last = ""
    last_notes = []
    why = ""
    for attempt in range(1, tries + 1):
        _emit(progress, "第 %d/%d 稿：开始查库" % (attempt, tries))
        text = comic_writer._clean(run_rounds(base + feedback, provider=provider,
                                              model=model, max_rounds=max_rounds,
                                              progress=progress))
        clean, notes = _extract_notes(_strip_preamble(text))
        # 空正文单独处理：这跟「交了稿但标签不对」是两回事。
        # 实测（2026-10-11）：模型整轮都在查库没动笔，正文是空串，而原来的代码
        # 把它当普通不合格稿回炉——回炉消息里带一条**空的** assistant 消息，模型
        # 看不出自己错在哪（看到的就是"上一份稿子"），于是接着查库；最后空串被
        # 原样交给 comic_api，照样建任务目录、写一个空 script.md，用户看到一片
        # 空白，还以为只是「解析 bug」。这里给它一句对症的指令，且**不覆盖**上一
        # 稿——上一稿至少是有内容的，丢不得。
        if not clean.strip():
            why = "模型整轮都在查库，没有写出剧本"
            log.warning("[comic-agent] 第 %d 稿没交出正文", attempt)
            feedback = [{"role": "user", "content":
                         "你上一轮只顾着查标签库，**没有交剧本**。这一轮请立刻输出"
                         "完整剧本（每一格都要有 `画面:` 和 `气泡:`，只输出剧本本"
                         "身），标签就用你已经查到的那些，不要新造词，也不要再输出"
                         "工具调用。"}]
            continue
        ok, why = check(clean, panels)
        if ok:
            log.info("[comic-agent] 剧本合格（第 %d 稿，%d 格，备选 %d 条）",
                     attempt, panels, len(notes))
            return _place_notes(clean, notes)
        log.warning("[comic-agent] 剧本不合格（第 %d 稿）：%s", attempt, why)
        last, last_notes = clean, notes
        feedback = [
            {"role": "assistant", "content": clean},
            {"role": "user", "content":
             "这份剧本不合格：%s\n请重新输出**完整**剧本（所有格都要重写一遍），"
             "只输出剧本本身。" % why},
        ]

    # 一稿都没写出来（全是空正文）。返回空串会让 comic_api 落一个空
    # script.md、前端只见空白框——宁可报错让人看见。这是「容忍」的边界：
    # 「标签没验过但稿子在」能容忍（用户在确认环节还能自己改），
    # 「什么都没有」容忍不了。
    if not last.strip():
        log.error("[comic-agent] %d 稿都没写出正文，报错", tries)
        raise RuntimeError("编剧 %d 稿都只顾查库、没有写出剧本" % tries)

    # 最后一次也没过：原样交出，把问题写在头部的注释行里让人看见。
    # ⚠️ 绝不能把警告拼到剧本**末尾**——parse_script_text 会把它当续行接进
    # 最后一格的台词里，台词就多了半截校验信息（见模块说明）。
    log.error("[comic-agent] %d 稿都不合格，返回最后一稿（问题：%s）", tries, why)
    if why:
        last_notes = list(last_notes) + ["[警告] 校验未通过：%s" % why]
    return _place_notes(last, last_notes)

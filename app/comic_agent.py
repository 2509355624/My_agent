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
   （`disembodied hand` / `arm grab` / `grabbing another's wrist`），但不查库
   就永远不知道。

第一类靠提示词能治，第二类**只能靠查库**——32.8 万条标签库和检索工具
（`search_tags`）都是现成的，缺的只是「让编剧真的去查」这一步。

## 三道防线

    ① 提示词：规范全文 + 「只能写标签」的硬规则 + 「先查完再写」的工作方式
    ② Agent 循环：模型自己调 search_tags / read_file / web_search 查证
    ③ 代码判据：产出后把每格 `画面:` 拆成标签**逐个查库**，查不到的当不合格
       打回重写（最多 tries 次，最后一次容忍并记警告）

③ 是必须的——项目一贯的做法是「模型做不到的规则，由代码判」。①②是软约束，
模型仍可能蒙一个库里没有的 tag 出来，只有③能兜住。

## 两条容易踩的实现约束

- **备选记录必须抽走**。规范要求「歧义必问」，但自动流程没人可问（用户
  2026-10-11 拍板：自选 + 记备选）。agent 在格末写 `备选:` 行，代码用
  `_extract_notes` 抽出来统一挪到剧本**头部**。不抽不行——`comic.parse_script_text`
  对未知行是按「续行」处理的（`cur[field] += line`），留在格里会被拼进
  `画面:` 或 `标签:`，直接污染提示词。
- **`spec.md` 里没有任何花括号**（已核对），但仍用 `str.replace` 而不是
  `str.format` 拼模板：规范是用户随时会改的外部文件，将来写进一个 `{}`
  就会让 format 抛异常，replace 不会。

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
# 单次 LLM 调用超时。比 search_agent 的 30s 长：编剧最后一轮要一口气吐出
# 整份剧本（十格），输出量大。
CALL_TIMEOUT = 90
# 单次工具结果上限。工具结果要一直留在 messages 里，不封顶会把上下文撑爆。
MAX_TOOL_CHARS = 2000

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


_SYSTEM_TMPL = """你是条漫分镜编剧，同时是 Anima 提示词工程师。

你写的每一格 `画面:` 会**直接**喂给 Anima 生图工作流，所以每一个标签都必须
真实存在于 Danbooru 标签库（32.8 万条）里。写错一个词，画面就会错位、多手、
甚至凭空多出一个人。

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
- 每个要用的标签都先调 search_tags 查一次。查中文（「抓手腕」「哭泣」）可以，
  直接查英文 tag（arm_grab、tears）也可以；不确定英文叫什么就查中文。
- 库里命中 → 用返回的 tag 名（把下划线换成空格写进剧本）。
- 一个中文名出来几十个候选 → 自己挑最贴合画面的那一个，把**其他候选**记进
  `备选:` 行（见下面「歧义怎么办」）。
- 查不到 → 换个说法再查；换两三次仍查不到，就用语义最近的库里标签，
  **绝不要自己造一个库里没有的 tag**。
- 需要时用 read_file 读完整规范（anima-tags/references/spec.md）与示例
  （anima-tags/references/examples.md）；用 web_search 查某个角色的作品出处。

【画面字段的硬规则】（违反任何一条，这一格会被代码打回重写）
1. **只能写逗号分隔的英文标签，绝不许出现句子。**
   错：girl stepping off the bus with one foot on the ground
   对：stepping off, bus, one foot on ground
   错：girl with watery eyes about to cry
   对：tears, crying, looking down
2. **顺序**：安全级 → 人数 → 主体特征（发型/瞳色/配饰）→ 服装 → 动作姿势 →
   场景 → 构图镜头。同类标签聚在一起。
3. **不要写质量词和画师 tag**（masterpiece / best quality / ultra-detailed /
   absurdres / 画师名）——工作流模板已经钉好了，重复写只会稀释权重。
4. **安全级必须有且只有一个**，只能从 safe / sensitive / nsfw / explicit 里选。
   注意 **不是 `sfw`** —— `sfw` 不在 Anima 规范里，写了会被打回。
5. **只出手/手臂入画时，必须用「只有手」的那类标签，绝不许把动作写成句子。**
   错：male hand grabbing her wrist from behind ← `male` 会画出整个人 + 多只手
   对：disembodied arm, hand grab, hand around wrist ← 只有手臂入画
   查「手」「抓」「手腕」「牵着」把这些标签找齐再用。
6. 每格必须有人数标签（1girl、solo）。视角方（哥哥/「你」）只允许手或手臂
   入画，**脸和身体一律不许出现**，画面仍以 1girl 开头。
7. 服饰颜色款式、灯光氛围每格写全并保持一致（见规范）。
8. **下面这些词看着都像对的，但库里没有，实测过了，不许写**：
   `sfw`（用 safe）、`arm grab`（用 hand grab）、`male hand`（用 disembodied arm）、
   `medium shot`（用 upper body / cowboy shot / close-up）、
   `soft lighting`、`cinematic lighting`（用 backlighting / sidelighting / dim lighting）、
   `stepping off` / `getting off`（用 standing on one leg 之类）、
   `one foot on ground`。
   **这类"差不多的写法"是本次最容易踩的坑**——凡是不确定的，先查库再写。

【Anima 提示词规范（全文，必须遵守）】
本工作流用**纯标签**写法——规范第 5 节的「纯自然语言 / 混合写法」本流程不用。

======== 规范全文开始 ========
__SPEC__
======== 规范全文结束 ========

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
气泡: <中文台词>
气泡2: <可选，第二句台词；没有就整行删掉>
标签: <可选，旁白解说条；没有就整行删掉>

## 02
...

硬性要求：
1. 每格以 `## NN` 开头，NN 为两位数字、从 01 起连续；**格数必须正好 __PANELS__ 格**。
2. `画面:` 全是英文标签，**绝对不许出现中文**。
3. 台词口语化、有活人感（可用省略号、语气词、话说一半），禁止书面腔；
   表达此刻的情绪，不复述画面。`气泡:` 不超过 __MAX_BUBBLE__ 字；
   话多就再写一行 `气泡2:`（同样不超过 __MAX_BUBBLE__ 字），不是把一条写长。
4. `标签:` 是画面顶部的旁白条，不超过 __MAX_LABEL__ 字，只用于交代时间/前提
   跳转，不是每格都要；同一格尽量别和 `气泡2:` 同时用。
5. 剧情节奏：__PACING__
6. 不要写 `任务号:` 这一行。
"""


def _system_prompt(panels):
    text = _SYSTEM_TMPL
    text = text.replace("__SPEC__", _spec_text() or "（规范文件读取失败，按上面的硬规则执行）")
    text = text.replace("__MAX_BUBBLE__", str(comic_writer.MAX_BUBBLE))
    text = text.replace("__MAX_LABEL__", str(comic_writer.MAX_LABEL))
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
    """跑 agent 循环，返回模型最终交出的正文（没收敛就返回最后一次的正文）。

    与 search_agent.search 同一套骨架，两点不同：
      - 中间轮（有工具调用的）正文**一律丢弃**——编剧边查边写时那半截稿子
        是过程稿，收尾轮的完整剧本才算数。
      - 保留最后一轮正文作为兜底：模型 12 轮都在查库没收敛时，至少把最后
        一次输出交出去，别白烧这些 token。
    """
    last_text = ""
    nudged = False            # 第一轮没查库时只打回一次，防死循环
    for round_no in range(1, max_rounds + 1):
        try:
            reply = llm.call_llm(messages, timeout=CALL_TIMEOUT,
                                 provider=provider, model=model)
        except Exception as e:
            log.warning("[comic-agent] 第 %d 轮调用失败：%s", round_no, e)
            return last_text
        if not reply or not reply.strip():
            log.warning("[comic-agent] 第 %d 轮空回复", round_no)
            return last_text

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
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content":
                                 "停——你还没有查过库就直接写剧本了。剧本里每一个"
                                 "标签都必须查证过才能用。请**先**输出工具调用，"
                                 '格式必须是这样一行：[[TOOL:search_tags]]{"query": "抓手腕"}'
                                 "（一次可以写多行，把这一话要用的标签一次查全），"
                                 "等我把结果给你之后再写剧本。"})
                continue
            doc = _strip(reply)
            if doc:
                last_text = doc
            log.info("[comic-agent] %d 轮结束，交出正文 %d 字", round_no, len(doc))
            return last_text

        results = []
        for c in calls:
            r = _run_tool(c)
            results.append("【%s】\n%s" % (c.get("name") or "?", r))
            a = _parse_args(c.get("args"))
            if a.get("random"):
                what = "随机 cat=%s" % a.get("cat")
            else:
                what = a.get("query") or a.get("path") or ""
            log.info("[comic-agent] 第 %d 轮 %s(%s) → %d 字",
                     round_no, c.get("name") or "?", what, len(r))
            _emit(progress, "%s(%s)" % (c.get("name") or "?", what))
        joined = "\n\n".join(results)
        if len(joined) > MAX_TOOL_CHARS:
            joined = joined[:MAX_TOOL_CHARS] + "\n…（结果已截断）"

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content":
                         "【工具结果】\n" + joined
                         + "\n\n还没查够就继续输出工具调用；"
                           "查够了一次性输出完整剧本（只输出剧本本身）。"})

    log.warning("[comic-agent] 达到 %d 轮上限仍未收敛", max_rounds)
    return last_text


# ─── 校验 ───────────────────────────────────────────────

def check(text, panels):
    """按契约 + 标签库校验剧本，返回 (是否合格, 问题描述)。"""
    ok, why = comic_writer._validate(text, panels)
    if not ok:
        return False, why
    miss = missing_tags(text)
    if miss:
        items = "、".join("格 %s 的 `%s`" % (no, tg) for no, tg in miss[:8])
        more = "（仅列前 8 个）" if len(miss) > 8 else ""
        return False, ("这些标签在库里查不到：%s%s。"
                       "用 search_tags 查一个真实存在的近似标签替换，别自己造词。"
                       % (items, more))
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

    # 最后一次也没过：原样交出，把问题写在头部的注释行里让人看见。
    # ⚠️ 绝不能把警告拼到剧本**末尾**——parse_script_text 会把它当续行接进
    # 最后一格的台词里，台词就多了半截校验信息（见模块说明）。
    log.error("[comic-agent] %d 稿都不合格，返回最后一稿（问题：%s）", tries, why)
    if why:
        last_notes = list(last_notes) + ["[警告] 校验未通过：%s" % why]
    return _place_notes(last, last_notes)

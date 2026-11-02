"""search_agent —— 只做一件事：查标签库，返回一份资料。

**定位（用户 2026-10-06 反复钉死）：唯一任务就是搜索。**

    它的唯一任务就是搜索，唯一任务就是搜索……它只需要搜索任何的 context，
    它只有一个任务就是搜索。就只有搜索这个任务，搜索完返回结果，
    让那个单次 API 它自己去调。

所以这里**刻意没有**这些东西，加进来就是错的：

- **不吃会话历史**。它拿到的只有用户这一轮的绘图需求（原话）。
  「用户如果反复修改怎么办」由调用方解决——把本轮原话传进来即可，
  指代（「再画一张」）由调用方在传参前拼好，不在这里维护历史。
- **不背大大怪的人设**。它不画图、不回话、不评价、不写提示词。
- **不产出提示词**。它产出的是**资料**（查到了哪些角色/标签），
  提示词由后面的单次生图 API 自己写。

## 2026-10-06 架构调整：定点抽取在前，这一层只管补漏

用户原话：「我唯一的目的其实就是 AI 不要犯错……我们进行搜索应该是一套工作流，
跑一次搜索，然后组合成差不多 500~1000 个字的这样一套东西发给这个生图 API，
让它去可以参考这个写法。」

## 2026-11-02 用户拍板：拆掉「代码定点抽取作前置」，这里吃**完整原话**

原话：「代码定点抽取给我去除，完整用户问题直接给搜索 agent，然后大模型自己
处理，我们人是无法用代码处理用户的问题的……主要还是要大模型自己去进行搜索
才是正确的，不然人工干预没有意义。」

所以 10-06 那套「① extract() 先扫库 → ② 本模块只补漏，靠 `hint` 告诉它别
重复查」**整段拆掉了**，连带 `hint` 参数一起删。原因不是它不准，而是它错位：
代码先扫一遍、再把「已经查过哪些词」写进提示词，等于用一份人写的规则框住
模型该查什么。代码扫漏的词，模型一看「这些已经查过了」就不会再补——而那
恰恰是最需要它自己判断的部分。

现在是单向的：

    用户原话（完整，一字不剪） → 本模块 search() → 一份资料（≤2000 字）

查什么、查几个、要不要联网，全由模型自己从原话里判断。

为什么要独立一层：中文名到 tag 的映射歧义极重——真实库里「初音未来」有 111 个
cat=4 候选、「胡桃」18 个、「银狼」3 个，而朴素取第一个必错（初音未来[0] 是
2019 台湾展版）。这一层负责**把候选捞全并筛掉明显不对的**，让单次生图 API
拿到干净的输入。它要解决的就是两件事：**角色不对** + **提示词不符合要求**。

两个工具（用户 2026-10-06 拍板「把网页搜索加上」）：

- `search_tags`：本地 Danbooru 标签库（32.8 万条）。**主力**，查角色/作品/标签；
  用户说「随机」时用 `random=true` 从库里均匀抽真条目。
- `web_search`：联网搜索。**只在标签库查不到、或候选太多拿不准时用**——
  它一次返回几百到上千字，比查库贵得多，不能拿来当默认手段。

日志里带会话标识：`[search] … [group_123]`。2026-10-06 排查时发现不带标识
根本分不清哪条埋点属于哪个会话（只能靠同毫秒的 `[cache]` 行反推），补上。
"""

import logging
import re

from app import llm
from app.agent import parse_tool_calls, _strip_tool_blocks
from app.tools.normal.search_tags import search_tags
from app.tools.normal.web_search import tool as _web_search_tool

log = logging.getLogger(__name__)

MAX_ROUNDS = 10
# 资料硬上限。2026-11-02 用户定：**2000 字**。
# 背景：这天把「代码定点抽取作前置」整段拆了（见模块说明），资料只由这一层
# 产出，原来是「定点抽取 600 + 补漏 600」两段合计 1000，现在单段 2000。
MAX_DOC_CHARS = 2000
# 单次工具结果上限。web_search 一次能吐上千字，10 轮累积会把上下文撑爆；
# 工具结果要一直留在 messages 里，所以这里统一收口。
MAX_TOOL_CHARS = 1500
# 单次 LLM 调用超时。搜索是生图链路的前置，不能拖太久。
CALL_TIMEOUT = 30
# 模型判定「这轮没有需要查的东西」时的固定输出。命中就当**空资料**返回——
# 否则这句会被当成资料塞进生图模板，下游还得自己无视它。
# 2026-10-06 实测背景：没有这个出口时，模型面对「三档」「泳衣颜色变浅」
# 这类不含专有名词的请求会反复换词空转（一场 8 轮 24 次调用、14,487 token）。
_NO_NEED_RE = re.compile(r"^\s*无需查库[。.！!～~\s]*$")

# 工具表：名字 → 可调用对象。`web_search` 取 tool 字典里的 function，
# 不依赖它的私有函数名。
_TOOLS = {
    "search_tags": search_tags,
    "web_search": _web_search_tool["function"],
}


def _sk():
    """当前会话标识，只用于日志。取不到就返回 "-"（测试环境没有会话）。"""
    try:
        from app import qq_api
        return qq_api.current_session_key() or "-"
    except Exception:
        return "-"

SEARCH_PROMPT = (
    "你是搜索员，唯一任务是查资料。\n"
    "\n"
    "【工具 1】search_tags —— 在 Danbooru 标签库（32.8 万条，含中文对照）里检索。\n"
    "  查词：[[TOOL:search_tags]]{\"query\": \"银狼\"}\n"
    "  query 给中文名（银狼、初音未来、胡桃）或英文 tag 名（silver_wolf）。\n"
    "  返回候选列表，每行是「tag 中文名 cat=类别」（4=角色 3=作品 0=通用 1=画师）。\n"
    "  随机抽：[[TOOL:search_tags]]{\"random\": true, \"cat\": \"1\", \"count\": 3}\n"
    "  cat：1=画师（默认）、4=角色、0=通用（**必须带 pattern**，如 \"dress|skirt\"）。\n"
    "**这是你的主力工具。**\n"
    "\n"
    "【工具 2】web_search —— 联网搜索。\n"
    "参数 query（搜索词）、max_results（条数，默认 5）。\n"
    "**只在标签库查不到、或候选太多拿不准时才用它**（比如确认某个角色的作品出处）。\n"
    "它一次返回几百上千字，比查库贵得多，**不要拿它查普通描述词或反复搜同一件事**。\n"
    "\n"
    "可以一次输出多行，查多个词。\n"
    "\n"
    "【你要做的事】—— 用户原话完整给你了，**查什么由你自己判断**。\n"
    "1. **有专有名词就先查**：把原话过一遍，挑出值得查的名字——角色名、作品名、\n"
    "   画师名，任何名字（中文、外文、缩写都算）。**第一轮就先调工具查它们**，\n"
    "   不许凭记忆写——同一个中文名可能有几十个候选（初音未来 111 个、胡桃 18 个），\n"
    "   凭印象写的角色 tag 大概率是错的。\n"
    "2. **同义词桥接**：用户的说法和库里的说法常常不一样（说「泳衣」库里叫\n"
    "   「泳装」、说「初音」库里叫「初音未来」、说「绫华」库里叫「神里绫华」）——\n"
    "   **没命中、命中明显不对、或者候选太多拿不准，就换个说法再查一次**。\n"
    "   注意坑：短名字很容易撞到同名的衍生角色（查「绫华」可能先给一个\n"
    "   `ayaka_(charismabard)`），那不是用户要的那个；把名字补全再查。\n"
    "   这是你最主要的价值。\n"
    "3. 库里查不到的专有名词（缩写、新作品、没有正式条目的角色）→ 用 web_search\n"
    "   确认它到底是谁、出自哪里，再拿确认到的名字回来查库。\n"
    "4. 普通描述词（发型、服饰、动作、表情、场景、气氛）**不用查**，\n"
    "   那些你自己写比库里的 tag 更贴切。\n"
    "5. **整句里没有专有名词**（都是普通描述词）→ **一个工具都别调**，\n"
    "   直接输出「无需查库」四个字就结束。别硬找词去查——反复换词空转既慢又贵，\n"
    "   还查不到东西。\n"
    "6. 用户说「随机」时，用 random=true 抽真条目。**同一个词不要重复查。**\n"
    "\n"
    "【资料的写法】\n"
    "- **按类别分组**，一组一个标题，下面每条写「中文 → tag 名」，例如：\n"
    "  角色：银狼 → silver_wolf_(honkai:_star_rail)（崩坏：星穹铁道）\n"
    "  服饰：泳装 → swimsuit\n"
    "  分这几类：角色 / 作品 / 画师 / 服饰 / 动作 / 表情 / 场景。"
    "**有哪类写哪类，没有的别硬凑标题。**\n"
    "- **只写工具结果里真实出现过的 tag，一个字都不许编。** 工具说没查到就写没查到。\n"
    "- 控制在 2000 字以内。不要写提示词、不要写画法建议、不要复述用户的话。\n"
    "\n"
    "整理好直接输出资料本身，不要加「以下是资料」这类开场白。\n"
)


def _strip(text):
    """把资料里可能混进来的工具调用块去掉，并截到硬上限。

    剥块复用 agent 里那份 `_strip_tool_blocks`——它两族都过（`[[TOOL:…]]`
    方括号族 + Anthropic 式 XML 族），且按花括号配对精确界定参数结束位置，
    能正确处理「工具块后面紧跟正文」。自己写正则会在 XML 族上漏掉，
    或者在方括号族上把正文一起吃掉。
    """
    t = _strip_tool_blocks(text or "").strip()
    if len(t) > MAX_DOC_CHARS:
        t = t[:MAX_DOC_CHARS].rstrip() + "…"
    return t


def _run_tool(call):
    """执行一次工具调用，返回给模型看的结果文本。"""
    name = (call.get("name") or "").strip()
    fn = _TOOLS.get(name)
    if fn is None:
        return "错误: 没有这个工具，你只有 search_tags 和 web_search。"
    # parse_tool_calls 给的是 {"name":…, "args":{…}}；args 正常情况下已经是
    # dict，容错再兜一层字符串（模型吐半截 JSON 时）。
    args = call.get("args")
    if isinstance(args, str):
        args = _parse_args(args)
    args = args if isinstance(args, dict) else {}
    query = (args.get("query") or "").strip()
    try:
        if name == "web_search":
            if not query:
                return "错误: 缺少 query 参数。"
            n = args.get("max_results") or 5
            try:
                n = max(1, min(int(n), 10))      # 上限 10 条，防一次吐太多
            except Exception:
                n = 5
            out = fn(query, n)
        elif args.get("random"):
            # 随机模式：query 可以空。**只在要随机时才多传参数**——
            # 普通查词保持 `fn(query)` 这一个位置参数，别把调用形状改复杂。
            out = fn(random=True, cat=args.get("cat"),
                     count=args.get("count") or 3,
                     pattern=args.get("pattern"))
        else:
            if not query:
                return ("错误: 缺少 query 参数（要随机抽就传 random=true）。")
            out = fn(query)
    except Exception as e:                       # 工具炸了不能带崩搜索
        log.exception("[search] %s 执行失败", name)
        return "工具执行失败: " + str(e)
    out = out or ""
    if len(out) > MAX_TOOL_CHARS:                # 工具结果要一直留在 messages 里
        out = out[:MAX_TOOL_CHARS] + "\n…（结果已截断）"
    return out


def _parse_args(raw):
    """工具参数是模型吐的 JSON 串，容错解析（失败返回 {}）。"""
    import json
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def search(need, max_rounds=MAX_ROUNDS):
    """把用户原话交给模型，自己判断查什么，返回一份资料文本；查不到返回 ""。

    参数 need：用户这一轮的绘图需求（**完整原话，一字不剪**）。**不传历史**
    ——指代由调用方在传进来之前就解决掉（见模块说明）。

    2026-11-02 删掉了 `hint` 参数：它原来装的是「代码定点抽取已经查过哪些词」，
    塞进提示词让模型别重复查。用户原话是「人工干预没有意义」——代码扫漏的词，
    模型因为看到「已查过」就不再补，方向反了。现在查什么完全由模型自己判断。

    返回值直接当资料用，调用方负责塞进生图 API 的 prompt。任何异常都吞掉
    返回 ""：搜索是增强，不是主链路，它挂了不该让生图也挂。
    """
    text = (need or "").strip()
    if not text:
        return ""

    messages = [{"role": "user",
                 "content": SEARCH_PROMPT + "\n【用户的需求】\n" + text}]
    last_doc = ""
    last_results = ""

    for round_no in range(1, max_rounds + 1):
        try:
            reply = llm.call_llm(messages, timeout=CALL_TIMEOUT)
        except Exception as e:
            log.warning("[search] 第 %d 轮调用失败：%s [%s]", round_no, e, _sk())
            return last_doc or _strip(last_results)
        if not reply or not reply.strip():
            log.warning("[search] 第 %d 轮空回复 [%s]", round_no, _sk())
            return last_doc or _strip(last_results)

        calls = parse_tool_calls(reply)
        if not calls:
            # 没有工具调用 = 收尾轮，这一轮的正文就是资料。
            # 中间轮（有工具调用的）的正文一律丢弃——那是「边想边说」，
            # 发出去就是用户抱怨过的「好的，是这个工具吗？」刷屏。
            doc = _strip(reply)
            if _NO_NEED_RE.match(doc):
                # 模型明确说了没有要查的东西 → 当空资料，别把这句话喂给生图。
                log.info("[search] %d 轮结束：判定无需查库 [%s]",
                         round_no, _sk())
                return ""
            if doc:
                last_doc = doc
            log.info("[search] %d 轮结束，资料 %d 字 [%s]",
                     round_no, len(doc), _sk())
            return doc

        results = []
        for c in calls:
            r = _run_tool(c)
            results.append("【%s】\n%s" % (c.get("name") or "?", r))
            _a = c.get("args") if isinstance(c.get("args"), dict) else {}
            # 把参数一起记下来——只记返回字数的话，事后没法复盘它到底
            # 查了哪些词（10-06 排查「24 次调用」时只能靠猜）。
            if _a.get("random"):
                _what = "随机 cat=%s pattern=%s" % (_a.get("cat"),
                                                    _a.get("pattern"))
            else:
                _what = (_a.get("query") or "").strip()
            log.info("[search] 第 %d 轮调用 %s(%s)，返回 %d 字 [%s]",
                     round_no, c.get("name") or "?", _what, len(r), _sk())
        last_results = "\n\n".join(results)

        messages.append({"role": "assistant", "content": reply})
        messages.append({
            "role": "user",
            "content": ("【工具结果】\n" + last_results
                        + "\n\n还没查够就继续输出工具调用；"
                          "查够了就直接输出资料（2000 字以内）。"),
        })

    log.warning("[search] 达到 %d 轮上限，用最后一轮的工具结果 [%s]",
                max_rounds, _sk())
    # 全程都在调工具 = 模型没收敛。至少把最后一轮的工具结果交出去，别白烧
    # （2026-10-06 实录：5 轮 40+ 次调用、6,199 miss token，最后返回空串）。
    return last_doc or _strip(last_results)

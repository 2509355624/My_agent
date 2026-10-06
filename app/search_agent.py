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

为什么要独立一层：中文名到 tag 的映射歧义极重——真实库里「初音未来」有 111 个
cat=4 候选、「胡桃」18 个、「银狼」3 个，而朴素取第一个必错（初音未来[0] 是
2019 台湾展版）。这层负责**把候选捞全并筛掉明显不对的**，让单次生图 API
拿到干净的输入。它要解决的就是两件事：**角色不对** + **提示词不符合要求**。

成本（实测，按 0.5 token/字）：2 轮约 2,508 字 ≈ **1,254 token**。
——注意这个数字里**没有** 1,396 字的 `_MASTER_TEMPLATE`、也**没有** 859 字的
历史；那些是单次生图 API 的东西，不该算到这一层头上。

循环上限 10 轮（用户 2026-10-06 从 5 调到 10）。轮次用尽或全程没查到东西时
返回 ""，调用方按「没有资料」继续走原来的路——**搜索失败不该让生图整个失败**。

两个工具（用户 2026-10-06 拍板「把网页搜索加上」）：

- `search_tags`：本地 Danbooru 标签库（32.8 万条）。**主力**，查角色/作品/标签。
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
# 资料硬上限：用户要求 500~1000 字。给一点余量，超了从尾巴截。
MAX_DOC_CHARS = 1200
# 单次工具结果上限。web_search 一次能吐上千字，10 轮累积会把上下文撑爆；
# 工具结果要一直留在 messages 里，所以这里统一收口。
MAX_TOOL_CHARS = 1500
# 单次 LLM 调用超时。搜索是生图链路的前置，不能拖太久。
CALL_TIMEOUT = 30

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
    "参数 query：中文名（银狼、初音未来、胡桃）或英文 tag 名（silver_wolf）。\n"
    "返回候选列表，每行是「tag 中文名 cat=类别」（4=角色 3=作品 0=通用 1=画师）。\n"
    "**这是你的主力工具，先用它。**\n"
    "\n"
    "【工具 2】web_search —— 联网搜索。\n"
    "参数 query（搜索词）、max_results（条数，默认 5）。\n"
    "**只在标签库查不到、或候选太多拿不准时才用它**（比如确认某个角色的作品出处）。\n"
    "它一次返回几百上千字，比查库贵得多，**不要拿它查普通描述词或反复搜同一件事**。\n"
    "\n"
    "要查就输出一行：[[TOOL:search_tags]]{\"query\": \"要查的词\"}\n"
    "要联网就输出：[[TOOL:web_search]]{\"query\": \"搜索词\"}\n"
    "可以一次输出多行，查多个词。\n"
    "\n"
    "【你要做的事】\n"
    "1. 从用户的需求里找出**需要查库的东西**：角色名、作品名、拿不准的标签。\n"
    "   普通描述词（女孩、微笑、长发）不用查，你自己知道对应的英文标签。\n"
    "2. **第一轮必须先调工具**。不许凭记忆直接写资料——同一个中文名可能有"
    "几十个候选（初音未来 111 个、胡桃 18 个），凭印象写的角色 tag 大概率是错的。\n"
    "3. 工具结果够了就整理成一份**资料**；不够就继续查，**同一个词不要重复查**。\n"
    "\n"
    "【资料的写法】\n"
    "- 角色写成：中文名 → tag 名（作品名）。同一个中文名有多个候选时，"
    "挑最可能的那一个放前面，其余列在后面备选，让下游自己判断。\n"
    "- 其他标签写成：中文 → tag 名。\n"
    "- **只写工具结果里真实出现过的 tag，一个字都不许编。** 工具说没查到就写没查到。\n"
    "- 控制在 1000 字以内。不要写提示词、不要写画法建议、不要复述用户的话。\n"
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
    if not query:
        return "错误: 缺少 query 参数。"
    try:
        if name == "web_search":
            n = args.get("max_results") or 5
            try:
                n = max(1, min(int(n), 10))      # 上限 10 条，防一次吐太多
            except Exception:
                n = 5
            out = fn(query, n)
        else:
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
    """查标签库，返回一份资料文本；查不到东西返回 ""。

    参数 need：用户这一轮的绘图需求（原话）。**不传历史**——指代由调用方
    在传进来之前就解决掉（见模块说明）。

    返回值直接当资料用，调用方负责塞进生图 API 的 prompt。任何异常都吞掉
    返回 ""：搜索是增强，不是主链路，它挂了不该让生图也挂。
    """
    text = (need or "").strip()
    if not text:
        return ""

    messages = [{"role": "user", "content": SEARCH_PROMPT + "\n【用户的需求】\n" + text}]
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
            if doc:
                last_doc = doc
            log.info("[search] %d 轮结束，资料 %d 字 [%s]",
                     round_no, len(doc), _sk())
            return doc

        results = []
        for c in calls:
            r = _run_tool(c)
            results.append("【%s】\n%s" % (c.get("name") or "?", r))
            log.info("[search] 第 %d 轮调用 %s，返回 %d 字 [%s]",
                     round_no, c.get("name") or "?", len(r), _sk())
        last_results = "\n\n".join(results)

        messages.append({"role": "assistant", "content": reply})
        messages.append({
            "role": "user",
            "content": ("【工具结果】\n" + last_results
                        + "\n\n还没查够就继续输出工具调用；"
                          "查够了就直接输出资料（1000 字以内）。"),
        })

    log.warning("[search] 达到 %d 轮上限，用最后一轮的工具结果 [%s]",
                max_rounds, _sk())
    # 全程都在调工具 = 模型没收敛。至少把最后一轮的工具结果交出去，别白烧
    # （2026-10-06 实录：5 轮 40+ 次调用、6,199 miss token，最后返回空串）。
    return last_doc or _strip(last_results)

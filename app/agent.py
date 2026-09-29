"""
Agent Loop
核心循环：LLM -> 工具调用 -> 执行 -> 结果塞回 -> 重复
"""

import hashlib
import json
import logging
import re
from app.cancel import is_cancelled
from app.llm import (call_llm_stream, candidates, current_stream_meta,
                     current_usage, get_effective_config)
from app import agents as agent_store
from app.memory import trim_history
from app.tools import execute_tool

log = logging.getLogger("agent")


# 统一工具块正则：容忍 "TOOL:" 前后/内部的空白，也容忍省略前缀的简写
#   [[TOOL:name]]   [[TOOL: name]]   [[TOOL:NAME]]   [[name]]
# 简写形式（无 TOOL: 前缀）只有在名字命中注册表时才被认作工具调用，
# 避免把正文里正常的 [[xxx]] 标记误判成工具。
#
# 开头的方括号也容忍被写成尖括号：实测（2026-09-29，233的粉丝群）模型偶发把
# `[[TOOL:x]]{...}[[/TOOL]]` 吐成 `<TOOL:x]]{...}[[/TOOL]]`，闭合那半是对的、
# 只有开头错。整块因为不匹配被当正文发到群里，群友直接看到工具源码。
# 带 TOOL: 前缀时尖括号照收；无前缀的简写仍要求命中注册表，所以
# 「<a]]」这类正文不会因为放宽开头就被误判成工具。
TOOL_TAG_RE = re.compile(r'(?:\[\[|<)\s*(?:(TOOL)\s*:\s*)?(\w+)\s*\]\]',
                         re.IGNORECASE)

# 模型偶尔还会把工具块套进别的框架的壳里（<tool_call>…</tool_call>）。
# 壳不是工具的一部分，剥块时一起吃掉，否则它会留在正文里被发出去。
# 同样的实测来源：233的粉丝群里 8 次畸形里全都带这个前缀。
_WRAPPER_OPEN_RE = re.compile(r'<\s*tool_calls?\s*>\s*$', re.IGNORECASE)
_WRAPPER_CLOSE_RE = re.compile(r'^\s*<\s*/\s*tool_calls?\s*>', re.IGNORECASE)

# ── Anthropic 式 XML 工具调用（2026-09-29 补）────────────────────
# 走 Anthropic 兼容端点的模型（mimo / doubao）会把工具调用吐成**自己的原生
# 格式**，而不是本项目的 `[[TOOL:name]]{json}[[/TOOL]]`：
#
#     <tool_call><function=generate_image><parameter=prompt>…</parameter>
#     <parameter=skill>anima</parameter></function></tool_call>
#
# 通篇没有 `]]`，TOOL_TAG_RE 一个字符都匹配不上 —— 于是**工具根本没执行、
# 整段 XML 连着提示词被当正文原样发进聊天**。
#
# 实测（09-29，233的粉丝群 1103174141 + 清酒瓶子的私聊 546587874）：日志里
# 84 条「发送 -> …<tool_call>」，其中 83 条出自 mimo-v2.6-flash，而且**每一条
# 所在的那一轮 `工具=-`**。用户连问三次「检查工具调用格式是否正确」，模型还给了
# 一套错的解释（「格式没问题」「是被内容审核拦了」）——所以它不只是不出图，
# 还在拿假原因骗人。
#
# 同时收两种写法：模型实际吐的 `<function=NAME>`，和 Anthropic 文档里的
# `<invoke name="NAME">`；参数同理收 `<parameter=KEY>` 与 `<parameter name="KEY">`。
# 值那里允许残留一个引号（实测出现过 `<parameter=nums">50</parameter>`）。
_XML_FUNC_RE = re.compile(
    r'<\s*(?:function\s*=\s*["\']?|invoke\s+name\s*=\s*["\'])\s*(\w+)',
    re.IGNORECASE)
_XML_FUNC_END_RE = re.compile(r'<\s*/\s*(?:function|invoke)\s*>', re.IGNORECASE)
_XML_PARAM_RE = re.compile(
    r'<\s*parameter\s*(?:=\s*["\']?|name\s*=\s*["\'])\s*(\w+)\s*["\']?\s*>'
    r'(.*?)<\s*/\s*parameter\s*>',
    re.DOTALL | re.IGNORECASE)


def _known_tool_names():
    """已注册工具名集合（懒加载，避免循环导入）"""
    global _TOOL_NAMES
    if _TOOL_NAMES is None:
        try:
            from app.tools.registry import TOOLS
            _TOOL_NAMES = {t["name"] for t in TOOLS}
        except Exception:
            _TOOL_NAMES = set()
    return _TOOL_NAMES


_TOOL_NAMES = None


def _iter_tool_tags(text):
    """产出 (match, name)，已过滤掉不合法的简写、并归一化大小写"""
    known = _known_tool_names()
    lower_map = {n.lower(): n for n in known}
    for m in TOOL_TAG_RE.finditer(text or ""):
        has_prefix = m.group(1) is not None
        name = m.group(2)
        # 关闭标签 [[/TOOL]] 已被 \w+ 排除（'/' 不是 \w）
        if name in known:
            yield m, name
            continue
        if name.lower() in lower_map:
            # 大小写不一致（如 [[TOOL:LIST_SKILLS]]）→ 归一到注册表名
            yield m, lower_map[name.lower()]
            continue
        # 名字没命中注册表：带 TOOL: 前缀的保留（让执行层报"工具不存在"），
        # 无前缀的当作正文标记忽略，避免误伤
        if has_prefix:
            yield m, name


def _iter_xml_tool_calls(text):
    """产出 (start, end, name, args)：Anthropic 式 XML 工具调用的位置与内容。

    start/end 是**整块**（含外层 <tool_call> 壳）在 text 里的下标，供剥块用；
    name 同样过一遍注册表归一（大小写不一致时归一到注册表名，和 _iter_tool_tags
    保持同一套规矩）。
    """
    low = (text or "").lower()
    if "<function" not in low and "<invoke" not in low:
        return
    known = _known_tool_names()
    lower_map = {n.lower(): n for n in known}
    for m in _XML_FUNC_RE.finditer(text):
        raw = m.group(1)
        name = raw if raw in known else lower_map.get(raw.lower(), raw)
        # 函数体：到 </function> / </invoke>，或下一个函数标签，或文末
        body_end = len(text)
        end_tag = _XML_FUNC_END_RE.search(text, m.end())
        nxt = _XML_FUNC_RE.search(text, m.end())
        if end_tag:
            body_end = end_tag.start()
        if nxt and nxt.start() < body_end:
            body_end = nxt.start()
        args = {}
        for pm in _XML_PARAM_RE.finditer(text, m.end(), body_end):
            args[pm.group(1)] = pm.group(2).strip()
        # 整块范围：往前吃掉紧贴的 <tool_call> 壳，往后吃掉 </function></tool_call>
        start = m.start()
        open_m = _WRAPPER_OPEN_RE.search(text[:start])
        if open_m:
            start = open_m.start()
        end = body_end
        if end < len(text):
            end_m = _XML_FUNC_END_RE.match(text, end)
            if end_m:
                end = end_m.end()
        close_m = _WRAPPER_CLOSE_RE.match(text[end:])
        if close_m:
            end += close_m.end()
        yield start, end, name, args


def parse_tool_calls(text):
    """从 LLM 回复中解析所有工具调用（支持一次多个）
    兼容格式：
    - [[TOOL:name]]{...}[[/TOOL]]   （带关闭标签）
    - [[TOOL:name]]{...}            （省略关闭标签，匹配到行尾/下一个工具）
    - [[TOOL: name]] / [[TOOL:NAME]] （容忍空白与大小写）
    - [[name]]                       （小模型常见的省略前缀写法，需命中注册表）
    - <function=name><parameter=k>v</parameter></function>   （Anthropic 式 XML）
    """
    found = []
    for m, name in _iter_tool_tags(text):
        rest = text[m.end():]
        # 尝试标准 JSON 参数（{...}）
        args_str = ""
        if rest.lstrip().startswith("{"):
            # 花括号配对，考虑 JSON 里的嵌套
            brace_match = rest.lstrip()
            depth = 0
            end_idx = None
            for idx, ch in enumerate(brace_match):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end_idx = idx + 1
                        break
            if end_idx is not None:
                args_str = brace_match[:end_idx]
        if not args_str:
            # 无参数或参数非 JSON
            args_str = "{}"
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {"raw": args_str}
        found.append((m.start(), {"name": name, "args": args}))
    # 两族混在一段回复里时，按出现位置排，保持模型的原意顺序
    for start, _end, name, args in _iter_xml_tool_calls(text):
        found.append((start, {"name": name, "args": args}))
    found.sort(key=lambda p: p[0])
    return [call for _pos, call in found]


def _strip_bracket_tool_blocks(text):
    """去掉 [[TOOL:name]]{...}[[/TOOL]] 形式的工具块（含参数），只保留正文
    通过花括号配对精确界定每个 [[TOOL:name]] JSON 参数的结束位置，
    这样能正确处理工具块后紧跟正文的情况。
    与 parse_tool_calls 使用同一套识别规则（含简写 [[name]]）。"""
    if not text:
        return text
    result_parts = []
    pos = 0  # 当前已扫描位置（属于正文）
    for m, _name in _iter_tool_tags(text):
        # 保留工具块之前的正文；顺手吃掉紧贴着的 <tool_call> 壳
        result_parts.append(_WRAPPER_OPEN_RE.sub("", text[pos:m.start()]))
        # 找到工具块结束位置（含参数和关闭标签）
        block_end = m.end()
        rest = text[block_end:]
        lstrip_rest = rest.lstrip()
        offset = len(rest) - len(lstrip_rest)  # 前导空白
        if lstrip_rest.startswith("{"):
            depth = 0
            for idx, ch in enumerate(lstrip_rest):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        block_end = block_end + offset + idx + 1
                        break
        # 跳过关闭标签 [[/TOOL]]
        after = text[block_end:].lstrip()
        if after.startswith("[[/TOOL]]"):
            block_end = block_end + (len(text[block_end:]) - len(after)) + len("[[/TOOL]]")
        # 再吃掉紧随其后的 </tool_call> 壳
        cm = _WRAPPER_CLOSE_RE.match(text[block_end:])
        if cm:
            block_end += cm.end()
        pos = block_end
    # 末尾正文
    result_parts.append(text[pos:])
    return "".join(result_parts)


def _strip_xml_tool_blocks(text):
    """剥掉 Anthropic 式 XML 工具块（含外层 <tool_call> 壳）。"""
    spans = [(s, e) for s, e, _n, _a in _iter_xml_tool_calls(text)]
    if not spans:
        return text
    parts = []
    pos = 0
    for start, end in spans:
        if start < pos:            # 与前一块重叠，跳过
            continue
        parts.append(text[pos:start])
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _strip_tool_blocks(text):
    """去掉回复中的所有工具调用块（含参数），只保留正文。

    两族依次过：`[[TOOL:...]]` 家族沿用原来那套精确界定，Anthropic 式 XML
    家族（见 _iter_xml_tool_calls）另走一遍。分两遍而不是揉在一起，是为了
    完全不改动已经在跑的方括号逻辑。"""
    return _strip_xml_tool_blocks(_strip_bracket_tool_blocks(text))


# ─── 生图空头承诺守卫 ────────────────────────────────
# 小模型（尤其 flash 档）常把「画个图」当成纯聊天：回一句「画着呢 等着收图」
# 就交差，**根本没发 [[TOOL:generate_image]]**。群里于是永远等不到图——这比
# 直接说「画不了」还糟，因为对方真的在等。
#
# 实测（2026-09-29 群 1103174141）：
#   233：那画一个呗，  →  胡桃桃：画着呢 等着收图   （无工具调用、无图）
#
# 光在 prompt 里写规矩拦不住小模型（同 DISABLED_IMAGE_SKILLS 那条教训：
# 「提示词里写了」≠「模型会遵守」），所以这里补一道**代码级**兜底。
#
# 判据刻意保守：只认「正在进行 / 已完成」的承诺词，且句子里出现「画不了 /
# 不画」等明确拒收词时一律不算——那是老实回话，不能触发重来。
_IMAGE_PROMISE_RE = re.compile(
    r"画着呢|在画了|正在画|画上了|画起来了|重画中|马上画|这就画|"
    r"开始画|我去画|帮你画|给你画|画好了|画完了|"
    r"等着收图|等收图|图在路上了|图马上到|排队画|"
    # ── 「跑」系（2026-09-29 补）──────────────────────────────────────
    # 群里把生图叫「跑图」，模型跟着说「跑着了 / 跑上了 / 跑完了」。原词表
    # 只有「画」系，这些一个都不认——实测当晚群 1041079621 三条假回执
    # （「这张也出了」「刚连着发了三张」「三张都发群里了」）全部漏过守卫。
    # 故意不收裸的「正在跑」：模型老实汇报「ComfyUI 正在跑别的图」也会中招。
    r"跑着呢|跑着了|在跑了|跑上了|跑起来了|重跑中|马上跑|这就跑|"
    r"开始跑|我去跑|帮你跑|给你跑|跑好了|跑完了|排队跑|"
    # ── 完成态（2026-09-29 补）───────────────────────────────────────
    # 「断言图已经存在 / 已经发出去」比「正在画」更容易漏，也更容易骗到人。
    # 不收裸的「出了」——「出了点问题」会误伤。
    r"出图|也出了|都出了|这单出了|已经出了|刚出了|"
    r"发群里|发出去了|发过去了|发了.{0,2}张"
)
_IMAGE_REFUSAL_RE = re.compile(
    r"画不了|不画|画不出|没法画|不能画|画不动|别画|不给画|画啥|"
    # 老实回话：说画不了 / 没出图 / 超时 / 失败，一律不算承诺，不能触发重来。
    # 「没出图」尤其关键——失败回执「超过 180 秒没出图」里有「出图」二字。
    r"跑不了|不跑了|别跑|不发|别发|发不出|没出图|没画|超时|失败"
)

_IMAGE_CLAIM_NUDGE = (
    "系统：你刚才说要画图，但**这一轮没有真正调用 generate_image**，"
    "所以那张图根本不存在，群里也收不到。现在二选一，别再空口承诺：\n"
    "① 真要画 → 这一轮必须输出工具块，单独一行、一字不差：\n"
    "[[TOOL:generate_image]]{\"prompt\": \"<英文标签串>\"}[[/TOOL]]\n"
    "② 画不了（本群关了 / ComfyUI 掉线 / 对方要的内容不能画）→ 就照实说，"
    "别再说「画着呢」「跑着了」「已经出了」「刚发群里了」这种话。"
)


def _looks_like_image_promise(text):
    """这句正文像不像「我在画 / 画好了」的空头承诺。

    带「画不了 / 不画」的句子一律不算——那是拒收，不是承诺。
    """
    if not text:
        return False
    if _IMAGE_REFUSAL_RE.search(text):
        return False
    return bool(_IMAGE_PROMISE_RE.search(text))


def _history_for_llm(history):
    """
    将内部 history 转换为 LLM 可识别的格式：
    - tool_result -> user role + "[工具结果]" 前缀
    - 其他保持不变
    """
    result = []
    for msg in history:
        if msg["role"] == "tool_result":
            result.append({
                "role": "user",
                "content": "[工具结果] " + msg["content"]
            })
        else:
            result.append(msg)
    return result


def _attach_images(llm_history, images):
    """把本轮的用户消息升级为多模态（text + 若干 image_url），供带图对话使用。

    只在第 1 轮调用。一张压缩后的图 base64 有几十万字符：每轮重复下发既白烧
    输入 token，又让前缀缓存每轮都 miss；而模型看图只需一次——后续轮次能从
    它自己写下的分析、以及工具返回结果里获得信息。

    实现上有两条不能碰的红线：
    1. **只替换列表元素，绝不修改元素内部的 dict**。_history_for_llm 返回的
       字典对象与 history 里的是同一批引用，改内部字段会把这个 base64 一起
       落进会话文件——那正是"图片不入档"要避免的事。
    2. 倒序找**第一条不是工具结果的** user 消息。本轮用户消息一定排在它自己
       的 pre_tool_results（手打 [[TOOL:]] 的结果）之前，倒序找别把工具结果
       当成用户消息升级了。
    """
    for i in range(len(llm_history) - 1, -1, -1):
        msg = llm_history[i]
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or content.startswith("[工具结果] "):
            continue
        parts = [{"type": "text", "text": content}]
        for data_url in images:
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        llm_history[i] = {"role": "user", "content": parts}
        return True
    return False


# ─── 图片预处理（给没有视觉能力的模型）───────────────

# 识图失败时给模型的说明。必须明确告诉它"你看不到这张图"，否则它会假装
# 看见了，顺着用户的"你看这个报错"编出一段分析——那比直接说不看更糟。
_VISION_FAIL_NOTE = "（这张图片识别失败，你看不到它的内容，请如实说明）"

# 单张识图结果写进历史的字数上限（2026-09-30）。
#
# 为什么要截：识图文字块是**写回 history 的**（图片本体不进，但这段文字进），
# 实测在群里占到历史总字数的 28%（19 条共 13,501 字 / 47,776 字，单条 300~1,712
# 字）。它就是群会话顶破 token 预算的头号推手——顶破之后压缩开始每轮触发，
# 而压缩会把摘要插在系统头正后面，一次作废整段前缀缓存（签名 `命中 6144 / 26xxx`）。
#
# 截在 400 是有依据的：识图 prompt 要求它先讲主体，实测前 300 字已把角色/发色/
# 瞳色/姿势说完，再往后的多是背景纹理与修辞。19 条截到 400 字后合计约 7,600 字，
# 历史降约 12%，估算从 24,879 掉回 24,000 预算以内 → 压缩根本不触发。
#
# **⚠️ 只在写进 history 的这一份上截**：当轮发给模型的也是同一份（图片本体不进
# 历史，模型只有这一次机会看这段文字），所以截断会同时影响当轮。这是有意的——
# 不截的话省不下来；真需要细节时用户会再问一句。
_VISION_NOTE_MAX_CHARS = 400

# 「识图模型其实没看到图」的判据（2026-09-30）。
#
# 火山/DeepSeek 的视觉模型偶发返回一段**看起来像回答、其实在说自己看不到**的
# 文字，例如实测两句：
#   「我无法直接查看或读取你这条消息里的图片内容（当前没有可解析的图片文件/链接）…」
#   「我无法直接看到图片，但根据你提供的引用信息和需求，我可以帮你梳理关键点…」
# 后者更坏：它接着**自行编造**了一段画面描述和实施计划，下游照样据此生图。
#
# 这类返回**不会抛异常**，所以 _VISION_FAIL_NOTE 那条兜底完全接不住。命中的一律
# 换成 _VISION_FAIL_NOTE——宁可让模型明说「看不到」，也不要它拿着编的内容当真。
#
# 正则只匹配「模型自称看不到」，不匹配描述里出现的"无法"（比如描述画面中的文字）。
_VISION_BLIND_RE = re.compile(
    r"我(?:无法|不能|没法)(?:直接)?(?:查看|看到|读取|识别|访问)"
    r"|无法查看或读取"
    r"|没有可解析的图片"
    r"|请(?:你)?重新(?:上传|发送)(?:一下)?(?:这张)?图片")

# 识图结果前面的说明。带发送者时把名字写进去——群里一轮可能混着好几张图，
# 不写谁发的，模型只能猜，猜错就是把 A 发的图安到 B 头上（「关系网乱」的
# 头号来源）。发送者未知时退回「用户发来」，宁可笼统也不要乱安人。
_VISION_HEAD = "[用户发来图片，以下是识别结果]"


def _vision_head(owners):
    """按发送者渲染识图块的头。owners 为空/全空时退回笼统写法。"""
    known = [o for o in (owners or []) if o]
    if not known:
        return _VISION_HEAD
    uniq = []
    for o in known:
        if o not in uniq:
            uniq.append(o)
    if len(uniq) == 1:
        return "[%s 发来图片，以下是识别结果]" % uniq[0]
    return "[%s 发来图片，以下是识别结果]" % "、".join(uniq)


def _vision_notes(images, owners=None, question=""):
    """逐张识图，返回可拼进用户输入的文本行。失败的项也占一行。

    owners 与 images 一一对应（可短可缺）；多张图时每张标出是谁发的。

    question 是本轮用户的原话，一并送进识图 prompt（见 vision.build_prompt）。
    不带的话识图只按通用指令读图，下游文本模型就只能拿到一段泛泛的描述，
    得自己猜「用户到底想问什么」。

    三条后处理都放在这一处（返回值同时是"当轮发给模型的"和"写回 history 的"）：
    1. **失败兜底**：describe 抛异常 → _VISION_FAIL_NOTE。
    2. **「成功返回但没看图」兜底**：见 _VISION_BLIND_RE。这类返回不抛异常，
       没有它就接不住，模型会拿一段编造的画面描述当真。
    3. **截断到 _VISION_NOTE_MAX_CHARS**：识图文字块是历史里最大的一块可压缩
       脂肪（实测占群历史 28%），不截就会把会话顶破预算、逼出每轮压缩。
    """
    from app.vision import build_prompt, describe

    notes = []
    total = len(images)
    for i, data_url in enumerate(images, 1):
        try:
            text = describe(data_url,
                            prompt=build_prompt(question, i, total)).strip()
        except Exception as exc:
            log.warning("识图失败（第 %d/%d 张）：%s", i, total, exc)
            text = ""
        # 没看图却说了一堆 —— 换掉。这段一旦写进 history 就是永久占位，
        # 而且下游会把它当事实（实测它编出过「原图是粉色小熊连体泳衣」）。
        if text and _VISION_BLIND_RE.search(text):
            log.warning("识图没看到图（第 %d/%d 张），改判失败：%r",
                        i, total, text[:60])
            text = ""
        if len(text) > _VISION_NOTE_MAX_CHARS:
            text = text[:_VISION_NOTE_MAX_CHARS].rstrip() + "…（描述已截断）"
        body = text or _VISION_FAIL_NOTE
        if total > 1:
            owner = (owners or [])[i - 1] if i - 1 < len(owners or []) else ""
            head = "【第 %d 张" % i
            head += "（%s 发的）】" % owner if owner else "】"
            notes.append(head + body)
        else:
            notes.append(body)
    return notes


def _with_vision(user_input, images, owners=None):
    """把识图结果并入用户输入文本（图片本体不进 history，只留这段文字）。

    加这段头是必要的：识别出来的文字混在用户的话里，多轮之后模型分不清
    哪些是"用户说的"、哪些是"从图里读出来的"，容易把图里的报错当成用户
    的诉求本身。

    user_input 同时作为识图 prompt 里的「用户的需求」传下去——识图看得见
    需求，描述才有针对性，否则它只能对着一张图泛泛而谈。
    """
    notes = _vision_notes(images, owners, question=user_input)
    if not notes:
        return user_input
    block = _vision_head(owners) + "\n" + "\n".join(notes)
    text = (user_input or "").strip()
    return (text + "\n\n" + block) if text else block


def _as_image_list(image):
    """把 image 参数归一化成列表。单张 data URL 与列表都接受。"""
    if not image:
        return []
    if isinstance(image, str):
        return [image]
    if isinstance(image, (list, tuple)):
        return [x for x in image if isinstance(x, str) and x]
    return []


def _error_reply(exc):
    """把异常翻成给用户看的一句话。

    额度耗尽和限流都长着 429 的脸，处理方式却相反：前者只能换模型，后者等
    几秒就好。混成一句"出错了"的话，用户只能去翻代码和日志——而这台机器上
    火山免费额度确实有用完的那一天，那是唯一需要动手切换的场景。
    """
    text = str(exc)
    if "QuotaExceeded" in text or "FreeQuota" in text or "额度" in text:
        return ("⚠️ 模型额度已用尽（免费额度已消耗完）。请打开管理页 "
                "/admin 把这个 agent 的模型切换成其它 provider 后重试。")
    if "RateLimit" in text or "rate_limit" in text:
        return "⚠️ 模型当前被限流，稍等几秒再发一次即可，不必切换模型。"
    return "❌ 执行出错：" + text


def _status_message(history, extra_context=None):
    """构造状态栏消息，追加在请求消息数组的末尾。

    动态内容只出现在尾部：状态栏每轮变化只 miss 它自己那几十 token，
    前面的稳定 system + 全部历史（只追加）都能命中 prefix cache。
    绝不把状态栏放在前部——那会让之后所有历史按原价重算。

    extra_context: 只在这一轮生效的补充上下文（QQ 侧传「群里最近的对话」）。
    挂在状态栏同一条消息里，出流即弃，**不写回 history**。写回去的话，这类
    内容每轮都要重新注入一遍，十几轮下来历史里堆着十几份重复的背景，既占
    预算、又让摘要越压越浑。
    """
    from app.agent_prompt import build_status_bar

    last_tool = "none"
    for msg in reversed(history):
        if msg.get("role") == "tool_result":
            last_tool = msg.get("tool_name", "none")
            break

    msg_count = len([m for m in history if m.get("role") != "system"])
    status = build_status_bar(message_count=msg_count, last_tool=last_tool)
    # 空串和纯空白都不拼——否则会多出一段空行，白占位置还让前缀比对失准
    extra = (extra_context or "").strip()
    content = (extra + "\n\n" + status) if extra else status
    return {"role": "system", "content": content}


def tail_tokens(history, extra_context=None):
    """估算尾部那条状态栏（含 extra_context）占多少 token。

    给 memory.save_history 的 reserve 用。这条消息每轮都拼进 prompt、每轮都
    按未命中计费，却从不写回 history，所以本地估算（estimate_messages）看不见
    它——不预留的话，压缩后真实 prompt 会比预算高出一整条尾巴。详见
    memory.trim_window 的 reserve 参数。
    """
    from app.memory import estimate_messages

    return estimate_messages([_status_message(history, extra_context)])


# 用户手动中断后写进历史的一条说明。必须留——否则下一轮模型看到自己那条半截
# 回复，会以为话说完了，容易顺着一个已经作废的前提继续往下讲。用 tool_result
# 承载：它不是用户说的话，而是「这一轮被系统中止了」这个事实。
_ABORT_NOTE = "⚠️ 用户手动中断了上一条回复，其内容可能不完整，请以用户的新指示为准。"


def run_agent_stream(user_input, history, provider=None, model=None, pre_tool_results=None,
                     agent_id=None, image=None, cancel_event=None, extra_context=None,
                     image_owners=None, session_key=None):
    """
    Agent Loop: 生成器版本，逐事件返回
    事件类型: user / assistant / tool_call / tool_result / aborted

    契约：会话消息类事件（user / assistant / tool_result）出流时，那条消息
    已经写进 history。调用方以「事件出流」作为落盘时机，所以这几处一律
    append 在前、yield 在后，不能反过来。reasoning 与 tool_call 不写历史
    （思考内容只出不进；工具调用的结果由随后的 tool_result 承载），不在此列。
    aborted 同样遵守：它出流时那条中断说明已经写进 history。

    改进：
    1. 一次处理多个工具调用
    2. turn 上限 MAX_TURNS 防止无限循环
    3. 异常捕获，保证至少返回回复
    4. provider/model 透传：支持 web 端动态切换模型
    pre_tool_results: 可选，用户输入里直接带的 [[TOOL:...]] 已执行完的结果，
      在进入 LLM 循环前先注入历史，让 LLM 一开始就能看到这些工具结果。
    agent_id: 哪个 agent 在跑。影响两件事——上下文压缩的冷却水位按 agent 分开记；
      工具白名单在此处兜底拦截（system prompt 里不列出是第一道，这里是第二道，
      模型即便硬写出白名单外的工具也不会被执行）。
    image: 可选，本轮随消息一起下发的图片。可以是单个 dataURL 字符串，也可以
      是 dataURL 列表（QQ 一条消息带多张图）。去向由**本轮实际生效的模型**
      决定：有视觉能力的按多模态下发（见 _attach_images，只在第 1 轮），没有
      视觉能力的先过识图转成文字再拼进用户输入。两种情况图片本体都不进
      history——进档的是文本（占位说明或识别结果）。
    cancel_event: 可选，threading.Event，置位表示用户按了「停止」。循环在三个
      检查点收工：① 每轮开头 ② 模型输出结束（半截回复绝不拿去解析工具调用——
      残缺的 JSON 会被当成一次合法调用真执行）③ 每个工具执行前。
      **工具一旦跑起来就只能靠工具内部自己检查**，生图那个轮询循环就是干这个的。
      收工走正常流程：落盘 + 出 aborted 事件，不做任何强制中断，进程和会话
      文件都保持干净。
    image_owners: 可选，与 image 一一对应的发送者昵称列表（QQ 侧传来）。
      只影响没有视觉能力时的识图文字块——把「谁发的图」写进去，模型才知道
      这张图该挂在谁头上。数量对不上（短了或空）时缺的那些退成笼统写法。
    extra_context: 可选，只在这一轮生效的补充上下文（QQ 侧传「群里最近的
      对话」）。挂在末尾那条状态栏消息里、**不写回 history**，所以每轮现取
      现用，不会在历史里重复堆积。详见 _status_message。
    session_key: 可选，这条会话线在 QQ 侧的 key（`private_<QQ>` / `group_<群号>`）。
      **只给上下文压缩用**：压缩掉的老轮次对群会话要顺带转交长期记忆（不然
      群里「以前聊过什么」会越来越薄），而长期记忆是按群存的，得先知道群号。
      网页端不传 → None → 行为与从前一致（不做转交）。
    """
    from app.config import MAX_TURNS, provider_vision

    # agent 级模型：调用方没显式指定时，用 agent.json 里配的 provider/model。
    # 放在这一处而不是各个调用点上，是为了让网页端 /api/chat 与 QQ 适配层
    # 都自动吃到这份配置——它们都只调 run_agent_stream，不必各写一遍。
    # 都没配则维持原样（传 None 下去，由 llm 层回退到 .env 的全局默认），
    # 所以老 agent.json 没这两个字段时行为与从前完全一致。
    _acfg = agent_store.agent_config(agent_id)
    if not provider and not model:
        provider = _acfg["provider"] or None
        model = _acfg["model"] or None
    # 上下文预算同理，0 → 传 None，由 memory 回退到 .env 的 CONTEXT_BUDGET。
    # 这样「QQ 群聊省钱、网页端深度任务放开」可以按 agent 各配一个数。
    context_budget = _acfg["context_budget"] or None

    # ─── 图片路由 ────────────────────────────────────
    # 判据是**本轮实际生效的模型**，不是 agent 配置的 provider：网页端的模型
    # 面板能在请求级覆盖模型，降级到别处时配置和实际也会不一致，按配置判会判错。
    #
    # 有视觉 → 图片按多模态原样下发；没有 → 先识图转成文字拼进正文。后者不是
    # "效果更好"，是必需的：火山的纯文本模型收到 base64 不会报错，而是整条
    # 请求挂死（实测 ReadTimeout 卡满 180 秒）。
    images = _as_image_list(image)
    attach_mode = False
    if images:
        eff = get_effective_config(provider, model)
        if provider_vision(eff["provider"], eff["model"]):
            attach_mode = True
        else:
            user_input = _with_vision(user_input, images, image_owners)

    history.append({"role": "user", "content": user_input})
    yield {"type": "user", "content": user_input}

    for tc in (pre_tool_results or []):
        history.append({
            "role": "tool_result",
            "content": tc["result"],
            "tool_name": tc["name"],
        })
        yield {"type": "tool_result", "name": tc["name"], "result": tc["result"]}

    turn_count = 0
    aborted = False
    # 空回复重试计数：每次 run_agent_stream（一条用户消息）最多换模型重试一次。
    empty_retries = 0
    # 生图空头承诺守卫的状态（见 _looks_like_image_promise 的注释）：
    # image_tool_used 记录本轮有没有真调过 generate_image；image_nudged 保证
    # 「退回重来」最多一次，不会无限拦。
    image_tool_used = False
    image_nudged = False
    # 该 agent 有没有生图工具——没有就根本不该触发这道守卫（写作 agent 说
    # 「画着呢」是另一回事，不归这里管）。
    can_generate_image = agent_store.allows_tool(agent_id, "generate_image")
    # 上一轮 LLM 调用的真实用量，供这一轮判断该不该压缩。
    # 初值给空 dict 而不是 None：None 会让 memory 回退去读「当前线程最近一次」，
    # 而 QQ 场景下线程是跨会话复用的，读到的可能是别的群刚留下的数。空 dict
    # 表示「还不知道」，于是第一轮不压缩——首轮本来也没什么可压的。
    last_usage = {}
    while turn_count < MAX_TURNS:
        # 检查点①：轮次边界。拦住"工具连环调用"继续往下走。
        if is_cancelled(cancel_event):
            aborted = True
            break
        turn_count += 1

        try:
            # 裁剪 + 转换为 LLM 格式（tool_result -> user）
            # provider/model 必须显式传：摘要是后台的隐形调用，不传会回退到
            # .env 的全局默认，于是主对话用 agent 配的模型、压缩却打另一家的
            # 额度（实测症状：agent 切到 deepseek 后压缩仍报火山的额度错误）。
            trimmed = trim_history(history, agent_id, usage=last_usage,
                                   budget=context_budget,
                                   provider=provider, model=model,
                                   session_key=session_key)
            # 压缩结果必须**原地写回** history（2026-09-30）。
            # trim_history 返回的是新列表；不写回的话它只活在这一轮的局部变量
            # 里，落盘时写的还是压缩前的胖历史 → 下一轮重新压一遍。实测症状：
            # 群里每个「带工具的回合」都白烧一次 13K~18K token 的**全价**摘要
            # 调用（0% 命中）+ 8 秒，09-29 共 94 次 ≥10K、合计 2.08M miss token。
            # 写回之后压缩只发生一次，后续轮次读回来的就是压好的历史。
            # ⚠️ 必须在 _status_message / tail_tokens 之前写回：那两处读 history
            # 算条数与尾巴，读压缩后的数才对得上本轮真正发出去的东西。
            if trimmed is not history:
                history[:] = trimmed
                trimmed = history
            llm_history = _history_for_llm(trimmed)
            # 带图对话（仅当本轮模型有视觉）：第 1 轮把用户消息升级成多模态，
            # 之后轮次不再带图（图片本体始终不在 history 里，历史中只有文本）
            if attach_mode and turn_count == 1:
                _attach_images(llm_history, images)
            # 状态栏追加在尾部，动态变化不毒化前缀缓存
            llm_history.append(_status_message(history, extra_context))
            # 把这条尾巴的大小记到当前线程，供 llm._record_usage 在 [cache]
            # 行里打出来（`尾巴≈N`）。它是每轮必 miss 的部分，等于命中率的
            # 天花板——不记下来，「这轮为什么这么贵」在日志里看不出来。
            try:
                from app import usage as _usage_stats
                _usage_stats.set_tail(tail_tokens(history, extra_context))
            except Exception:        # 纯展示，绝不能影响主链路
                pass
            # 流式调用：
            # - 思考内容(reasoning)即时下发给前端展示。**只出不进**——绝不写回
            #   history，模型侧要求思考内容不参与后续上下文，写回去还会毒化前缀缓存。
            # - 正文(content)只在本轮累积，等收完再解析工具调用：直接边收边下发
            #   会让 [[TOOL:...]] 标签在页面上闪一下。
            reply_parts = []
            reasoning_chars = 0
            for kind, text in call_llm_stream(llm_history, provider=provider,
                                              model=model, cancel_event=cancel_event):
                if kind == "reasoning":
                    reasoning_chars += len(text)
                    yield {"type": "reasoning", "content": text}
                else:
                    reply_parts.append(text)
            reply = "".join(reply_parts)
            # 记下本轮真实用量，下一轮拿它判断该不该压缩
            last_usage = current_usage()

            # 空回复守卫：流「正常结束」但正文一字未吐（偶发，开思维链的
            # deepseek 系最容易犯——reasoning 花完了正文却没动笔）。不拦截的
            # 话这一轮就静默无声，用户看到的是「收到了消息却不回」。
            # 处理：历史不落空 assistant（根本没 append），换降级链下一家重跑
            # 本轮；再空就放弃（用户点了停止的中断不算空回复，走下面的收尾）。
            if not reply.strip() and not is_cancelled(cancel_event):
                meta = current_stream_meta()
                if empty_retries >= 1:
                    log.warning("[llm-empty] 换模型重试后仍空（reasoning=%d字 finish=%s），本轮放弃",
                                reasoning_chars, meta.get("finish_reason"))
                    break
                empty_retries += 1
                cands = candidates(provider, model)
                if len(cands) > 1:
                    nxt = cands[1]
                    log.warning("[llm-empty] 空回复（reasoning=%d字 finish=%s）→ 换 %s/%s 重试",
                                reasoning_chars, meta.get("finish_reason"), nxt[0], nxt[1])
                    provider, model = nxt
                else:
                    log.warning("[llm-empty] 空回复（reasoning=%d字 finish=%s）→ 链上无备选，原模型重试",
                                reasoning_chars, meta.get("finish_reason"))
                continue

            history.append({"role": "assistant", "content": reply})

            # 检查点②：模型输出期间被中断。此刻 reply 可能是半截话——含没闭合的
            # [[TOOL:...]] 或残缺 JSON，直接收尾，绝不往下解析工具调用（残缺参数
            # 会被当成一次合法调用真执行，那比不做更糟）。
            if is_cancelled(cancel_event):
                aborted = True
                break

            tool_calls = parse_tool_calls(reply)

            # 每次迭代留一行指纹。同一轮里若某段正文重复出现，看输出指纹能分清
            # 是模型自己抄了上文，还是上游网关把同一份响应重放了两遍（输入指纹
            # 也一样才叫重放）。没有这行，光看落盘数据两边都证不了。
            log.info("[turn] #%d 输入=%d条/%d字 输出=%d字 hash=%s 工具=%s",
                     turn_count, len(llm_history),
                     sum(len(m.get("content") or "") for m in llm_history),
                     len(reply),
                     hashlib.md5(reply.encode("utf-8")).hexdigest()[:8],
                     ",".join(c["name"] for c in tool_calls) or "-")

            # 提取回复正文（去掉所有工具块及其参数）
            reply_text = _strip_tool_blocks(reply).strip()

            # ─── 生图空头承诺守卫 ───────────────────────
            # 说要画、但整轮都没调生图工具 → **这句话不发出去**，塞一条系统
            # 提示让它重来一次；一次为限，第二次照发（不能无限拦）。
            if (reply_text and not tool_calls and not image_tool_used
                    and not image_nudged and can_generate_image
                    and _looks_like_image_promise(reply_text)):
                image_nudged = True
                log.warning("[image-claim] 没调工具却声称在画图，退回重来：%r",
                            reply_text[:60])
                history.append({"role": "tool_result",
                                "content": _IMAGE_CLAIM_NUDGE,
                                "tool_name": "generate_image"})
                yield {"type": "tool_result", "name": "generate_image",
                       "result": _IMAGE_CLAIM_NUDGE}
                continue

            if reply_text:
                yield {"type": "assistant", "content": reply_text}

            if not tool_calls:
                # 无工具调用，结束
                break

            # 依次执行所有工具调用
            for tool_call in tool_calls:
                # 检查点③：每个工具执行前。一旦工具开始跑（生图最长可达
                # IMAGE_GEN_TIMEOUT），就只剩工具内部自己能检查了。
                if is_cancelled(cancel_event):
                    aborted = True
                    break
                name = tool_call["name"]
                args = tool_call["args"]
                if name == "generate_image":
                    image_tool_used = True
                yield {"type": "tool_call", "name": name, "args": args}

                # 第二道白名单拦截：prompt 里不列出是「看不见」，这里是「调不动」。
                # 少了这一道，「写作 agent 不能用生图」就只是名义上的隔离。
                if not agent_store.allows_tool(agent_id, name):
                    result = "该工具在当前 agent 不可用：" + name
                else:
                    result = execute_tool(name, args)
                # 先入历史再出流：调用方把「事件出流」当作落盘时机，
                # 顺序反了这条就赶不上落盘（客户端中断时尤其明显）。
                # 用 tool_result role 存储，便于前端区分展示
                history.append({
                    "role": "tool_result",
                    "content": result,
                    "tool_name": name
                })
                yield {"type": "tool_result", "name": name, "result": result}

            if aborted:
                break

            # 循环继续 → 执行完所有工具 → LLM 再思考一次
            if tool_calls:
                continue

            # 无工具 → break
            break

        except Exception as e:
            # 中断期间冒出来的异常（上游连接被关、读取被打断）不是"执行出错"，
            # 报给用户没有意义，按中断收尾。
            if is_cancelled(cancel_event):
                aborted = True
                break
            # 异常捕获：至少返回错误，不要断循环。
            # 文案交给 _error_reply：额度耗尽要明确告诉用户去切模型，而不是
            # 丢一句"出错了"让人去翻日志。
            yield {"type": "assistant", "content": _error_reply(e)}
            break

    if aborted:
        # 中断也走正常收尾：先把说明写进历史再出流（调用方以事件出流为落盘时机）
        history.append({
            "role": "tool_result",
            "content": _ABORT_NOTE,
            "tool_name": "user_cancel",
        })
        yield {"type": "aborted", "content": _ABORT_NOTE}
        return

    if turn_count >= MAX_TURNS:
        yield {"type": "assistant", "content": f"⚠️ 已达到最大轮次限制 ({MAX_TURNS} 轮)，请继续提问。"}

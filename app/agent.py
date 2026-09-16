"""
Agent Loop
核心循环：LLM -> 工具调用 -> 执行 -> 结果塞回 -> 重复
"""

import json
import re
from app.llm import call_llm_stream
from app import agents as agent_store
from app.memory import trim_history
from app.tools import execute_tool


# 统一工具块正则：容忍 "TOOL:" 前后/内部的空白，也容忍省略前缀的简写
#   [[TOOL:name]]   [[TOOL: name]]   [[TOOL:NAME]]   [[name]]
# 简写形式（无 TOOL: 前缀）只有在名字命中注册表时才被认作工具调用，
# 避免把正文里正常的 [[xxx]] 标记误判成工具。
TOOL_TAG_RE = re.compile(r'\[\[\s*(?:(TOOL)\s*:\s*)?(\w+)\s*\]\]', re.IGNORECASE)


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


def parse_tool_calls(text):
    """从 LLM 回复中解析所有工具调用（支持一次多个）
    兼容格式：
    - [[TOOL:name]]{...}[[/TOOL]]   （带关闭标签）
    - [[TOOL:name]]{...}            （省略关闭标签，匹配到行尾/下一个工具）
    - [[TOOL: name]] / [[TOOL:NAME]] （容忍空白与大小写）
    - [[name]]                       （小模型常见的省略前缀写法，需命中注册表）
    """
    result = []
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
        result.append({"name": name, "args": args})
    return result


def _strip_tool_blocks(text):
    """去掉回复中的所有工具调用块（含参数），只保留正文
    通过花括号配对精确界定每个 [[TOOL:name]] JSON 参数的结束位置，
    这样能正确处理工具块后紧跟正文的情况。
    与 parse_tool_calls 使用同一套识别规则（含简写 [[name]]）。"""
    if not text:
        return text
    result_parts = []
    pos = 0  # 当前已扫描位置（属于正文）
    for m, _name in _iter_tool_tags(text):
        # 保留工具块之前的正文
        result_parts.append(text[pos:m.start()])
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
        pos = block_end
    # 末尾正文
    result_parts.append(text[pos:])
    return "".join(result_parts)


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


def _status_message(history):
    """构造状态栏消息，追加在请求消息数组的末尾。

    动态内容只出现在尾部：状态栏每轮变化只 miss 它自己那几十 token，
    前面的稳定 system + 全部历史（只追加）都能命中 prefix cache。
    绝不把状态栏放在前部——那会让之后所有历史按原价重算。
    """
    from app.agent_prompt import build_status_bar

    last_tool = "none"
    for msg in reversed(history):
        if msg.get("role") == "tool_result":
            last_tool = msg.get("tool_name", "none")
            break

    msg_count = len([m for m in history if m.get("role") != "system"])
    return {"role": "system", "content": build_status_bar(message_count=msg_count, last_tool=last_tool)}


def run_agent_stream(user_input, history, provider=None, model=None, pre_tool_results=None,
                     agent_id=None):
    """
    Agent Loop: 生成器版本，逐事件返回
    事件类型: user / assistant / tool_call / tool_result

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
    """
    from app.config import MAX_TURNS

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
    while turn_count < MAX_TURNS:
        turn_count += 1

        try:
            # 裁剪 + 转换为 LLM 格式（tool_result -> user）
            trimmed = trim_history(history, agent_id)
            llm_history = _history_for_llm(trimmed)
            # 状态栏追加在尾部，动态变化不毒化前缀缓存
            llm_history.append(_status_message(history))
            # 流式调用：
            # - 思考内容(reasoning)即时下发给前端展示。**只出不进**——绝不写回
            #   history，模型侧要求思考内容不参与后续上下文，写回去还会毒化前缀缓存。
            # - 正文(content)只在本轮累积，等收完再解析工具调用：直接边收边下发
            #   会让 [[TOOL:...]] 标签在页面上闪一下。
            reply_parts = []
            for kind, text in call_llm_stream(llm_history, provider=provider, model=model):
                if kind == "reasoning":
                    yield {"type": "reasoning", "content": text}
                else:
                    reply_parts.append(text)
            reply = "".join(reply_parts)

            history.append({"role": "assistant", "content": reply})

            tool_calls = parse_tool_calls(reply)

            # 提取回复正文（去掉所有工具块及其参数）
            reply_text = _strip_tool_blocks(reply).strip()

            if reply_text:
                yield {"type": "assistant", "content": reply_text}

            if not tool_calls:
                # 无工具调用，结束
                break

            # 依次执行所有工具调用
            for tool_call in tool_calls:
                name = tool_call["name"]
                args = tool_call["args"]
                yield {"type": "tool_call", "name": name, "args": args}

                # 第二道白名单拦截：prompt 里不列出是「看不见」，这里是「调不动」。
                # 少了这一道，「写作 agent 不能用生图」就只是名义上的隔离。
                if not agent_store.allows_tool(agent_id, name):
                    result = "该工具在当前 agent 不可用：" + name
                else:
                    result = execute_tool(name, args)
                yield {"type": "tool_result", "name": name, "result": result}

                # 用 tool_result role 存储，便于前端区分展示
                history.append({
                    "role": "tool_result",
                    "content": result,
                    "tool_name": name
                })

            # 循环继续 → 执行完所有工具 → LLM 再思考一次
            if tool_calls:
                continue

            # 无工具 → break
            break

        except Exception as e:
            # 异常捕获：至少返回错误，不要断循环
            yield {"type": "assistant", "content": f"❌ 执行出错：{str(e)}"}
            break

    if turn_count >= MAX_TURNS:
        yield {"type": "assistant", "content": f"⚠️ 已达到最大轮次限制 ({MAX_TURNS} 轮)，请继续提问。"}

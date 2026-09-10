"""
Agent Loop
核心循环：LLM -> 工具调用 -> 执行 -> 结果塞回 -> 重复
"""

import json
import re
from app.llm import call_llm
from app.memory import trim_history
from app.tools import execute_tool


def parse_tool_calls(text):
    """从 LLM 回复中解析所有工具调用（支持一次多个）
    兼容两种格式：
    - [[TOOL:name]]{...}[[/TOOL]]   （带关闭标签）
    - [[TOOL:name]]{...}            （省略关闭标签，匹配到行尾/下一个工具）
    """
    result = []
    # 先找所有 [[TOOL:xxx]] 出现的位置
    for m in re.finditer(r'\[\[TOOL:(\w+)\]\]', text):
        name = m.group(1)
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
    这样能正确处理工具块后紧跟正文的情况。"""
    if not text:
        return text
    result_parts = []
    pos = 0  # 当前已扫描位置（属于正文）
    for m in re.finditer(r'\[\[TOOL:\w+\]\]', text):
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


def run_agent_stream(user_input, history):
    """
    Agent Loop: 生成器版本，逐事件返回
    事件类型: user / assistant / tool_call / tool_result

    改进：
    1. 一次处理多个工具调用
    2. turn 上限 MAX_TURNS 防止无限循环
    3. 异常捕获，保证至少返回回复
    """
    from app.config import MAX_TURNS

    history.append({"role": "user", "content": user_input})
    yield {"type": "user", "content": user_input}

    turn_count = 0
    while turn_count < MAX_TURNS:
        turn_count += 1

        try:
            # 裁剪 + 转换为 LLM 格式（tool_result -> user）
            trimmed = trim_history(history)
            llm_history = _history_for_llm(trimmed)
            reply = call_llm(llm_history)
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

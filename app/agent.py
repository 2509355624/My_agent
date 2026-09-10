"""
Agent Loop
核心循环：LLM -> 工具调用 -> 执行 -> 结果塞回 -> 重复
"""

import json
import re
from app.llm import call_llm
from app.memory import trim_history
from app.tools import execute_tool


def parse_tool_call(text):
    """从 LLM 回复中解析工具调用"""
    match = re.search(r'\[\[TOOL:(\w+)\]\](.*?)\[\[/TOOL\]\]', text, re.DOTALL)
    if not match:
        return None
    name = match.group(1)
    try:
        args = json.loads(match.group(2).strip())
    except json.JSONDecodeError:
        args = {"raw": match.group(2).strip()}
    return {"name": name, "args": args}


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
    """
    history.append({"role": "user", "content": user_input})
    yield {"type": "user", "content": user_input}

    while True:
        # 裁剪 + 转换为 LLM 格式（tool_result -> user）
        trimmed = trim_history(history)
        llm_history = _history_for_llm(trimmed)
        reply = call_llm(llm_history)
        history.append({"role": "assistant", "content": reply})

        tool_call = parse_tool_call(reply)

        if not tool_call:
            yield {"type": "assistant", "content": reply}
            break

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

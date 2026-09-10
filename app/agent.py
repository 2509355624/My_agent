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


def run_agent_stream(user_input, history):
    """
    Agent Loop: 生成器版本，逐事件返回
    事件类型: user / assistant / tool_call / tool_result
    """
    history.append({"role": "user", "content": user_input})
    yield {"type": "user", "content": user_input}

    while True:
        llm_history = trim_history(history)
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

        history.append({
            "role": "user",
            "content": "[工具结果] " + result
        })

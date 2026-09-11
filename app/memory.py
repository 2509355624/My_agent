"""
会话持久化 + 上下文管理

上下文压缩策略（缓存感知 + 一次性整段 LLM 摘要）：
- 原则 1：命中率高时绝不压缩。DeepSeek 1M 上下文，命中 token 近乎免费；
  让上下文自然增长，保持热前缀不被掐断。
- 原则 2：只在"占用率高 且 命中率低"时，把旧区一次性交给 LLM 生成摘要，
  替换为单条摘要消息，建立新的稳定热前缀。
- 原则 3：不做逐轮逐条截断——那会每轮掐断一次热前缀（cache miss 元凶）。

基于天枢 cache-preserving 策略思想简化实现。
"""

import json
import os
from app.config import SESSION_FILE
from app.llm import LAST_USAGE, CONTEXT_LIMIT


# ─── 持久化 ──────────────────────────────────────────

def save_history(history):
    """保存会话到 JSONL 文件"""
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        for msg in history:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def load_history():
    """从 JSONL 文件加载会话"""
    if not os.path.exists(SESSION_FILE):
        return []
    history = []
    with open(SESSION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                history.append(json.loads(line))
    return history


# ─── 上下文压缩 ──────────────────────────────────────

# token 占用率触发点（对应 DeepSeek 1M 上下文）
WATCH_RATIO = 0.5     # 占用 50% 开始"考虑"（仅记录，不压缩）
COMPACT_RATIO = 0.80  # 占用 80% 且命中率低 → 触发整段摘要
REACTIVE_RATIO = 0.90 # 占用 90% → 无论命中率都强制压缩

# 命中率"低"判据：低于此值才认为该压缩（保护热前缀）
LOW_HIT_RATE = 0.3

# 完整保留的最近轮次（保证热前缀有稳定"锚点"）
FULL_RECENT_TURNS = 3

# 压缩冷却：压缩后短时间内不再触发，避免连续摘要浪费
_LAST_COMPACT_TOKENS = 0  # 上次压缩时的占用 token 数
COMPACT_COOLDOWN_TOKENS = 100_000  # 压缩后至少涨 10 万 token 才再次压缩


def _split_turns(messages):
    """把消息按用户轮次分组（每轮从 user 消息开始）"""
    turns = []
    current = []
    for msg in messages:
        if msg["role"] == "user" and current:
            turns.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        turns.append(current)
    return turns


def _summarize_old_turns(old_msgs):
    """把旧交错义消息（不含 system）交给 LLM 生成一段摘要。

    用独立一次 LLM 调用，把整段旧历史压缩成一句 / 若干句要点，
    替换为单条工具结果式消息，从而建立新的稳定前缀。
    """
    from app.llm import call_llm

    # 拼装摘要请求：只把旧内容交给模型，不混入新历史
    payload = [{
        "role": "system",
        "content": (
            "你是会话压缩器。下面是一段 AI 助手同用户的旧对话记录，"
            "包含其调用工具的过程和结果。请你用简洁的中文，提炼出对"
            "后续继续对话仍然重要的事实、结论、用户偏好、已完成的任务"
            "和产生的文件/产物。省略工具调用的机械过程，只保留有长期"
            "价值的信息。控制在 200 字以内。"
        )
    }, {
        "role": "user",
        "content": "以下是旧对话记录：\n\n" + _dump_messages(old_msgs)
    }]

    summary = call_llm(payload)
    summary = summary.strip()
    if not summary:
        summary = "（旧对话无需要保留的长期信息）"
    return summary


def _dump_messages(msgs):
    """把消息转成供摘要的紧凑文本"""
    lines = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content", "")
        if role == "tool_result":
            lines.append("[工具结果：" + m.get("tool_name", "?") + "] " + str(content)[:3000])
        elif role == "user":
            lines.append("[用户] " + str(content))
        elif role == "assistant":
            lines.append("[助手] " + str(content))
    return "\n".join(lines)


def trim_history(history):
    """
    缓存感知的上下文压缩。

    触发逻辑（用最近一次 LLM 调用的真实 token 占用 + 命中率）：
    1. 占用率 < COMPACT_RATIO：直接原样返回（不压，保住热前缀）。
    2. 占用率 >= COMPACT_RATIO 且命中率低：触发一次整段摘要替换旧区。
    3. 占用率 >= REACTIVE_RATIO：无论命中率，强制压缩。
    4. 压缩冷却期内不重复触发。

    压缩方式：保留最近 FULL_RECENT_TURNS 轮完整，其余旧轮一次性交给
    LLM 生成摘要，替换为单条摘要消息。此后热前缀重建，后续稳定命中。
    """
    global _LAST_COMPACT_TOKENS

    system_msgs = [m for m in history if m.get("role") == "system"]
    other_msgs = [m for m in history if m.get("role") != "system"]

    if not other_msgs:
        return history

    # 用真实 token 占用率决定是否压缩（而不是消息条数/轮次）
    total_tokens = LAST_USAGE.get("total_tokens", 0)
    hit_rate = LAST_USAGE.get("hit_rate", 1.0)
    ratio = total_tokens / CONTEXT_LIMIT if CONTEXT_LIMIT else 0

    should = False
    forced = False
    if total_tokens and ratio >= REACTIVE_RATIO:
        should = True
        forced = True
    elif total_tokens and ratio >= COMPACT_RATIO and hit_rate < LOW_HIT_RATE:
        should = True

    # 冷却期防护：非强制场景下，压缩后 token 增量太小则跳过
    # REACTIVE 强制压缩不受冷却影响——接近真实上限时必须立即压，否则爆上下文
    if should and not forced and total_tokens - _LAST_COMPACT_TOKENS < COMPACT_COOLDOWN_TOKENS:
        return history

    if should:
        _LAST_COMPACT_TOKENS = total_tokens
        return _compact(history, system_msgs, other_msgs)

    # 默认：不压缩。命中率越高越不该动（每 token 都是免费命中价）
    return history


def _compact(history, system_msgs, other_msgs, force=False):
    """执行整段摘要替换：保留最近几轮完整，旧区交给 LLM 一次性摘要。"""
    turns = _split_turns(other_msgs)
    if len(turns) <= FULL_RECENT_TURNS:
        return history

    old_turns = turns[:-FULL_RECENT_TURNS]
    recent_turns = turns[-FULL_RECENT_TURNS:]

    # 旧轮扁平化为摘要输入
    old_msgs = []
    for t in old_turns:
        old_msgs.extend(t)

    summary = _summarize_old_turns(old_msgs)

    # 拼装：system + 摘要消息 + 最近几轮 + 状态栏（由 agent 运行时追加）
    result = list(system_msgs)
    result.append({
        "role": "tool_result",
        "tool_name": "compact_summary",
        "content": "[上文压缩摘要] " + summary,
    })
    for t in recent_turns:
        result.extend(t)
    return result


def _compact_if_needed_for_history(history):
    """兼容旧调用点的高层入口"""
    return trim_history(history)
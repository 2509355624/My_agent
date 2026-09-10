"""
会话持久化 + 上下文管理

上下文压缩策略（渐进式）：
- Tier 0 (< WATCH_THRESHOLD)：完整保留，监控
- Tier 1 (>= WATCH_THRESHOLD)：旧轮次工具结果截断
- Tier 2 (>= COMPACT_THRESHOLD)：更多轮次截断 + 更强截断
- Tier 3 (>= REACTIVE_THRESHOLD)：强压缩，只保留最近几轮完整

设计原则：
1. 不删除消息，只截断内容 → 保持消息数组长度稳定，保护 prefix cache
2. 渐进式 → 尽量晚地破坏信息，先压最旧的
3. 工具结果优先压 → user/assistant 消息保留完整对话逻辑
"""

import json
import os
from app.config import SESSION_FILE, MAX_TURNS


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

# 压缩阈值（按轮次计算，一轮 = user + assistant + 若干 tool_result）
WATCH_THRESHOLD = int(MAX_TURNS * 0.5)      # 超过 50% 开始轻度截断
COMPACT_THRESHOLD = int(MAX_TURNS * 0.75)   # 超过 75% 中度截断
REACTIVE_THRESHOLD = int(MAX_TURNS * 0.9)   # 超过 90% 强压缩

# 工具结果截断长度（字符数）
TRUNCATE_LIGHT = 800      # 轻度截断：保留 800 字预览
TRUNCATE_MEDIUM = 300     # 中度截断：保留 300 字预览
TRUNCATE_HEAVY = 100      # 重度截断：保留 100 字预览

# 完整保留的最近轮次
FULL_RECENT_TURNS = 3     # 最近 3 轮永远完整保留


def _estimate_turns(other_msgs):
    """估算消息中的用户轮次（user 消息数 = 轮次数）"""
    return sum(1 for m in other_msgs if m["role"] == "user")


def _truncate_content(content, max_chars, tool_name=""):
    """截断工具结果内容，保留开头预览 + 标记"""
    if len(content) <= max_chars:
        return content

    # 保留前 max_chars 字符
    preview = content[:max_chars]
    marker = f"\n\n[已截断 · 原内容 {len(content)} 字，仅保留前 {max_chars} 字]"
    if tool_name:
        marker = f"\n\n[已截断 · {tool_name} · 原 {len(content)} 字，保留前 {max_chars} 字]"

    return preview + marker


def trim_history(history):
    """
    渐进式上下文压缩。

    策略：
    1. system 消息永远保留
    2. 最近 FULL_RECENT_TURNS 轮完整保留
    3. 更早的轮次，按"越旧越狠"的原则截断工具结果
    4. 超过 MAX_TURNS 的轮次，只保留 user 消息（作为上下文锚点）和截断的 assistant 消息

    关键：不删除消息（只截断内容），保持消息数量稳定，
    保护 prefix cache 的消息索引不被破坏。
    """
    system_msgs = [m for m in history if m.get("role") == "system"]
    other_msgs = [m for m in history if m.get("role") != "system"]

    total_turns = _estimate_turns(other_msgs)

    # 还没到阈值，完整返回
    if total_turns <= WATCH_THRESHOLD:
        return history

    # 按轮次分组（一轮 = 从 user 开始，到下一个 user 之前）
    turns = _split_turns(other_msgs)
    total = len(turns)

    if total <= FULL_RECENT_TURNS:
        return history

    # 计算每轮的压缩级别
    # 最近 FULL_RECENT_TURNS 轮：完整保留
    # 往前的轮次：越旧越狠
    result_msgs = []
    for i, turn_msgs in enumerate(turns):
        turn_age = total - 1 - i  # 0 = 最新，越大越旧

        if turn_age < FULL_RECENT_TURNS:
            # 最近几轮，完整保留
            result_msgs.extend(turn_msgs)
        else:
            # 旧轮次，截断工具结果
            truncate_len = _get_truncate_length(turn_age, total)
            for msg in turn_msgs:
                if msg["role"] == "tool_result":
                    # 已截断的消息字节一次定型，永不再改：
                    # 二次重截会改变历史字节，制造新的缓存断点
                    if msg.get("_truncated"):
                        result_msgs.append(msg)
                        continue
                    msg_copy = dict(msg)
                    tool_name = msg.get("tool_name", "")
                    msg_copy["content"] = _truncate_content(
                        msg["content"], truncate_len, tool_name
                    )
                    msg_copy["_truncated"] = True
                    result_msgs.append(msg_copy)
                else:
                    result_msgs.append(msg)

    # 如果还是太多（超过 MAX_TURNS 很多），做更强的处理：
    # 把最旧的轮次里的 assistant 消息也截断
    final_turns = _split_turns(result_msgs)
    if len(final_turns) > MAX_TURNS:
        excess = len(final_turns) - MAX_TURNS
        compressed = []
        for i, turn_msgs in enumerate(final_turns):
            if i < excess:
                # 最旧的 excess 轮：只保留 user 消息（首条），其他极简
                compressed.append(turn_msgs[0])  # user 消息
                # assistant 消息截断到很短
                for msg in turn_msgs[1:]:
                    # 已定型的消息跳过，不再改字节
                    if msg.get("_truncated"):
                        compressed.append(msg)
                        continue
                    if msg["role"] == "assistant":
                        msg_copy = dict(msg)
                        msg_copy["content"] = _truncate_content(
                            msg["content"], 100, ""
                        )
                        msg_copy["_truncated"] = True
                        compressed.append(msg_copy)
                    elif msg["role"] == "tool_result":
                        msg_copy = dict(msg)
                        msg_copy["content"] = _truncate_content(
                            msg["content"], TRUNCATE_HEAVY, msg.get("tool_name", "")
                        )
                        msg_copy["_truncated"] = True
                        compressed.append(msg_copy)
            else:
                compressed.extend(turn_msgs)
        result_msgs = compressed

    return system_msgs + result_msgs


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


def _get_truncate_length(turn_age, total_turns):
    """根据轮次年龄计算截断长度：越旧越短"""
    # turn_age: 0 = 最新，total_turns-1 = 最旧
    if total_turns <= FULL_RECENT_TURNS + 1:
        return TRUNCATE_LIGHT

    # 线性插值：从 LIGHT 到 HEAVY
    old_ratio = (turn_age - FULL_RECENT_TURNS) / max(1, total_turns - FULL_RECENT_TURNS)
    old_ratio = min(1.0, max(0.0, old_ratio))

    truncate_range = TRUNCATE_LIGHT - TRUNCATE_HEAVY
    length = int(TRUNCATE_LIGHT - old_ratio * truncate_range * 0.8)
    return max(TRUNCATE_HEAVY, length)

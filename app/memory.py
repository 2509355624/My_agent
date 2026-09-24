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
import threading
import time
from app.agents import session_file as _agent_session_file
from app.llm import LAST_USAGE, CONTEXT_LIMIT


# ─── 持久化 ──────────────────────────────────────────

# 进程内写锁：防止同进程多线程同时写同一个会话文件（多端并发时至少保证
# 单进程内是串行的；跨进程的并发写由 os.replace 的原子性兜底——最终文件
# 永远是"某一次完整写入"的结果，不会出现两份内容交错）。
_SAVE_LOCK = threading.Lock()


def _atomic_replace(src, dst, attempts=5, delay=0.05):
    """把 src 原子替换为 dst。

    os.replace 在同一文件系统内是原子操作（Windows 走 MoveFileEx 的 replace
    语义），读者永远看不到"写了一半"的文件。Windows 上目标文件可能被其他
    进程（例如同时刷新的另一个终端）短暂占用而抛 PermissionError，做少量
    重试即可，不引入跨进程文件锁的复杂度。
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def save_history(history, agent_id=None, session_key=None):
    """原子保存某个 agent 的会话到它的 JSONL 文件。

    先写同目录临时文件 -> flush + fsync -> os.replace 原子替换。
    相比直接 open("w") 覆盖写，避免两种问题：
    1. 写到一半进程崩溃/被杀 -> 留下半截损坏文件，下次直接解析失败；
    2. 多端（手机/电脑）同时刷新 -> 读者读到中间态。

    agent_id 决定写哪个 agent 的会话文件（不传用默认 agent）。
    session_key 用于「一个 agent 下挂多条互不相干的会话线」的场景（QQ 接入
    时每个私聊用户 / 每个群各一条），不传就是该 agent 的主会话。
    """
    path = _agent_session_file(agent_id, session_key)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = path + ".tmp"
    with _SAVE_LOCK:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for msg in history:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(tmp_path, path)


def load_history(agent_id=None, session_key=None):
    """从某个 agent 的 JSONL 文件加载会话。

    跳过空行与损坏行：单行坏数据不应该让整段历史读不出来
    （原子写之后正常情况下不会出现，这里是防御性兜底）。
    session_key 与 save_history 同义。
    """
    path = _agent_session_file(agent_id, session_key)
    if not os.path.exists(path):
        return []
    history = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                history.append(json.loads(line))
            except json.JSONDecodeError:
                continue
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

# 压缩冷却：压缩后短时间内不再触发，避免连续摘要浪费。
# 按 agent 分别记水位——多个 agent 并存时，A 的压缩不该让 B 的冷却误判。
_LAST_COMPACT_TOKENS = {}  # {agent_id: 上次压缩时的占用 token 数}
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


def trim_history(history, agent_id=None):
    """
    缓存感知的上下文压缩。

    触发逻辑（用最近一次 LLM 调用的真实 token 占用 + 命中率）：
    1. 占用率 < COMPACT_RATIO：直接原样返回（不压，保住热前缀）。
    2. 占用率 >= COMPACT_RATIO 且命中率低：触发一次整段摘要替换旧区。
    3. 占用率 >= REACTIVE_RATIO：无论命中率，强制压缩。
    4. 压缩冷却期内不重复触发。

    压缩方式：保留最近 FULL_RECENT_TURNS 轮完整，其余旧轮一次性交给
    LLM 生成摘要，替换为单条摘要消息。此后热前缀重建，后续稳定命中。

    agent_id 只用于隔离压缩冷却水位（每个 agent 各记一份）。
    """
    key = agent_id or "_default"

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
    last_tokens = _LAST_COMPACT_TOKENS.get(key, 0)
    if should and not forced and total_tokens - last_tokens < COMPACT_COOLDOWN_TOKENS:
        return history

    if should:
        _LAST_COMPACT_TOKENS[key] = total_tokens
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


def _compact_if_needed_for_history(history, agent_id=None):
    """兼容旧调用点的高层入口"""
    return trim_history(history, agent_id)
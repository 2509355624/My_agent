"""
会话持久化 + 上下文管理

上下文压缩策略（缓存感知 + 一次性整段 LLM 摘要）：
- 原则 1：命中率高时**尽量**不压缩。命中的 token 只有未命中的十分之一价，
  让上下文自然增长、保持热前缀不被掐断，通常比反复摘要更划算。
- 原则 2：命中率低时提前压——到预算的四分之三且命中率差，就把旧区一次性
  交给 LLM 生成摘要，替换为单条摘要消息，建立新的稳定热前缀。
- 原则 3：不做逐轮逐条截断——那会每轮掐断一次热前缀（cache miss 元凶）。
- 原则 4：**预算是硬闸门**。原则 1 的「别压缩」不能没有上限，否则上下文
  一路涨到几万 token，每轮都在为这一长串付钱。所以一到预算就无条件压，
  无论命中率多高。这是「最坏情况花多少钱」的唯一保证。

水位按 **token 绝对量**判断（对比 CONTEXT_BUDGET），不再按百分比算——
预算本身是可配的，用绝对量能让「设 3.2 万就真的是 3.2 万」一目了然。

基于天枢 cache-preserving 策略思想简化实现。
"""

import json
import os
import re
import threading
import time
from app.agents import session_file as _agent_session_file
from app.config import CONTEXT_BUDGET
from app.llm import current_usage


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


def peek_system(agent_id=None, session_key=None):
    """只读会话文件第一行，取回首条 system 的内容；不是 system 或读不到返回 None。

    存在的理由：判断"人设变了没有"每轮都要做一次，而 load_history 会把整个
    文件解析成对象列表——QQ 群里聊上几个月，会话文件可能是几 MB，每轮为比
    一个字符串读几 MB 太亏。JSONL 的首行就是 system 头，读一行就够。
    """
    path = _agent_session_file(agent_id, session_key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            line = f.readline().strip()
    except OSError:
        return None
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(msg, dict) and msg.get("role") == "system":
        return msg.get("content")
    return None


# ─── 上下文压缩 ──────────────────────────────────────

# 水位：相对预算的比例。到 WARN_RATIO 且命中率低才提前压；到 100% 无条件压
WARN_RATIO = 0.75

# 命中率"低"判据：低于此值才认为该提前压缩（保护热前缀）
LOW_HIT_RATE = 0.3

# 完整保留的最近轮次（保证热前缀有稳定"锚点"）
FULL_RECENT_TURNS = 3

# 压缩冷却：压缩后至少再涨这么多（占预算的比例）才考虑下一次，避免连续摘要。
# 强制压缩不受它限制——接近预算时必须立即压，否则每轮都超支。
# 水位按 agent 记。注意同一个 agent 挂多条会话线时（QQ 一个群里多个群共用
# qq 这个 agent）它们共用一份水位，理论上会互相干扰；实际影响很小——压完会
# 落到很低，涨回 WARN_RATIO 本身就隔了足够多的增量，冷却很少成为瓶颈。
_LAST_COMPACT_TOKENS = {}  # {agent_id: 上次压缩时的 token 数}
COMPACT_COOLDOWN_RATIO = 0.2

# 摘要消息自身的 token 开销（提示词要求控制在 200 字内，留足余量）
_SUMMARY_TOKENS = 800

# 中日韩字符 + 全角标点。用于 estimate_tokens()，见那里的说明。
_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff"
                     r"\uf900-\ufaff\uff00-\uffef]")


def estimate_tokens(text):
    """粗估一段文本的 token 数。

    口径取自 DeepSeek 官方文档：1 个中文字符 ≈ 0.6 token，1 个英文字符
    ≈ 0.3 token。**只用于「压缩之后还超不超预算」这种兜底判断**，真实用量
    一律以 API 返回的 usage 为准（见 llm.current_usage）。两者不需要对齐，
    估算偏大一点反而是好事。
    """
    if not text:
        return 0
    s = str(text)
    cjk = len(_CJK_RE.findall(s))
    return int(cjk * 0.6 + (len(s) - cjk) * 0.3)


def estimate_messages(msgs):
    """粗估一段消息列表的 token 数（含每条的角色开销）。"""
    total = 0
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            # 多模态消息（历史里不该出现，防御性处理）
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text") or "")
        total += 4
    return total


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


def trim_history(history, agent_id=None, usage=None, budget=None):
    """
    缓存感知的上下文压缩。

    触发逻辑（用最近一次 LLM 调用的真实 token 占用 + 命中率）：
    1. 未到 WARN_RATIO：原样返回（不压，保住热前缀）。
    2. 到 WARN_RATIO 且命中率低：触发一次整段摘要替换旧区。
    3. 到预算：无论命中率，强制压缩。这是原则 4 的硬闸门。
    4. 压缩冷却期内不重复触发（强制压缩不受此限）。

    压缩方式：保留最近 FULL_RECENT_TURNS 轮完整，其余旧轮一次性交给
    LLM 生成摘要，替换为单条摘要消息。此后热前缀重建，后续稳定命中。

    usage: 上一次 LLM 调用的用量 dict（llm.current_usage() 的返回值）。不传
      则回退到「当前线程最近一次」，兼容旧调用点与测试。**agent 循环应当显式
      传**——QQ 场景下线程会被不同会话复用，显式传才读不到别人的数。
    budget: 该 agent 的 token 预算；不传用全局 CONTEXT_BUDGET。
    agent_id 只用于隔离压缩冷却水位（每个 agent 各记一份）。
    """
    key = agent_id or "_default"
    budget = budget or CONTEXT_BUDGET

    system_msgs = [m for m in history if m.get("role") == "system"]
    other_msgs = [m for m in history if m.get("role") != "system"]

    if not other_msgs:
        return history

    if usage is None:
        usage = current_usage()
    total_tokens = usage.get("total_tokens", 0) or 0
    hit_rate = usage.get("hit_rate", 1.0)

    should = False
    forced = False
    if total_tokens >= budget:
        should = True
        forced = True
    elif total_tokens >= budget * WARN_RATIO and hit_rate < LOW_HIT_RATE:
        should = True

    # 冷却期防护：非强制场景下，压缩后 token 增量太小则跳过。
    # 强制压缩不受冷却影响——到预算了必须立即压，否则每轮都超支。
    last_tokens = _LAST_COMPACT_TOKENS.get(key, 0)
    if (should and not forced
            and total_tokens - last_tokens < budget * COMPACT_COOLDOWN_RATIO):
        return history

    if should:
        _LAST_COMPACT_TOKENS[key] = total_tokens
        return _compact(history, system_msgs, other_msgs, budget)

    # 默认：不压缩。命中率越高越不该动（每 token 都是命中价）
    return history


def _compact(history, system_msgs, other_msgs, budget):
    """执行整段摘要替换：保留最近若干轮完整，旧区交给 LLM 一次性摘要。

    **保留几轮不是固定的**：先估一次体积，若「摘要 + 最近 FULL_RECENT_TURNS
    轮」仍超预算，就少保留一轮、把更多内容并进摘要，最多降到只剩最近 1 轮。
    估算放在调 LLM 之前，所以无论降几级都只花一次摘要调用的钱。

    没有这一步，预算在「最近几轮自己就很大」时形同虚设——摘要省下来的空间
    会被原样留下的那几轮吃回去，于是每轮都超支、每轮都要摘要。
    """
    turns = _split_turns(other_msgs)
    if len(turns) <= FULL_RECENT_TURNS:
        # 轮数本来就少：再压就把上下文榨干了，不压。
        # 这种「单轮自身过大」只能靠入口侧限制输入长度兜住。
        return history

    base = estimate_messages(system_msgs) + _SUMMARY_TOKENS
    keep = FULL_RECENT_TURNS
    while True:
        size = base + sum(estimate_messages(t) for t in turns[-keep:])
        if size <= budget or keep <= 1:
            break
        keep -= 1

    if size > budget:
        print("[compact] 摘要后仍需约 %d tokens（预算 %d）：最近一轮自身过大，"
              "应在入口侧限制单条输入长度" % (size, budget))

    old_turns = turns[:-keep]
    recent_turns = turns[-keep:]

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
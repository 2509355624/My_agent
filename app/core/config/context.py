"""会话轮次、上下文预算与压缩目标配置。

只放“愿意为这一轮付多少钱”和本地估算阈值；请求与压缩执行在 app/memory.py。
"""

import os

from app.core.config.llm import LLM_PROVIDER

# ─── 会话轮次与上下文预算 ─────────────────────────────

MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))

# 上下文预算：**单次请求允许携带的 prompt token 上限**。
# 注意它跟模型的物理上下文上限（火山/DeepSeek 是 128K~1M）是两件事——
# 这里管的是「我愿意为每一轮付多少钱」，不是「模型装不装得下」。超了就
# 压缩历史（见 app/memory.py）。
# **这是压缩的唯一判据**（2026-09-29 起）：到预算就压，不再看轮数。
# 判据用 memory.estimate_messages 的**本地估算**，刻意不读 API 的 usage：
# usage 走 threading.local，而 QQ 适配层用 asyncio.to_thread 从线程池取线程，
# 同一会话的不同消息落在不同线程上，读到的 total_tokens 常常是 0 ——
# 这就是「预算设了却从来没压缩过」的根因（实测一个群攒到 18 万字全量重发）。
# 折算参考：一个中文字符约 0.6 token，32000 约合 5.3 万汉字、50~60 轮对话。
# 单个 agent 可在 agent.json 里用 context_budget 覆盖（0 = 用这里的全局值）。
CONTEXT_BUDGET = int(os.getenv("CONTEXT_BUDGET", "32000"))

# 各provider 的**物理上下文窗口**（token）。用于给 CONTEXT_BUDGET 封顶：
# 预算是「我愿意为这一轮付多少钱」，物理窗口是「模型装不装得下」——
# 预算线高过物理墙时，压缩这条闸门就形同虚设（prompt 撞满窗口后才想起
# 已经超了，可那时已经截断完了）。
#
# 只列**明显小于全局预算**的 provider。云端（volc/deepseek/scnet/mimo）都是
# 128K~1M，装得下 32000，不列= 不封顶，行为与从前完全一致。
#
# ollama 的窗口不是模型属性，而是 Modelfile 里写死的 num_ctx（实测这台
# 机器上 qwen3.8-9b-heretic = 16384）。要改就改 Modelfile 的 num_ctx，
# 这里只是读同一个数、提前一步拦住，别指望它等于模型上限。
_PROVIDER_CONTEXT_WINDOW = {
    "ollama": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
    # llama-server 的窗口由启动参数 -c 决定（start-server.bat 里 16384），
    # 两边保持同一个数：改启动脚本时这里要跟着改。
    "llama": int(os.getenv("LLAMA_NUM_CTX", "16384")),
    # DeepSeek 官方（2026-10-09 实测 GET /models）：
    #   deepseek-flash (V4.1-Flash) context_window = 1,048,576，max_output 393,216
    # 云端本来就是「不列 = 不封顶」，这里列出来**只为给「把预算开到最大」的
    # agent（`agents/writing`）留出 30% 余量**：不列的话预算 100 万就等于物理
    # 窗口 104.8 万，压缩闸门形同虚设（真撞墙才想起已经超了）。对预算 3 万的
    # agent 是彻底的 no-op（min(30000, 734003) 还是 30000）。
    "deepseek": int(os.getenv("DEEPSEEK_CONTEXT_WINDOW", "1048576")),
}

# 压缩后的目标水位 = 窗口的这个比例。留出的余量给状态栏、extra_context
# 这些挂在尾巴上的东西（实测≈465 token）以及本轮新发的消息。
_CONTEXT_WINDOW_SAFE_RATIO = 0.7


def physical_budget(provider=None, model=None, budget=None):
    """把 token 预算压到模型物理窗口以内。

    provider 不在 _PROVIDER_CONTEXT_WINDOW 里（或窗口 >= 预算）就原样返回，
    所以对云端 provider 是彻底的 no-op。
    """
    if budget is None:
        budget = CONTEXT_BUDGET
    pid = (provider or LLM_PROVIDER or "").lower()
    window = _PROVIDER_CONTEXT_WINDOW.get(pid)
    if not window:
        return budget
    cap = int(window * _CONTEXT_WINDOW_SAFE_RATIO)
    return min(budget, cap) if cap > 0 else budget

# 会话历史窗口（**已废弃，只作兼容开关**）。
#
# 2026-09-27 曾按轮数开窗（保留最近 N 轮），理由是「轮数不用读 usage」。
# 2026-09-29 又改回 token 判据（见 CONTEXT_BUDGET）：轮数开窗挡不住
# 「100 轮里塞了 500 条消息」的群——实测那个群到 384 条 / 13.2 万字，
# 按轮数裁完全压不住，单轮又慢又贵。
#
# 现在 trim_window 只在 max_turns<=0 时**关闭裁剪**，正数一律忽略。
# 保留这个配置项是为了「一键关掉压缩」这条退路。
CONTEXT_MAX_TURNS = int(os.getenv("CONTEXT_MAX_TURNS", "20"))

# 滚出窗口的那批轮次要摘成的摘要字数上限。比群聊归档摘要（300 字）更短：
# 细节由窗口里的原文兜着，摘要只需要留住「常聊的群友 + 他的偏好」。
MEMORY_TURN_DIGEST_CHARS = int(os.getenv("MEMORY_TURN_DIGEST_CHARS", "150"))
__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")] + ["physical_budget"]

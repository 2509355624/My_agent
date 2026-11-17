"""
配置加载
从 .env 文件读取环境变量，提供全局配置项
"""

import os

from app.core.config.paths import (
    AGENTS_DIR,
    BASE_DIR,
    DATA_DIR,
    DOCUMENTS_DIR,
    ROOT_DIR,
    SKILLS_DIR,
    VECTOR_STORE_DIR,
    WEB_DIR,
    _is_test_process,
    state_path,
)

from app.core.config.persona import *  # noqa: F401,F403
from app.core.config.llm import *  # noqa: F401,F403
from app.core.config.image import *  # noqa: F401,F403
from app.core.config.vision import *  # noqa: F401,F403
from app.core.config.rag import *  # noqa: F401,F403
from app.core.config.web import *  # noqa: F401,F403
from app.core.config.web import _parse_gallery_roots  # noqa: F401
from app.core.config.card import *  # noqa: F401,F403
from app.core.config.qq import *  # noqa: F401,F403
from app.core.config.watchdog import *  # noqa: F401,F403

# ─── Web 搜索（豆包搜索）─────────────────────────────
# 豆包搜索（原 联网搜索/融合信息搜索）专用 API Key，在火山「联网搜索控制台」创建：
# https://console.volcengine.com/search-infinity/api-key?tab=post_paid
# 注意：与方舟 ARK 的 VOLC_API_KEY 不同，需单独开通。未配置时 web_search 自动回退 DuckDuckGo。
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")
DOUBAO_SEARCH_ENDPOINT = os.getenv("DOUBAO_SEARCH_ENDPOINT",
                                   "https://open.feedcoopapi.com/search_api/web_search")


# ─── Agent ──────────────────────────────────────────

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

# 搜索结果截断上限（字符）。联网搜索单次返回动辄三四千字，而它会作为
# tool_result **永久留在历史里**——不截断的话，用过一次之后每一轮请求都要
# 重发这几千 token。截断只影响「查资料的详尽程度」，不影响聊天。
WEB_SEARCH_MAX_CHARS = int(os.getenv("WEB_SEARCH_MAX_CHARS", "800"))

# ─── 路径 ───────────────────────────────────────────
# 已搬到 app/core/config/paths.py；这里只保留引用。


# 多 agent：每个 agent 一个自包含目录（agents/<id>/ 里放 agent.json 配置、
# prompt.md 人设、session.jsonl 会话）。会话文件路径由 app/agents.py 解析，
# 不再有「全局单会话文件」这种东西。
DEFAULT_AGENT_ID = os.getenv("DEFAULT_AGENT_ID", "main")


# ─── 自定义 LLM 中转站（2026-10-22）─────────────────────────
# 用户在漫画台自己加的中转站（第一家是 relaycore 的 grok-4.5），存在
# `data/custom_providers.json`，启动时合并进 PROVIDERS。
#
# **为什么合并进 PROVIDERS 而不是另开一张表**：`llm.get_effective_config`、
# `model_catalog._fetch`、`/api/providers`、漫画台的两级下拉全都在读这张
# dict。合并进去，这些地方一行都不用改；另开一张表就得在每一处加分支。
#
# 合并是**幂等**的：先摘掉上一次合并进去的那几个（靠 cfg["custom"] 认），
# 再按文件重新加。否则改完一个渠道调一次 reload，PROVIDERS 里就会堆同名旧值，
# 删掉的渠道也永远留在下拉里。
CUSTOM_PROVIDERS_FILE = os.path.join(DATA_DIR, "custom_providers.json")


def load_custom_providers():
    """把自定义渠道合并进 PROVIDERS，返回合并了几个。

    这个函数在模块末尾调用一次；网页上增删改之后由 `app/main.py` 再调，
    所以不重启进程也能让新渠道立刻可用。
    """
    from app import provider_store

    for pid in [k for k, v in PROVIDERS.items() if (v or {}).get("custom")]:
        PROVIDERS.pop(pid, None)

    items = provider_store.load(CUSTOM_PROVIDERS_FILE)
    n = 0
    for pid, cfg in items.items():
        if pid in PROVIDERS:      # 内置 id 优先，绝不覆盖
            continue
        PROVIDERS[pid] = cfg
        n += 1
    return n


# 启动即合并一次。出错只吞掉——自定义渠道是加分项，配置坏了不该让整个进程
# 起不来（它是被 QQ 机器人、网页端、漫画台共同 import 的）。
try:
    CUSTOM_PROVIDER_COUNT = load_custom_providers()
except Exception:                                    # pragma: no cover
    import logging as _logging
    _logging.getLogger("config").warning("自定义渠道加载失败（忽略）", exc_info=True)
    CUSTOM_PROVIDER_COUNT = 0

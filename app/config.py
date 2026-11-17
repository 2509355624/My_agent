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

from app.core.config.llm import *  # noqa: F401,F403
from app.core.config.image import *  # noqa: F401,F403
from app.core.config.vision import *  # noqa: F401,F403
from app.core.config.rag import *  # noqa: F401,F403
from app.core.config.web import *  # noqa: F401,F403
from app.core.config.web import _parse_gallery_roots  # noqa: F401
from app.core.config.card import *  # noqa: F401,F403
from app.core.config.qq import *  # noqa: F401,F403

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


# ── 看门狗的「静默判据」（假在线）──────────────────────────────
# 看门狗的三态探针**探不出「假在线」**：:3000 接口全好、登录态在，只有
# 「腾讯 → QQ 客户端」的下行推送死了（2026-09-29 22:36~22:53 实测）。能观测到的
# 唯一信号是「本该到的消息没到」。所以改看 qq_bot 的静默时长（状态快照里的
# last_activity_ago），超过 WATCHDOG_SILENCE_SECONDS 一条都没有 → 重启当探针。
#
# ⚠️ 重启当探针为什么成立（用户 2026-09-29 的判断，已被日志证实）：
#   自动登录 → 会话本来是好的，就是「群里本来就安静」→ 静默放过、不通知；
#   要扫码   → 会话早被腾讯作废，这正是假在线/掉线的本质 → 推二维码。
# 所以误报的代价只是一次静默重启，真故障却能第一时间暴露。
WATCHDOG_SILENCE_SECONDS = float(os.getenv("WATCHDOG_SILENCE_SECONDS", "1200"))

# 连续静默时两次重启之间的**起步**间隔（秒）。每静默重启一次就翻倍，封顶
# WATCHDOG_SILENCE_MAX_GAP；一旦收到消息立刻清零。
# ⚠️ 没有它就会出事：静默重启成功后 qq_bot 一起重启、静默计时归零，整夜没人
# 说话就变成每 WATCHDOG_SILENCE_SECONDS 杀一次 QQ —— 反过来招风控。
WATCHDOG_SILENCE_COOLDOWN = float(os.getenv("WATCHDOG_SILENCE_COOLDOWN", "3600"))
WATCHDOG_SILENCE_MAX_GAP = float(os.getenv("WATCHDOG_SILENCE_MAX_GAP", "14400"))

# ⚠️ 静默时**要不要真的重启**。默认 0 = **只记日志、不重启**。
#
# 原来是 1（重启当探针），09-30 用户拍板改成默认关。原因是一天 32 次实测：
#
#   静默重启 → 一键启动全部 force → taskkill /IM QQ.exe /F → -q 快速登录
#     → 腾讯判定「刚登过又登」= 异常登录 → 作废会话
#     → 真掉线（tag=下线通知 / 你的账号当前登录已失效）
#     → 又没人说话 → 又静默 → 又重启 …… 死循环
#
# 09-30 硬证据：08:57:50 静默重启（08:57:59 还报过「已有 QQ 适配层在运行」），
# 08:58:55 重连成功，**09:00:57 就掉了线**——间隔 3 分钟。05:37 那次更直接：
# 重启后看门狗自己因为锁冲突退出（05:37:18），机器人反而活下来了。
#
# 所以「假在线」很可能是这个循环**制造**出来的，不是根因。先关掉静默重启，
# 用一天观察真实掉线频率：明显下降 = 推断成立；照旧 1 小时一次 = 真是风控。
# 想恢复老行为把 .env 里设 WATCHDOG_SILENCE_RESTART=1（改完要重启看门狗）。
WATCHDOG_SILENCE_RESTART = os.getenv("WATCHDOG_SILENCE_RESTART", "0") not in ("0", "false", "False", "")

# ── ComfyUI 看门狗分支总闸（2026-09-30）──────────────────
# ⚠️ 默认 0 = 整个 ComfyUI 分支不动作（只探活不重启）。
# 09-30 凌晨它过度重启：05:20/05:51/06:26 各触发一轮（含 1800 秒退避），
# 因为 ComfyUI 是**按需启动、常常故意不开**，看门狗却当成「崩了」。
# 现在默认开启；`comfy_seen` 闩仍保留，只在 ComfyUI 曾经在线后持续失联时执行恢复。
WATCHDOG_COMFY_ENABLED = os.getenv("WATCHDOG_COMFY_ENABLED", "1") not in ("0", "false", "False", "")


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

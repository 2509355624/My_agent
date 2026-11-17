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
from app.core.config.context import *  # noqa: F401,F403

# ─── Web 搜索（豆包搜索）─────────────────────────────
# 豆包搜索（原 联网搜索/融合信息搜索）专用 API Key，在火山「联网搜索控制台」创建：
# https://console.volcengine.com/search-infinity/api-key?tab=post_paid
# 注意：与方舟 ARK 的 VOLC_API_KEY 不同，需单独开通。未配置时 web_search 自动回退 DuckDuckGo。
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")
DOUBAO_SEARCH_ENDPOINT = os.getenv("DOUBAO_SEARCH_ENDPOINT",
                                   "https://open.feedcoopapi.com/search_api/web_search")

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

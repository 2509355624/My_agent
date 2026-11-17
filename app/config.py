"""
配置加载
从 .env 文件读取环境变量，提供全局配置项
"""

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
from app.core.config.search import *  # noqa: F401,F403
from app.core.config.agent import *  # noqa: F401,F403

# ─── 路径 ───────────────────────────────────────────
# 已搬到 app/core/config/paths.py；这里只保留引用。


def load_custom_providers():
    """兼容门面：使用本文件里的路径，实现委托给 core.config.llm。"""
    from app.core.config import llm as _llm

    return _llm.load_custom_providers(CUSTOM_PROVIDERS_FILE)


# 启动即合并一次。出错只吞掉——自定义渠道是加分项，配置坏了不该让整个进程
# 起不来（它是被 QQ 机器人、网页端、漫画台共同 import 的）。
try:
    CUSTOM_PROVIDER_COUNT = load_custom_providers()
except Exception:                                    # pragma: no cover
    import logging as _logging
    _logging.getLogger("config").warning("自定义渠道加载失败（忽略）", exc_info=True)
    CUSTOM_PROVIDER_COUNT = 0

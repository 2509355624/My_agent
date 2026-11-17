"""配置公开入口。

迁移期：路径与环境变量已经搬到本包；其余域仍由 ``app/config.py`` 实现。
新代码从本包或域模块 import；旧代码继续使用 ``app.config``。
"""

from .env import _env_bool, _env_list
from .paths import (
    AGENTS_DIR,
    BASE_DIR,
    DATA_DIR,
    DOCUMENTS_DIR,
    ROOT_DIR,
    SKILLS_DIR,
    VECTOR_STORE_DIR,
    WEB_DIR,
    state_path,
)


def __getattr__(name):
    """迁移期：未搬完的配置名回落到旧实现。"""
    from app import config as _legacy

    return getattr(_legacy, name)

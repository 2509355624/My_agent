"""配置公开入口。

新代码从本包或域模块 import；旧代码继续使用 ``app.config``。
本包不再惰性回落到旧文件，所有域都已显式导出。
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
    _is_test_process,
    state_path,
)

from .agent import *  # noqa: F401,F403
from .card import *  # noqa: F401,F403
from .context import *  # noqa: F401,F403
from .image import *  # noqa: F401,F403
from .persona import *  # noqa: F401,F403
from .llm import *  # noqa: F401,F403
from .qq import *  # noqa: F401,F403
from .rag import *  # noqa: F401,F403
from .runtime import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403
from .vision import *  # noqa: F401,F403
from .watchdog import *  # noqa: F401,F403
from .web import *  # noqa: F401,F403
from .web import _parse_gallery_roots  # noqa: F401

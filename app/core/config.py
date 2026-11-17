"""配置公开入口。

迁移期：实现仍在 ``app/config.py``。新代码从这里 import；
旧代码继续使用 ``app.config``，等配置按域拆分完成后再反转。
"""

# standard-exception: PY-04 兼容 facade，实现仍在 app.config；拆分后移除。
from app.config import *  # noqa: F401,F403

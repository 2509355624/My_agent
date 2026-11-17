""".env 加载与环境变量解析。

所有配置域都从这里拿布尔/列表，不各自写一份 ``_env_bool``。
"""

import os

from dotenv import load_dotenv

from app.core.config.paths import BASE_DIR

_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)


def _env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "")


def _env_list(name):
    """逗号分隔的字符串列表，去空项。"""
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]

"""运行期可热更新的设置。

只存**非密钥**、管理页可以改的运行参数（降级链、超时、重试、预算等）。
密钥仍只从 .env 读；这里落盘的是 `data/runtime_settings.json`，不进 git。

读取按 mtime 缓存，保存用临时文件 + os.replace；管理页保存后下一次 LLM
调用就会读到新值，不需要重启进程。
"""

import json
import os
import threading

from app.core.config.paths import DATA_DIR

_LOCK = threading.RLock()
_CACHE = {"path": None, "mtime": None, "data": {}}


def runtime_path():
    """运行期设置文件路径；测试可用 AGENT_RUNTIME_SETTINGS 指到临时文件。"""
    override = os.environ.get("AGENT_RUNTIME_SETTINGS")
    if override:
        return override
    return os.path.join(DATA_DIR, "runtime_settings.json")


def load():
    """读当前运行期设置；文件不存在/坏掉时返回 {}（绝不抛错）。"""
    path = runtime_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    with _LOCK:
        if (_CACHE["path"] == path and _CACHE["mtime"] == mtime):
            return dict(_CACHE["data"])
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        _CACHE.update(path=path, mtime=mtime, data=data)
        return dict(data)


def save(patch):
    """合并写入；值为 None 表示删掉这一项。成功返回 True。"""
    if not isinstance(patch, dict):
        return False
    path = runtime_path()
    with _LOCK:
        data = dict(load())
        for key, value in patch.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, path)
        except OSError:
            return False
    clear_cache()
    return True


def clear_cache():
    with _LOCK:
        _CACHE.update(path=None, mtime=None, data={})

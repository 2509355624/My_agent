"""绝对路径与 state/ 目录。

放在 ``app/core/config/paths.py`` 后，``__file__`` 比原来深两层；这里统一用
四层 dirname 回到仓库根，避免每个调用方自己算路径。
"""

import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
ROOT_DIR = BASE_DIR

SKILLS_DIR = os.path.join(BASE_DIR, "skills")
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
DOCUMENTS_DIR = os.path.join(BASE_DIR, "documents")
VECTOR_STORE_DIR = os.path.join(BASE_DIR, "vector_store", "chroma")
AGENTS_DIR = os.path.join(BASE_DIR, "agents")

# ─── state/ 路径与测试隔离 ───────────────────────────
# state/ 下都是运行期真实数据；测试跑一遍不能往里写假数据。
# 测试隔离只认进程入口，不认 sys.modules 里有没有 unittest——
# 运行时 import matplotlib 这类库也会把 unittest 带进来。
_TEST_STATE_DIR = None


def _is_test_process():
    """是不是「测试 runner 拉起来」的进程。"""
    if os.environ.get("AGENT_STATE_TEST") == "1":
        return True
    if not ("unittest" in sys.modules or "pytest" in sys.modules):
        return False
    main = sys.modules.get("__main__")
    main_file = (getattr(main, "__file__", "") or "").replace("\\", "/").lower()
    argv0 = (sys.argv[0] if sys.argv else "").replace("\\", "/").lower()
    orig = list(getattr(sys, "orig_argv", None) or [])
    m_module = ""
    for i, arg in enumerate(orig[:-1]):
        if arg == "-m":
            m_module = orig[i + 1]
            break
    hay = " ".join((main_file, argv0, m_module)).lower()
    return "unittest" in hay or "pytest" in hay


def state_path(filename):
    """state/ 下某个文件的路径；测试 runner 进程导向临时目录。"""
    global _TEST_STATE_DIR
    if _is_test_process():
        if _TEST_STATE_DIR is None:
            _TEST_STATE_DIR = tempfile.mkdtemp(prefix="agent_state_test_")
        return os.path.join(_TEST_STATE_DIR, filename)
    return os.path.join(BASE_DIR, "state", filename)

"""RAG 配置域：知识库开关、角色记忆向量库、世界书预算。

这里只放“检索与记忆”的默认值；向量库 daemon、重排模型和角色记忆实现
分别在 app/rag/、app/rerank.py、app/persona_rag.py，不写进本文件。
"""

import os

from app.core.config.paths import BASE_DIR as _BASE_DIR


def rag_enabled():
    """RAG 向量知识库能不能用（2026-10-19）。三态：

        ENABLE_RAG 未配置 → 按「chromadb 能不能 import」自判（装了就有）
        ENABLE_RAG=1/0    → 强制开 / 强制关

    为什么要这一层：chromadb + sentence-transformers + torch 装完 2~3 GB，
    很多人只要生图和助手、根本不想要知识库。`tools/registry.py` 原来无条件注册
    kb / preset / memory 三套工具，没装的机器一调就拉起 rag_daemon → ImportError。
    现在没装就整套不注册，模型也看不见这些工具。
    """
    raw = os.getenv("ENABLE_RAG", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    try:
        import chromadb  # noqa: F401
    except Exception:
        return False
    return True


# ─── 角色扮演的长期记忆 RAG（2026-11-05）─────────────────
#
# 和主 RAG **分库分模型**：主库是 BGE-M3（1024 维），这里用 CPU 小模型
# bge-small-zh-v1.5（512 维）。混在一个 daemon 里会维度不匹配，
# 所以向量目录也单独一份，互不影响。
#
#   PERSONA_RAG=0            关掉（默认开，模型缺失时会自动降级为不检索）
#   PERSONA_RAG_MODEL        模型名或本地目录（ModelScope 缓存能命中就不用下载）
#   PERSONA_RAG_DATA_DIR     向量库目录（只放扮演记忆）
#   PERSONA_RAG_K            每轮最多注入几条回忆
#   PERSONA_RAG_MAX_DISTANCE 余弦距离上限，超过的不注入（0.60 来自实测：
#                            相关记忆约 0.34~0.51，闲聊约 0.63+；
#                            2026-11-10 放宽到 0.66，配合 MMR 去重）
PERSONA_RAG = os.getenv("PERSONA_RAG", "1").strip().lower() not in (
    "0", "false", "no", "off")
PERSONA_RAG_MODEL = os.getenv(
    "PERSONA_RAG_MODEL", "BAAI/bge-small-zh-v1.5").strip()
PERSONA_RAG_DATA_DIR = os.getenv(
    "PERSONA_RAG_DATA_DIR",
    os.path.join(_BASE_DIR, "vector_store", "chroma_persona"))
try:
    # 2026-11-08 用户拍板 3 → 5：演进到几百条记忆之后，top-3 经常把
    # 「馄饨摊」这种具体场景挤掉，只剩几条泛泛的偏好。5 条大约多 400~600 字，
    # 锚点（第一次/初夜）另算、不占这个名额。
    # 2026-11-10 再放到 7：跨天问「昨天约好的事」时，弱相关的那条常排在
    # 第 6~7 位，5 条正好被切掉。
    PERSONA_RAG_K = max(1, int(os.getenv("PERSONA_RAG_K", "7")))
except ValueError:
    PERSONA_RAG_K = 7
try:
    PERSONA_RAG_MAX_DISTANCE = float(
        os.getenv("PERSONA_RAG_MAX_DISTANCE", "0.66"))
except ValueError:
    PERSONA_RAG_MAX_DISTANCE = 0.66
try:
    PERSONA_RAG_TIMEOUT = float(os.getenv("PERSONA_RAG_TIMEOUT", "2.5"))
except ValueError:
    PERSONA_RAG_TIMEOUT = 2.5
try:
    PERSONA_RAG_INGEST_EVERY = max(
        1, int(os.getenv("PERSONA_RAG_INGEST_EVERY", "5")))
except ValueError:
    PERSONA_RAG_INGEST_EVERY = 5

# ─── 角色卡世界书（2026-11-07）──────────────────────────
#
# 酒馆卡的 `character_book` 自带 `token_budget`，实测两张卡是 500 / 512
# token（约 4 条）。我们把它当参考值而不是上限：本机 token 预算充足，而少
# 注一条就是「她不知道自己在哪」。
#
#   PERSONA_LORE_TOKENS  每轮世界书预算（默认 1200，约 2.4 倍于卡自带值）
#   PERSONA_LORE_RAG=0   关掉「关键词没命中时按语义补条目」那一路
PERSONA_LORE_TOKENS = max(0, int(os.getenv("PERSONA_LORE_TOKENS", "1200") or 1200))
PERSONA_LORE_RAG = os.getenv("PERSONA_LORE_RAG", "1").strip().lower() not in (
    "0", "false", "no", "off")

__all__ = ["rag_enabled"] + [
    name for name in globals()
    if name.isupper() and not name.startswith("_")
]

"""本地 cross-encoder 重排（2026-11-16）：在向量召回之后、MMR 之前精排。

为什么需要：向量召回是双塔（bi-encoder），把文档压成一个向量算相似度，
快但粗——同义词、否定、细粒度匹配经常排错。重排把「问题 + 候选文档」
拼成一对送进 cross-encoder 逐对精算相关性，精度高得多。本模块只做打分
和排序，多样性去重仍交给各链路已有的 MMR（persona_rag.mmr_select /
rag.vector_store 的 MMR），两者职责不重叠。

模型：BAAI/bge-reranker-base（XLM-RoBERTa base，278M 参数），从 ModelScope
下载到 `models/bge-reranker-base`（HF 直连被墙、gh-proxy 不通，魔搭可用）。
**强制 CPU**（device="cpu"）——本机显存要留给 ComfyUI，绝不抢卡。

降级（fail-open）：模型缺失 / 加载失败 / 打分异常 → 按原顺序返回，绝不
阻断检索。这是检索增强层，不是可用性闸门；模型没就绪时用户仍能用旧的
向量排序 + MMR。配置：
  RAG_RERANK_ENABLED   "0" 关闭（默认开）；模型缺失自动降级，不用手动关
  RAG_RERANK_MODEL     模型目录，默认项目根 models/bge-reranker-base
  RAG_RERANK_BATCH     每批打分条数，默认 16
"""
import logging
import os
import sys
import threading

from app.config import ROOT_DIR

log = logging.getLogger("rerank")

# 测试进程不加载 1GB 模型（和 persona_rag._TESTING 同一惯例）：unittest 环境
# 默认关掉重排，各业务测试沿用旧的向量排序行为；重排本身由 test_rerank.py 用
# 假模型验证。生产进程没有 unittest，默认开。
_TESTING = "unittest" in sys.modules

_HERE = ROOT_DIR
_MODEL_DIR = os.getenv(
    "RAG_RERANK_MODEL", os.path.join(_HERE, "models", "bge-reranker-base")).strip()
_BATCH = max(1, int(os.getenv("RAG_RERANK_BATCH", "16") or 16))

_MODEL = None
_LOCK = threading.Lock()
_FAILED = False          # 加载失败过就不再重试（避免每轮都吃冷启动开销）
_LOAD_MS = 0


def enabled():
    """开关：显式关掉 / 测试进程返回 False；模型缺失不在这里判，load 里降级。"""
    if _TESTING and not os.getenv("RAG_RERANK_ENABLED"):
        return False
    return os.getenv("RAG_RERANK_ENABLED", "1").strip() not in ("0", "false", "False")


def model_ready():
    """模型是否已经成功加载（操作页/测试用它判断重排是否在生效）。"""
    return _MODEL is not None


def _load():
    """懒加载 cross-encoder；失败记一次，之后直接降级。线程安全。"""
    global _MODEL, _FAILED, _LOAD_MS
    if _MODEL is not None:
        return _MODEL
    if _FAILED:
        return None
    with _LOCK:
        if _MODEL is not None or _FAILED:
            return _MODEL
        try:
            import time
            from sentence_transformers import CrossEncoder
            t = time.time()
            if not os.path.isdir(_MODEL_DIR):
                raise FileNotFoundError("模型目录不存在：%s" % _MODEL_DIR)
            m = CrossEncoder(_MODEL_DIR, device="cpu", max_length=512)
            _MODEL = m
            _LOAD_MS = int((time.time() - t) * 1000)
            log.info("[重排] 模型已加载（CPU，%dms）：%s", _LOAD_MS, _MODEL_DIR)
        except Exception as exc:                       # noqa: BLE001
            _FAILED = True
            log.warning("[重排] 模型加载失败，本进程不再重试，按原顺序返回：%s", exc)
        return _MODEL


def rerank(query, docs, top_n=None, text_key="content"):
    """按 query 对 docs 精排，返回**同结构**的 docs 列表（不改原对象）。

    docs 每项至少要有 `text_key` 字段（默认 content）；distance/metadata 等
    一并保留。top_n=None 返回全部（顺序已重排）；给了就截断。
    模型不可用时返回原列表的浅拷贝，调用方无需 try/except。
    """
    if not docs:
        return []
    if not enabled():
        return list(docs)
    model = _load()
    if model is None:
        return list(docs)

    pairs = []
    valid = []
    for i, d in enumerate(docs):
        if not isinstance(d, dict):
            continue
        text = str(d.get(text_key) or "").strip()
        if not text:
            continue
        pairs.append([str(query or ""), text[:1000]])
        valid.append(i)
    if not pairs:
        return list(docs)

    try:
        scores = model.predict(pairs, batch_size=_BATCH, show_progress_bar=False)
    except Exception as exc:                           # noqa: BLE001
        log.warning("[重排] 打分失败，按原顺序返回：%s", exc)
        return list(docs)

    try:
        scored = [(float(s), idx) for s, idx in zip(scores, valid)]
    except (TypeError, ValueError):
        log.warning("[重排] 分数解析失败，按原顺序返回")
        return list(docs)

    scored.sort(key=lambda x: x[0], reverse=True)
    out = [docs[idx] for _score, idx in scored]
    # 没有分数的条目（空内容 / 打分被跳过）保持原相对顺序排到末尾
    seen = {idx for _score, idx in scored}
    out.extend(d for i, d in enumerate(docs) if i not in seen)
    return out[:top_n] if top_n else out


def status():
    """给操作页看的诊断快照。"""
    return {
        "enabled": enabled(),
        "model_ready": model_ready(),
        "failed": _FAILED,
        "model_dir": _MODEL_DIR,
        "load_ms": _LOAD_MS,
        "batch": _BATCH,
    }

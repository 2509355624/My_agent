# -*- coding: utf-8 -*-
"""ChromaDB 持久化向量存储（按知识库名称隔离 collection）。"""

import re
import hashlib
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings


def safe_collection_name(kb_name: str) -> str:
    """生成安全的 collection 名（chroma 限制：3-63 字符，字母开头）。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(kb_name or "default"))
    safe = safe.lower()
    if not safe or not safe[0].isalpha():
        safe = "kb_" + safe
    if len(safe) > 63:
        suffix = hashlib.md5(safe.encode()).hexdigest()[:8]
        safe = safe[:54] + "_" + suffix
    return safe


class VectorStore:
    def __init__(self, persist_dir: str | Path):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )

    def list_collections(self) -> list[str]:
        """列出所有知识库 collection。"""
        return [c.name for c in self.client.list_collections()]

    def _collection(self, kb_name: str):
        name = safe_collection_name(kb_name)
        return self.client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def count(self, kb_name: str) -> int:
        col = self._collection(kb_name)
        return col.count()

    def upsert_entries(self, kb_name: str, entries: list[dict[str, Any]]) -> int:
        if not entries:
            return 0
        col = self._collection(kb_name)
        ids = []
        documents = []
        metadatas = []
        embeddings = []
        for entry in entries:
            doc_id = str(entry["id"])
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            meta = entry.get("metadata") or {}
            meta = {k: (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in meta.items()}
            ids.append(doc_id)
            documents.append(content)
            metadatas.append(meta)
            if entry.get("embedding") is not None:
                embeddings.append(entry["embedding"])
        if not ids:
            return 0
        kwargs = {"ids": ids, "documents": documents, "metadatas": metadatas}
        if embeddings and len(embeddings) == len(ids):
            kwargs["embeddings"] = embeddings
        col.upsert(**kwargs)
        return len(ids)

    def query(self, kb_name: str, query_embedding: list[float], k: int = 4) -> list[dict]:
        col = self._collection(kb_name)
        if col.count() == 0:
            return []

        # MMR: 先取候选池，再按多样性选 k 条
        fetch_n = min(max(k * 5, 20), col.count())
        result = col.query(
            query_embeddings=[query_embedding],
            n_results=fetch_n,
            include=["documents", "metadatas", "distances", "embeddings"],
        )

        candidates = []
        for i in range(len(result["ids"][0])):
            candidates.append({
                "id": result["ids"][0][i],
                "content": result["documents"][0][i],
                "metadata": result["metadatas"][0][i] or {},
                "distance": result["distances"][0][i],
                "embedding": result["embeddings"][0][i],
            })

        if len(candidates) <= k:
            for c in candidates:
                c.pop("embedding", None)
            return candidates

        # MMR 选择
        import numpy as np
        lambda_param = 0.6
        query_emb = np.array(query_embedding)

        selected = [candidates[0]]
        remaining = list(range(1, len(candidates)))

        while len(selected) < k and remaining:
            best_score = -1.0
            best_idx = remaining[0]
            for idx in remaining:
                relevance = 1.0 - candidates[idx]["distance"]
                max_sim = 0.0
                for sel in selected:
                    sim = float(np.dot(candidates[idx]["embedding"], sel["embedding"]))
                    if sim > max_sim:
                        max_sim = sim
                mmr_score = lambda_param * relevance - (1.0 - lambda_param) * max_sim
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = idx
            selected.append(candidates[best_idx])
            remaining.remove(best_idx)

        for s in selected:
            s.pop("embedding", None)
        return selected

    def delete_kb(self, kb_name: str) -> bool:
        name = safe_collection_name(kb_name)
        try:
            self.client.delete_collection(name)
            return True
        except Exception:
            return False

    def delete_entry(self, kb_name: str, entry_id: str) -> bool:
        return self.delete_entries(kb_name, [entry_id])

    def delete_entries(self, kb_name: str, entry_ids: list[str]) -> int:
        """批量删除指定 id 的条目（ChromaDB 原生批量删，非 for 循环）。

        返回实际删除条数；id 不存在会被 ChromaDB 静默忽略。
        """
        col = self._collection(kb_name)
        ids = [i for i in (entry_ids or []) if i]
        if not ids:
            return 0
        try:
            col.delete(ids=ids)
            return len(ids)
        except Exception:
            return 0

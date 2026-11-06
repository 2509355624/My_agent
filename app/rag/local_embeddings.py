# -*- coding: utf-8 -*-
"""本地 BGE-M3 向量模型。"""

import os
import sys
from typing import List

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
DEFAULT_MODEL = "BAAI/bge-m3"


def _resolve_model_path(model_name: str) -> str:
    if os.path.isdir(model_name):
        return model_name
    try:
        from modelscope import snapshot_download
        path = snapshot_download(model_name)
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return model_name


class LocalEmbeddings:
    def __init__(self, model_name: str = DEFAULT_MODEL, device: str | None = None,
                 batch_size: int = 16):
        from sentence_transformers import SentenceTransformer
        import torch

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.batch_size = batch_size

        model_path = _resolve_model_path(model_name)
        self.model = SentenceTransformer(model_path, device=device)
        print(
            f"[LocalEmbeddings] {model_name} ready on {device}, "
            f"dim={self.model.get_sentence_embedding_dimension()}",
            file=sys.stderr,
            flush=True,
        )

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=len(texts) > 8,
        )
        return vectors.astype("float32").tolist()

    def embed_query(self, text: str) -> List[float]:
        # 用**列表**形式编码（2026-11-06）：某些模型/版本下 `encode(str)` 会抛
        # `TextEncodeInput must be Union[...]`，而 `encode([str])` 一直正常
        # （embed_documents 就是这么调的）。取第 0 条即可，语义完全一样。
        try:
            vector = self.model.encode(
                [QUERY_INSTRUCTION + str(text)],
                normalize_embeddings=True,
                convert_to_numpy=True,
            )[0]
        except Exception:
            vector = self.model.encode(
                QUERY_INSTRUCTION + str(text),
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
        return vector.astype("float32").tolist()

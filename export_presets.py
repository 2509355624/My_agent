# -*- coding: utf-8 -*-
"""
导出某个 QQ 的「文生图预设库」为 JSON（含向量，跨机可重导入）。

用法（在本机、qq_bot 停止时运行最安全，避免和 RAG daemon 抢同一个 Chroma 目录）：
    python export_presets.py <QQ号> [chroma目录] [输出json]
例：
    python export_presets.py 546587874
    python export_presets.py 546587874 D:/AI/agent_my_test/vector_store/chroma presets_546587874.json

注意：Chroma 把 collection 映射成 UUID 目录，无法按名字直接拷贝单个集合，
所以走「导出 JSON -> 另一台导入」才是干净、只迁一个人的做法。
"""
import json
import os
import re
import sys

import chromadb
from chromadb.config import Settings


def safe_collection_name(kb):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(kb or "default"))
    safe = safe.lower()
    if not safe or not safe[0].isalpha():
        safe = "kb_" + safe
    if len(safe) > 63:
        import hashlib
        safe = safe[:54] + "_" + hashlib.md5(safe.encode()).hexdigest()[:8]
    return safe


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    qq = sys.argv[1]
    here = os.path.dirname(os.path.abspath(__file__))
    default_chroma = os.path.join(here, "vector_store", "chroma")
    chroma_dir = sys.argv[2] if len(sys.argv) > 2 else default_chroma
    out = sys.argv[3] if len(sys.argv) > 3 else "presets_%s.json" % qq

    col_name = safe_collection_name("presets_" + qq)
    client = chromadb.PersistentClient(
        path=chroma_dir, settings=Settings(anonymized_telemetry=False)
    )
    try:
        col = client.get_collection(col_name)
    except Exception:
        print("未找到集合 %s（QQ %s 可能没有预设，或 chroma 目录不对）" % (col_name, qq))
        sys.exit(1)

    res = col.get(include=["documents", "metadatas", "embeddings"])
    raw_emb = res.get("embeddings")
    has_emb = raw_emb is not None and len(raw_emb) > 0
    entries = []
    for i, eid in enumerate(res["ids"]):
        emb = None
        if has_emb:
            e = raw_emb[i]
            emb = e.tolist() if hasattr(e, "tolist") else list(e)
        entries.append({
            "id": eid,
            "content": res["documents"][i],
            "metadata": (res["metadatas"][i] or {}),
            "embedding": emb,
        })

    json.dump(
        {"qq": qq, "collection": col_name, "entries": entries},
        open(out, "w", encoding="utf-8"),
        ensure_ascii=False,
    )
    print("已导出 %d 条预设 -> %s" % (len(entries), out))


if __name__ == "__main__":
    main()

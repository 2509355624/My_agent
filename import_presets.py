# -*- coding: utf-8 -*-
"""
把 export_presets.py 导出的 JSON 导入到本机 Chroma（按原向量写入，不需重算 embedding）。

用法（在另一台机器、vector_store/chroma 所在项目根下运行）：
    python import_presets.py <json文件> [chroma目录]
例：
    python import_presets.py presets_546587874.json
    python import_presets.py presets_546587874.json D:/AI/agent_my_test/vector_store/chroma

导入后，7874 在另一台私聊里用 preset_list / preset_search 即可看到原有预设。
"""
import json
import os
import sys

import chromadb
from chromadb.config import Settings


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    inp = sys.argv[1]
    here = os.path.dirname(os.path.abspath(__file__))
    default_chroma = os.path.join(here, "vector_store", "chroma")
    chroma_dir = sys.argv[2] if len(sys.argv) > 2 else default_chroma

    data = json.load(open(inp, encoding="utf-8"))
    col_name = data["collection"]
    entries = data.get("entries") or []
    if not entries:
        print("JSON 里没有预设条目，跳过。")
        sys.exit(1)

    client = chromadb.PersistentClient(
        path=chroma_dir, settings=Settings(anonymized_telemetry=False)
    )
    col = client.get_or_create_collection(
        col_name, metadata={"hnsw:space": "cosine"}
    )

    ids = [e["id"] for e in entries]
    docs = [e["content"] for e in entries]
    metas = [e["metadata"] for e in entries]
    embs = [e["embedding"] for e in entries if e.get("embedding")]

    if len(embs) == len(ids):
        col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
        print("已导入 %d 条预设（含原向量，零重算）-> %s" % (len(ids), col_name))
    else:
        # 没带向量：交给本机 embedding 函数重算（要求集合配了 embedding 函数）
        col.upsert(ids=ids, documents=docs, metadatas=metas)
        print("已导入 %d 条预设（未带向量，已用本机 embedding 重算）-> %s"
              % (len(ids), col_name))


if __name__ == "__main__":
    main()

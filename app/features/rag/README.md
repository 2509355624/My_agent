# rag

负责：文档解析、切片、向量/关键词检索、重排、引用结果。

不负责：页面样式、QQ 推送、模型 provider 实现。

目录：

- `lab.py`：`/api/raglab/*` 文档问答接口、切片、引用。
- `rag_client.py` / `rag_daemon.py`：常驻向量进程与客户端。
- `local_embeddings.py`：本地 embedding。
- `vector_store.py`：Chroma 向量库与 collection 名净化。
- `rerank.py`：cross-encoder 重排，模型缺失时 fail-open。

状态：已从 `app/rag/`、`app/rag_lab.py`、`app/rerank.py` 迁入并接线；
URL 与检索行为不变。`lab.py` 约 1,049 行，暂按 1,000 行容忍口径保留，
下一步再按“上传/切片/检索/回答”拆。

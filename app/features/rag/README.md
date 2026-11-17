# rag

负责：文档解析、切片、向量/关键词检索、重排、引用结果。

不负责：页面样式、QQ 推送、模型 provider 实现。

迁移起点：`rag/` 包、`rag_lab.py`、`rerank.py`。完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：空包，未接线。先建 `service.py` 的 `ingest/query` 两个用例，再抽 `repository.py` 存切片。

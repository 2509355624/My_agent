# 2026-11-17 `rag` feature 迁移

## 目标

把文档问答与向量检索从 `app/` 根目录收进 `app/features/rag/`：
客户端、daemon、本地 embedding、向量库、重排、文档问答接口。

不改变 `/api/raglab/*`、`/rag` 页面和检索行为。

## 映射

| 旧文件 | 新文件 | 职责 |
|---|---|---|
| `app/rag/rag_client.py` | `app/features/rag/rag_client.py` | daemon 客户端 |
| `app/rag/rag_daemon.py` | `app/features/rag/rag_daemon.py` | 常驻向量进程 |
| `app/rag/local_embeddings.py` | `app/features/rag/local_embeddings.py` | 本地 embedding |
| `app/rag/vector_store.py` | `app/features/rag/vector_store.py` | Chroma 向量库 |
| `app/rag_lab.py` | `app/features/rag/lab.py` | 文档问答接口与切片/引用 |
| `app/rerank.py` | `app/features/rag/rerank.py` | cross-encoder 重排 |

## 需要修正的相对路径

- `rag_client.py`：`from ..config` → `from app.config`（包层级变深）。
- `rag_daemon.py`：项目根从 `parent.parent` 改成 `parent.parent.parent`；
  `app.rag.*` 导入改成 `app.features.rag.*`。
- `lab.py`：`LAB_DATA_DIR` 的 `__file__` 两层 dirname 改成项目根常量。
- `rerank.py`：模型目录默认值改成项目根。

## 调用方

`app/main.py`、`app/persona_rag.py`、`app/lora_lib.py`、`app/tools/rag/*`、
`tests/test_rag_lab.py`、`tests/test_rag_utils.py`、`tests/test_rerank.py`。

## 验证

1. `compileall -q app`
2. `test_rag_lab.py`、`test_rag_utils.py`、`test_rerank.py`、`test_persona_rag.py`
3. `/api/raglab/...` 与 `/rag` 冒烟
4. `git diff --check`
5. 提交并推送

## 不做

- 不拆 `lab.py`（1,049 行，按 1,000 行容忍口径先保持完整）。
- 不改切片、召回、重排算法。

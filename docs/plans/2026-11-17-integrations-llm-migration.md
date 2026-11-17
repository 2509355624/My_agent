# 2026-11-17 `integrations`：LLM 客户端迁移

## 目标

`app/llm.py` → `app/integrations/llm/__init__.py`

整包迁移保持模块对象不变：`from app.integrations import llm` 拿到的就是包本身，
`mock.patch("app.integrations.llm.xxx")` 与实现里的全局名是同一个对象。

## 调用方

`persona.py`、`persona_api.py`、`novel.py`、`main.py`、`direct_gen.py`、
`comic_agent.py`、`features/chat/*`、`features/comic/*`、`features/persona/*`、
`features/rag/lab.py`、`features/card/service.py` 及约 15 个测试。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_llm_chain.py` 66/66、`test_llm_stream.py` 31/31、`test_core_config_llm.py`
6/6、`test_persona.py` 379/379、`test_qq.py` 199/199；`test_custom_model.py`
18 条中 3 failures（缺 `data/custom_providers.json`，基线）。

# 2026-11-17 core：用量与模型目录迁移

## 目标

- `app/usage.py` → `app/core/usage.py`
- `app/provider_store.py` → `app/core/provider_store.py`
- `app/model_catalog.py` → `app/core/model_catalog.py`

三者都是跨业务共享的基础设施：用量账本、自定义中转站持久化、模型清单缓存，
不属于任何单个 feature。

## 调用方

`agent.py`、`llm.py`、`vision.py`、`main.py`、`features/chat/{interject,longterm,memory}.py`、
`features/persona/billing.py`、`features/qq/bill.py`、`core/config/llm.py`；
测试 `test_usage.py`、`test_window.py`、`test_llm_stream.py`、`test_qq_bill.py`、
`test_custom_providers.py`、`test_model_catalog.py`。

## 验证

`compileall`（app + tests）。
`test_usage.py` 15/15、`test_model_catalog.py` 18/18、
`test_custom_providers.py` 32/32、`test_qq_bill.py` 8/8、`test_window.py` 39/39。

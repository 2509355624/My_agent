# 2026-11-17 `persona` feature 第三轮迁移

## 目标

把角色长期记忆收进角色域：

- `app/persona_rag.py` → `app/features/persona/rag.py`

## 调用方

`app/main.py`（预热）、`app/persona.py`、`app/novel.py`、`app/showcase_api.py`、
`app/features/admin/routes.py`；测试 `test_persona.py`、`test_persona_rag.py`、
`test_novel.py`、`test_showcase.py`。

## 验证

`compileall`（app + tests）、`test_persona_rag.py` 52/52、`test_persona.py` 379/379、
`test_showcase.py` 36/36；`test_novel.py` 仍 6 failures + 7 errors，与迁移前逐条一致
（写作提示词与 UI 接线基线）；`/api/status` 200。

## 不做

- 不改 collection 命名、入库节奏、召回阈值和回填逻辑。
- 不搬 `persona.py`、`persona_api.py`。

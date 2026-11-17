# 2026-11-17 `persona` feature 第一轮迁移

## 目标

把角色计费收进角色域：

- `app/persona_billing.py` → `app/features/persona/billing.py`

## 调用方

`app/llm.py`、`app/persona_api.py`、`app/features/admin/routes.py`、
`app/features/qq/bill.py`；测试 `test_persona_billing.py`、`test_qq_bill.py`。

## 验证

`compileall`、`test_persona.py` 379/379、`test_qq_bill.py` 8/8；
`test_persona_billing.py` 41 条中 1 条既有 UI 断言失败（`used.kind === "paid"`），
与计费逻辑无关；`/api/status` 200。

## 不做

- 不搬 `persona.py`、`persona_api.py`、`persona_rag.py`。
- 不改价格表路径、余额文件、高峰/闲时规则和充值语义。

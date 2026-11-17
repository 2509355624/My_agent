# 2026-11-17 `showcase` feature：展示页迁移

## 目标

- `app/showcase_api.py` → `app/features/showcase/routes.py`
- `app/showcase_prompts.py` → `app/features/showcase/prompts.py`

展示页聚合接口和提示词模板同属展示域，URL/响应字段不变。

## 调用方

`app/main.py` 注册 `showcase_api`（保留别名）；`routes.py` 内部引用
`showcase_prompts`（保留别名）；测试 `test_showcase.py`。

## 验证

`compileall`（app + tests）；`test_showcase.py` 36/36。

# 2026-11-17 `persona` feature 第五轮迁移

## 目标

把角色道具收进角色域：

- `app/items.py` → `app/features/persona/items.py`

## 调用方

`app/persona.py`、`app/persona_api.py`；测试 `test_affection_v2_adult.py`。

## 验证

`compileall`（app + tests）、`test_persona.py` 379/379、
`test_affection_v2_adult.py` 167/167、`/api/status` 200。

## 不做

- 不改道具表、增减规则和 `items` 事件文案。

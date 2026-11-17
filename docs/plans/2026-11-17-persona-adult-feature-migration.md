# 2026-11-17 `persona` feature 第二轮迁移

## 目标

把成人向状态条结算器收进角色域：

- `app/affection_v2_adult.py` → `app/features/persona/adult.py`

## 调用方

`app/persona.py`、`app/affection.py`；测试 `test_affection_v2_adult.py`、
`test_affection.py`。

## 验证

`compileall`、`test_affection_v2_adult.py` 167/167、`test_affection.py` 176/176、
`test_persona.py` 379/379、`/api/status` 200。函数内导入的缩进已同步修正。

## 不做

- 不改状态轴、闸门、clamp、回落和 `config/affection_v2_adult.json` 结构。

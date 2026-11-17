# 2026-11-17 `persona` feature 第六轮迁移

## 目标

把角色成就收进角色域：

- `app/achievements.py` → `app/features/persona/achievements.py`

## 调用方

`app/affection.py`。

## 验证

`compileall`（app + tests）、`test_affection.py` 176/176、`/api/status` 200。

## 不做

- 不改成就条件、解锁文案和存档字段。

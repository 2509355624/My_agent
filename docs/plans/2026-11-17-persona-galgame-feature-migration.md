# 2026-11-17 `persona` feature 第八轮迁移

## 目标

把 Galgame 状态/回合收进角色域：

- `app/galgame.py` → `app/features/persona/galgame.py`

## 调用方

`app/qq_bot.py`；测试 `test_galgame.py`。

## 验证

`compileall`（app + tests）、`test_galgame.py` 58/58、`/api/status` 200。

## 不做

- 不改回合推进、阶段和场景选择。

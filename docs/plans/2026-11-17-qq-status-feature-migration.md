# 2026-11-17 `qq` feature 第三轮迁移

## 目标

把 QQ 状态快照收进 QQ 域：

- `app/qq_status.py` → `app/features/qq/status.py`

## 调用方

`app/main.py`（`/api/status` 函数内导入）、`app/qq_bot.py` 的状态线程；测试
`test_qq_status.py`、`test_qq.py`。

## 验证

`compileall`、`test_qq_status.py` 11/11、`test_qq.py` 199/199、`/api/status` 200。

## 不做

- 不搬 `qq_api.py`、`qq_bot.py`。
- 不改 `state/qq_status.json` 字段、原子替换和 stale 判定。

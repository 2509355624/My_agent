# 2026-11-17 `qq` feature 第二轮迁移

## 目标

把 QQ 会话名字缓存收进 QQ 域：

- `app/qq_names.py` → `app/features/qq/names.py`

## 调用方

`app/main.py`、`app/qq_bot.py`、`app/qq_status.py`；测试 `test_qq_names.py`、
`test_interject.py`。

## 验证

`compileall`、`test_qq_names.py` 9/9；`test_interject.py` 57 条中 3 failures + 2 errors
（假 `_Runner.submit` 不接 `sender_uin`、日志断言），与名字缓存导入无关，属既有基线；
`/api/status` 200。

## 不做

- 不搬 `qq_api.py`、`qq_bot.py`。
- 不改缓存文件 `state/qq_names.json`、刷新间隔和 `name_for` 回退规则。

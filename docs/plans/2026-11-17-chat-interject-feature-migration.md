# 2026-11-17 `chat` feature：主动插话迁移

## 目标

`app/interject.py` → `app/features/chat/interject.py`

主动插话判定属于对话域，不是 QQ 协议细节。

## 调用方

`app/qq_bot.py`；测试 `test_interject.py`。

## 验证

`compileall`（app + tests）；`test_interject.py` 57 条中 3 failures + 2 errors，
与迁移前一致（假 `_Runner.submit` 不接 `sender_uin`、日志断言）。

# 2026-11-17 `qq` feature：掉线通知迁移

## 目标

`app/notify.py` → `app/features/qq/notify.py`

掉线/扫码通知是 QQ 接入链路的旁路通道（PushPlus），不属于对话域，也不属于
通用 `core`。

## 调用方

`app/watchdog.py`（两层导入都改）、`app/features/qq/status.py`、`app/qq_bot.py`
的多行 `from app import (...)`；测试 `test_notify.py`、`test_qq.py`。

## 验证

`compileall`（app + tests）；`test_notify.py` 39/39、`test_qq.py` 199/199。

# 2026-11-17 `qq` feature：OneBot 接口层迁移

## 目标

`app/qq_api.py` → `app/features/qq/api.py`

QQ 的 NapCat/OneBot HTTP 调用、会话上下文、发送与日志都在 `qq` 域；模块对象
通过 `from app.features.qq import api as qq_api` 保持别名不变。

## 调用方

`qq_bot.py`、`image_jobs.py`、`main.py`、`workers/watchdog.py`、`gateways/friend_auth.py`、
`features/*`、`tools/*`；测试 20 余个文件，含 `test_qq.py`、`test_qq_api_log.py`、
`test_qq_api_robust.py`、`test_confirm_gate.py`、`test_image_audit.py`。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_qq.py` 199/199、`test_qq_api_log.py` 9/9、`test_qq_api_robust.py` 31/31、
`test_confirm_gate.py` 29/29、`test_image_audit.py` 73/73、`test_qq_names.py` 9/9、
`test_image_quota.py` 62/62；`test_sessions_admin.py` 103 条中 2 failures（远程
管理开关 403 基线）。

# 2026-11-17 `core`：取消信号与日志初始化迁移

## 目标

- `app/cancel.py` → `app/core/cancel.py`
- `app/logsetup.py` → `app/core/logsetup.py`

两者都是跨域基础设施：`agent`、`llm`、`image_jobs`、`comic`、`gen_api`、
`tools/normal/generate_image` 都依赖取消信号；`main`、`qq_bot`、`watchdog`
都依赖日志初始化。放 `core` 比放某个 feature 更符合依赖方向。

## 调用方

`from app.cancel import` 改为 `from app.core.cancel import`，涉及
`agent.py`、`comic.py`、`gen_api.py`、`image_jobs.py`、`llm.py`、
`tools/normal/generate_image.py` 与两个测试；`qq_bot.py` 的多行
`from app import (...)` 里把 `logsetup` 拆出到 `from app.core import logsetup`；
`watchdog.__main__` 同步改。

## 验证

`compileall`（app + tests）；`test_cancel.py` 16/16；`test_logsetup.py` 9/9；
`test_watchdog.py` 62 条中 1 条失败，仍是既有启动命令断言。

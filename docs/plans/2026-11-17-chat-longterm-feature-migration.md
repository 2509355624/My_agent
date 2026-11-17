# 2026-11-17 `chat` feature：长期记忆迁移

## 目标

`app/longterm.py` → `app/features/chat/longterm.py`

长期记忆只服务对话：把滚出窗口的轮次摘要成记忆、按 agent/群召回。不是通用存储。

## 调用方

`app/memory.py`（函数内延迟导入）、`app/recent.py`、`app/qq_bot.py`；
测试 `test_longterm.py`、`test_recent.py`、`test_window.py`。

## 验证

`compileall`（app + tests）；`test_longterm.py` 19/19、`test_recent.py` 40/40、
`test_window.py` 39/39、`test_qq.py` 199/199。

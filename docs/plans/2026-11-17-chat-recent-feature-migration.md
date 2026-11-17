# 2026-11-17 `chat` feature：近期上下文迁移

## 目标

`app/recent.py` → `app/features/chat/recent.py`

`recent` 管的是「最近几轮对话窗口 + 滚动后交给长期记忆」，属于对话域。

## 调用方

`app/interject.py`、`app/qq_bot.py`；测试 `test_recent.py`、`test_longterm.py`。

## 验证

`compileall`（app + tests）；`test_recent.py` 40/40、`test_longterm.py` 19/19、
`test_qq.py` 199/199。

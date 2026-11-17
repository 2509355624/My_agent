# 2026-11-17 `chat` feature：会话记忆迁移

## 目标

`app/memory.py` → `app/features/chat/memory.py`

会话历史读写、窗口裁剪、token 估算都只服务对话，不是通用持久化。

## 调用方

`app/agent_prompt.py`、`app/agent.py`、`app/main.py`、`app/qq_bot.py`；
测试 `test_memory.py`、`test_prompt_sync.py`、`test_recent.py`、`test_web_api.py`、
`test_window.py`、`test_session_prompt.py`。

## 验证

`compileall`（app + tests）；`test_memory.py` 43/43、`test_window.py` 39/39、
`test_recent.py` 40/40、`test_session_prompt.py` 35/35。

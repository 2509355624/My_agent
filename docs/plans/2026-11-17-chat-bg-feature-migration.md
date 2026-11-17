# 2026-11-17 `chat` 背景图迁移

## 目标

先把对话背景图从 `app/chat_bg.py` 收进 `app/features/chat/background.py`，
让“对话背景”有明确归属；对话主循环暂不搬。

## 映射

| 旧文件 | 新文件 |
|---|---|
| `app/chat_bg.py` | `app/features/chat/background.py` |

## 调用方

`app/main.py`、`app/persona_api.py`、`app/persona_admin_api.py`、
`tests/test_chat_bg.py`。

## 验证

`compileall`、`test_chat_bg.py`、`/api/status` 与应用导入。

## 不做

- 不搬 `agent.py` / `memory.py` / `longterm.py` 等对话主循环。
- 不改背景图 URL、存储路径、请求字段。

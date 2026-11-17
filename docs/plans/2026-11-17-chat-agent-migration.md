# 2026-11-17 `chat` feature：Agent 主循环迁移

## 目标

`app/agent.py` → `app/features/chat/agent.py`

LLM → 工具调用 → 结果回填的对话主循环只服务聊天/QQ/漫画 Agent，归 `chat`。

## 调用方

`main.py`、`qq_bot.py`、`comic_agent.py`、`features/chat/search_agent.py`；
测试 `test_agent_loop.py`、`test_agent_dedup.py`、`test_tool_protocol.py`、
`test_cancel.py`、`test_vision.py`、`test_vision_prompt.py`、`test_web_api.py`、
`test_image_claim.py`、`test_image_receipt.py`、`test_recent.py`。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_agent_loop.py` 49/49、`test_agent_dedup.py` 22/22、`test_tool_protocol.py`
48/48、`test_cancel.py` 16/16、`test_vision.py` 84/84、`test_qq.py` 199/199、
`test_persona.py` 379/379。

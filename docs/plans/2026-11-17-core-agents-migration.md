# 2026-11-17 `core`：Agent 注册表迁移

## 目标

`app/agents.py` → `app/core/agents.py`

`agents/<id>/agent.json`、`prompt.md`、`session.jsonl` 的读取、热加载、工具/Skills
白名单、配额策略是跨业务共享的 Agent 配置层，归 `core`。

## 调用方

`agent.py`、`main.py`、`persona.py`、`qq_bot.py`、`moss_bot.py`、
`features/chat/*`、`features/image/*`、`features/qq/stickers.py`、
`integrations/vision.py`、`tools/normal/generate_image.py` 及约 40 个测试。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_agents.py` 59/59、`test_agent_loop.py` 49/49、`test_agent_dedup.py` 22/22、
`test_confirm_gate.py` 29/29、`test_memory.py` 43/43、`test_qq.py` 199/199、
`test_persona.py` 379/379、`test_image_audit.py` 73/73。

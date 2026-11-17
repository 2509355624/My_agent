# chat

负责：对话循环、提示词、短期/长期记忆、模型路由、识图、搜索工具、插话。

不负责：5174/5175/5176 网关、静态页、生图落盘、QQ 协议细节。

迁移起点：`agent.py`、`agent_prompt.py`、`memory.py`、`longterm.py`、`llm.py`、
`vision.py`、`search_agent.py`、`interject.py`、`chat_bg.py`、`confirm_gate.py`、`cancel.py`。
完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：空包，未接线。迁移时先建 `routes.py` + `service.py`，需要落盘再抽 `repository.py`。

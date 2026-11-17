# chat

负责：对话循环、提示词、短期/长期记忆、模型路由、识图、搜索工具、插话。

不负责：5174/5175/5176 网关、静态页、生图落盘、QQ 协议细节。

迁移起点：`agent.py`、`agent_prompt.py`、`memory.py`、`longterm.py`、`llm.py`、
`vision.py`、`search_agent.py`、`interject.py`、`background.py`、`confirm_gate.py`、`cancel.py`。
完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`background.py` 已从 `chat_bg.py` 迁入并接线；`longterm.py`（长期记忆摘要/召回）、
`recent.py`（近期上下文窗口）、`interject.py`（主动插话判定）、
`confirm_gate.py`（工具执行前确认）、`memory.py`（会话读写/裁剪/预算）已迁入，`memory.py` / `qq_bot.py` / `interject.py`
通过 feature 路径调用。对话主循环仍暂留根目录，
下一步再按 `routes.py` + `service.py` 拆 `agent.py` / `memory.py`。

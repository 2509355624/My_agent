"""默认 agent 配置。"""

import os

# 多 agent：每个 agent 一个自包含目录（agents/<id>/ 里放 agent.json 配置、
# prompt.md 人设、session.jsonl 会话）。会话文件路径由 app/agents.py 解析，
# 不再有「全局单会话文件」这种东西。
DEFAULT_AGENT_ID = os.getenv("DEFAULT_AGENT_ID", "main")
__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")]

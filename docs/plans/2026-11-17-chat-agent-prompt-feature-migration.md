# 2026-11-17 `chat` feature：系统提示词迁移

## 目标

`app/agent_prompt.py` → `app/features/chat/agent_prompt.py`

系统提示词、工具清单、技能清单、状态栏只服务对话回合，属于 chat 域。

## 调用方

`app/agent.py`、`app/main.py`、`app/qq_bot.py`；测试 `test_comfy_status.py`、
`test_image_channels.py`、`test_image_i2i_gate.py`、`test_image_jobs.py`、
`test_image_lora.py`、`test_prompt_sync.py`。

## 验证

`compileall`（app + tests）；`test_image_lora.py` 32 条中 1 failure + 2 errors、
`test_prompt_sync.py` 33 条中 5 failures + 4 errors，与迁移前一致（缺
`agents/qq/image_guide.md` 等资源）。

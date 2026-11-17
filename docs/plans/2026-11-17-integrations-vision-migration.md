# 2026-11-17 `integrations`：识图适配迁移

## 目标

`app/vision.py` → `app/integrations/vision.py`

图片下载、MIME 嗅探、多模态描述、审核候选都是外部模型/网络适配，不写业务
策略，归 `integrations` 而不是 `chat`。

## 调用方

`agent.py`、`direct_gen.py`、`main.py`、`qq_bot.py`、`features/image/audit.py`、
`features/image/comfy_src.py`、`features/qq/stickers.py`、`features/rag/lab.py`；
测试 `test_vision.py`、`test_vision_prompt.py`、`test_image_audit.py`、
`test_qq.py`、`test_agent_loop.py`、`test_direct_gen.py`、`test_llm_chain.py`、
`test_logsetup.py`、`test_image_i2i.py`。

## 验证

`compileall`（app + tests）。
`test_vision.py` 84/84、`test_vision_prompt.py` 24/24、`test_image_audit.py` 73/73、
`test_qq.py` 199/199。顺带修复 `test_vision.py` 的 `app.usage.record` 补丁路径
（`usage` 已先在 core 迁移中移动）。

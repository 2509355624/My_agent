# image

负责：生图提交、渠道选择、队列、审核、落盘、额度、LoRA、预设。

不负责：漫画分镜/编剧、QQ 消息、管理页 UI、进程启动。

迁移起点：`image_jobs.py`、`image_audit.py`、`image_log.py`、`image_migrate.py`、
`image_out.py`、`image_quota.py`、`image_review.py`、`direct_gen.py`、`gen_api.py`、
`gen_params.py`、`gen_steps.py`、`comfy_*.py`、`lora_*.py`、`anima_random.py`、`nai.py`、
`outfit_api.py`。完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：空包，未接线。`comfy_*` 同时涉及 `integrations/comfy`，先按“谁调用”拆，不按文件名切。

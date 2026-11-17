# image

负责：生图提交、渠道选择、队列、审核、落盘、额度、LoRA、预设。

不负责：漫画分镜/编剧、QQ 消息、管理页 UI、进程启动。

迁移起点：`image_jobs.py`、`image_audit.py`、`image_log.py`、`image_migrate.py`、
`image_out.py`、`image_quota.py`、`image_review.py`、`direct_gen.py`、`gen_api.py`、
`gen_params.py`、`gen_steps.py`、`comfy_*.py`、`lora_*.py`、`anima_random.py`、`nai.py`、
`outfit_api.py`。完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`log.py`（生图编号 → 提示词账本）、`out.py`（发图副本/临时目录）、
`quota.py`（私聊额度）、`audit.py`（发图前审核）已迁入并接线；`image_jobs.py` 仍留在
根目录，它的测试直接 patch 内部函数，硬拆前要先定测试兼容策略。

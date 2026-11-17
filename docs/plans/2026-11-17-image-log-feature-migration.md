# 2026-11-17 `image` feature 第一轮迁移

## 目标

先把生图域里依赖最少的 `image_log.py` 归位：

- `app/image_log.py` → `app/features/image/log.py`

`image_jobs.py` 暂不搬：`tests/test_image_jobs.py` 大量直接 patch
`image_jobs._queue_prompt` / `wait_done` / `_notice` 等内部函数，先搬会改变 patch
目标，测试会大面积失败。等测试兼容策略定下来再拆队列。

## 调用方

`app/image_jobs.py`、`app/direct_gen.py`、`app/qq_bot.py`、`app/image_review.py`、
`app/tools/normal/recall_image.py`；测试 `test_image_log.py`、`test_image_jobs.py`、
`test_image_review.py`、`test_job_ledger.py`、`test_recall_image.py`。

## 验证

`compileall`、`test_image_log.py`、`test_recall_image.py`、`test_job_ledger.py`、
`test_image_review.py`、`test_qq.py`，外加 `/api/status`、`/api/providers`、`/card`。

`test_image_jobs.py` 的既有红项（`anima_draw` 假实现不接新参数、缺 `skills/` 数据、
`COMFY_WAIT_UP` 环境值）在迁移前后一致，不作为本次新增失败。

## 不做

- 不搬 `image_jobs.py`、`image_quota.py`、`image_out.py`、`image_audit.py`、
  `image_review.py` 的实现。
- 不改账本文件格式、编号前缀、接口字段。

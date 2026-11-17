# 2026-11-17 `image` feature 第四轮迁移

## 目标

把发图前的 NSFW 审核收进生图域：

- `app/image_audit.py` → `app/features/image/audit.py`

## 调用方

`app/main.py`、`app/showcase_api.py`、`app/image_jobs.py`、`app/moss_bot.py`、
`app/outfit_api.py`、`app/qq_bot.py`、`app/features/card/bot.py`；测试
`test_image_audit.py`、`test_image_jobs.py`、`test_image_review.py`、
`test_moss_bot.py`、`test_outfit_api.py`。

## 验证

`compileall`、`test_image_audit.py` 73/73、`test_image_review.py` 44/44、
`test_moss_bot.py` 67/67、`test_card_bot.py` 33/33、`test_qq.py` 199/199、
`test_showcase.py` 36/36；`test_outfit_api.py` 50 条中 3 条缺
`skills/_qwen_outfit/workflow.json`，属既有数据基线。

## 不做

- 不搬 `image_jobs.py`、`image_review.py`。
- 不改审核开关、提示词、fail-closed 口径。

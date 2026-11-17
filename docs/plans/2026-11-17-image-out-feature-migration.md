# 2026-11-17 `image` feature 第二轮迁移

## 目标

把发图前的本地副本与临时目录收进生图域：

- `app/image_out.py` → `app/features/image/out.py`

`image_jobs.py` 仍不搬，原因同上一轮：测试直接 patch 模块内部函数。

## 调用方

`app/image_jobs.py`、`app/gen_api.py`、`app/qq_bot.py`、`app/moss_bot.py`、
`app/vision.py`；测试 `test_image_out.py`、`test_image_audit.py`、`test_image_jobs.py`、
`test_moss_bot.py`、`test_qq.py`、`test_vision.py`。

## 验证

`compileall`、`test_image_out.py` 21/21、`test_image_audit.py` 73/73、
`test_moss_bot.py` 67/67、`test_vision.py` 84/84、`test_qq.py` 199/199；
`test_image_jobs.py` 的 23 failures + 16 errors 与迁移前逐条一致，均为既有基线。

## 不做

- 不搬 `image_jobs.py`、`image_quota.py`、`image_audit.py`、`image_review.py`。
- 不改 `prepare_for_send` 的 jpg/png 策略、临时目录、URL 字段。

# 2026-11-17 `image` feature 第五轮迁移

## 目标

把人工二审队列收进生图域：

- `app/image_review.py` → `app/features/image/review.py`

## 调用方

`app/main.py`（管理页接口）、`app/features/image/audit.py`（拦截后入队）；测试
`test_image_review.py`、`test_image_audit.py`。

## 验证

`compileall`、`test_image_review.py` 44/44、`test_image_audit.py` 73/73、
`/api/status` 200。

## 不做

- 不搬 `image_jobs.py`。
- 不改复核记录格式、补发行为、管理页字段。

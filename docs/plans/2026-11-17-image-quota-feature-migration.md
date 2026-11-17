# 2026-11-17 `image` feature 第三轮迁移

## 目标

把私聊生图额度账本收进生图域：

- `app/image_quota.py` → `app/features/image/quota.py`

## 调用方

`app/agents.py`（额度判断/展示）、`app/image_jobs.py`（失败退款）、
`app/tools/normal/generate_image.py`；测试 `test_image_quota.py`。

## 验证

`compileall`、`test_image_quota.py` 62/62、`test_persona.py` 379/379、
`test_qq.py` 199/199。

## 不做

- 不搬 `image_jobs.py`、`image_audit.py`、`image_review.py`。
- 不改额度 JSON 结构、重置时间、退款口径。

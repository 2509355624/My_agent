# 2026-11-17 `image` feature：任务台账迁移

## 目标

把任务/重抽台账收进生图域：

- `app/job_ledger.py` → `app/features/image/job_ledger.py`

## 调用方

`app/image_jobs.py`；测试 `test_job_ledger.py`。

## 验证

`compileall`（app + tests）、`test_job_ledger.py` 18/18、`/api/status` 200。

## 不做

- 不改台账 JSON 结构、重抽判定和清理策略。

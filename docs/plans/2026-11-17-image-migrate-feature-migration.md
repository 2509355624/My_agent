# 2026-11-17 `image` feature 第六轮迁移

## 目标

把旧图目录迁移工具收进生图域：

- `app/image_migrate.py` → `app/features/image/migrate.py`

## 调用方

`app/main.py`；测试 `test_image_migrate.py`、`test_migrate_api.py`。

## 验证

`compileall`、`test_image_migrate.py` 40/40、`test_migrate_api.py` 20/20。

## 不做

- 不搬 `image_jobs.py`。
- 不改旧目录扫描/复制规则。

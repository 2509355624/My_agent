# 2026-11-17 `comic` feature 第一轮迁移

## 目标

把漫画额度账本收进漫画域：

- `app/comic_quota.py` → `app/features/comic/quota.py`

## 调用方

`app/comic_api.py`、`app/comic_delivery.py`、`app/comic.py`、
`app/features/novel/routes.py`；测试 `test_comic_friend.py`、`test_comic_exclusive.py`。

## 验证

`compileall`、`test_comic_friend.py` 63/63、`test_comic_exclusive.py` 20/20、
`/api/status` 200。

## 不做

- 不搬 `comic.py`、`comic_api.py`、`comic_agent.py`、`moss_*.py`。
- 不改额度默认值、账号归属、作品索引字段。

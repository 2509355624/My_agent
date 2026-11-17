# 2026-11-17 `comic` feature 第二轮迁移

## 目标

把漫画缩略图缓存收进漫画域：

- `app/comic_thumb.py` → `app/features/comic/thumb.py`

## 调用方

`app/comic_api.py` 静态导入；`app/comic_delivery.py`、`app/comic.py`、
`app/features/image/review.py` 函数内导入；测试 `test_comic_friend.py`。

## 验证

`compileall`、`test_comic_friend.py` 63/63、`test_image_review.py` 44/44、
`/api/status` 200。

## 不做

- 不改缩略图尺寸、WEBP 编码参数、缓存路径和 `parse_rel` 白名单。

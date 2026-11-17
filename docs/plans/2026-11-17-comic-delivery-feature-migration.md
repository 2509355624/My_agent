# 2026-11-17 `comic` feature 第四轮迁移

## 目标

把漫画成品交付状态机收进漫画域：

- `app/comic_delivery.py` → `app/features/comic/delivery.py`

## 调用方

`app/comic_api.py`；测试 `test_comic_friend.py`。

## 验证

`compileall`、`test_comic_friend.py` 63/63、`test_comic_exclusive.py` 20/20、
`/api/status` 200。测试里读源码的路径同步改到 feature 目录。

## 不做

- 不改交付状态机、打包规则、指纹、zip 命名和 `RULE` 文案。

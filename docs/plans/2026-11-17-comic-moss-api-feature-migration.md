# 2026-11-17 `comic` feature：Moss 漫画台接口迁移

## 目标

把 Moss 漫画台 Blueprint 收进漫画域：

- `app/moss_api.py` → `app/features/comic/moss_api.py`

## 调用方

`app/main.py`、`app/moss_bot.py`、`app/persona_api.py`；测试 `test_moss_api.py`、
`test_moss_bot.py`、`test_image_channels.py`。

## 验证

`compileall`（app + tests）、`test_moss_api.py` 16/16、`test_moss_bot.py` 67/67、
`/api/status` 200。

## 不做

- 不改 `/moss` 路由、`STATE_ROOT`、`STEPS` / `CFG` / `SKILL` 和任务目录规则。

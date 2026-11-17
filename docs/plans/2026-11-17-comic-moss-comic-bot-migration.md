# 2026-11-17 `comic` feature：MOSS 漫画与机器人迁移

## 目标

- `app/moss_comic.py` → `app/features/comic/moss_comic.py`
- `app/moss_bot.py` → `app/features/comic/moss_bot.py`

MOSS 连载漫画、消息模板、机器人入口都属于漫画域。`moss_comic.py` 的
`from .config` 改为 `from app.config`。

## 调用方

`features/comic/moss_api.py`、`features/comic/soda_letter.py`、
`features/comic/moss_bot.py`；测试 `test_moss_bot.py`、`test_moss_comic.py`、
`test_soda_letter.py`、`test_moss_api.py`、`test_moss_prompt.py`、
`test_moss_series.py`。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_moss_bot.py` 67/67、`test_moss_comic.py` 44/44、`test_soda_letter.py` 10/10、
`test_moss_api.py` 16/16、`test_moss_prompt.py` 10/10、`test_moss_series.py` 41/41。

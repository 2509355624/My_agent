# 2026-11-17 `comic` feature 第八轮迁移

## 目标

把漫画编剧层收进漫画域：

- `app/comic_writer.py` → `app/features/comic/writer.py`
- `app/comic_spec.md` → `app/features/comic/comic_spec.md`（编剧层私有规范，
  路径跟着模块走）

## 调用方

`app/comic_agent.py`、`app/comic.py`；测试 `test_comic.py`、`test_comic_agent.py`。

## 验证

第一次只搬 `.py` 导致 `_SPEC_PATH` 仍指旧目录、2 条报 `FileNotFoundError`；把
`comic_spec.md` 一起 `git mv` 后错误消失。`test_comic.py` 112 条中 3 failures 全为
`skills/qwen_image_v1` 缺失；`test_comic_agent.py` 211 条中 4 failures，同属渠道数据
基线；其余通过。

## 不做

- 不改台词/标签上限、校验回炉次数和 `comic.parse_script_text` 契约。

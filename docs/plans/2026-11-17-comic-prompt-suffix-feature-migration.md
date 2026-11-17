# 2026-11-17 `comic` feature 第三轮迁移

## 目标

把漫画提示词后缀收进漫画域：

- `app/comic_prompt_suffix.py` → `app/features/comic/prompt_suffix.py`

## 调用方

`app/comic_api.py` 静态导入；`app/comic.py` 静态导入；测试
`test_comic_prompt_suffix.py`。

## 验证

`compileall`、`test_comic_prompt_suffix.py` 34/34、`/api/status` 200。

## 不做

- 不改后缀清洗规则、保存文件、`MAX_CHARS` 和接口字段。

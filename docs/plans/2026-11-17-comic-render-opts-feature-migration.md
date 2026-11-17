# 2026-11-17 `comic` feature 第七轮迁移

## 目标

把漫画渲染参数收进漫画域：

- `app/comic_render_opts.py` → `app/features/comic/render_opts.py`

## 调用方

`app/comic.py`、`app/comic_api.py`、`app/features/comic/portrait.py`、
`app/features/card/service.py`；测试 `test_comic_render_opts.py`、
`test_card_maker.py`、`test_lora_slots.py`。

## 验证

`compileall`、`test_comic_render_opts.py` 59 条中 11 failures + 38 errors：全部是
`load_skill` 读不到 `skills/anima-*` 模板导致的 `NoneType` / 默认值缺失，属既有数据
基线，不是迁移改坏。`/api/status` 200。

## 不做

- 不改 `DEFAULT_SKILL`、`RENDER_FILE`、`DEFAULTS_FILE`、参数钳位和 LoRA 链重建规则。

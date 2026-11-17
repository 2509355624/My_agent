# 2026-11-17 `image` feature：LoRA/NAI 辅助模块迁移

## 目标

把生图依赖的整体辅助层收进 `features/image/`：

- `lora_lib.py`、`lora_presets.py`、`lora_slots.py`、`lora_bot.py`
- `char_loras.py`
- `anima_random.py`
- `nai.py`

这些都是生图渠道/LoRA 选择与参数拼装，不是通用工具。

## 调用方

`direct_gen.py`、`image_jobs.py`、`comic.py`、`friend_api.py`、
`features/comic/{moss_api,render_opts}.py`、`features/image/{routes,outfit_api}.py`、
`tools/normal/generate_image.py`、`qq_bot.py`；测试约 12 个文件。

## 路径修正

`test_anima_random.py` 原来按模块 `__file__` 拼 `../skills`，模块进 feature 后失效；
改为从 `tests/` 上溯项目根，直接定位 `skills/_anima_random/workflow.json`。

## 验证

`compileall`（app + tests）。
`test_lora_slots.py` 28 条中 13 failures + 6 errors、`test_lora_presets.py`
47 条中 1 failure、`test_char_loras.py` 3 failures + 2 errors、
`test_char_channel.py` 2 failures、`test_anima_random.py` 3 errors、
`test_nai.py` 2 errors：均为新工作树缺 `skills/`、`NAI_API_KEY` 的既有数据基线。

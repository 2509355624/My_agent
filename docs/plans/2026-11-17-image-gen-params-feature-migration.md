# 2026-11-17 `image` feature 辅助模块迁移

## 目标

把生图参数辅助收进生图域：

- `app/gen_steps.py` → `app/features/image/gen_steps.py`
- `app/default_loras.py` → `app/features/image/default_loras.py`

## 调用方

`app/gen_api.py`、`app/image_jobs.py`；测试 `test_gen_steps.py`。

## 验证

`compileall`（app + tests）、`test_gen_steps.py` 14 条中 1 条既有失败
（`test_reads_and_writes_the_real_moss_template` 缺 `skills/` 真模板），其余 13 条通过；
`image_jobs.py` 保持 CRLF。

## 不做

- 不改步数档位、采样器和默认 LoRA 表。

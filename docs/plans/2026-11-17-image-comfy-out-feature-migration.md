# 2026-11-17 `image` feature：output 分流迁移

## 目标

把 ComfyUI output 按会话/渠道分目录收进生图域：

- `app/comfy_out.py` → `app/features/image/comfy_out.py`

## 调用方

`app/comic.py`、`app/direct_gen.py`、`app/gen_api.py`、`app/image_jobs.py`、
`app/main.py`、`app/outfit_api.py`、`app/showcase_api.py`、`app/features/comic/quota.py`、
`app/features/image/out.py`；测试 `test_comfy_out.py`、`test_gen_landscape.py`、
`test_persona.py`。

## 验证

`compileall`（app + tests）；`test_comfy_out.py` 21 条中 1 条既有失败
（`ManualRunTest` 缺 `skills/` 目录），其余 20 条通过；`image_jobs.py` 保持 CRLF。

## 不做

- 不改 `qq/群_`、`web/`、漫画任务号的目录规则和幂等改写。

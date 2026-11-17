# 2026-11-17 `image` feature：ComfyUI 门禁与源目录迁移

## 目标

- `app/comfy_gate.py` → `app/features/image/comfy_gate.py`
- `app/comfy_src.py` → `app/features/image/comfy_src.py`

两者都只服务 ComfyUI 生图：一个管并发/显存准入，一个定位 ComfyUI 源目录，
不是通用基础设施。

## 调用方

`app/image_jobs.py`（模块级 + 函数内各一处）、`app/comic.py`、
`app/features/image/outfit_api.py`、`app/tools/normal/generate_image.py`；
测试 `test_comfy_gate.py`、`test_comic_exclusive.py`、`test_image_i2i.py`、
`test_image_jobs.py`。

## 验证

`compileall`（app + tests）；`test_comfy_gate.py` 16/16、
`test_comic_exclusive.py` 20/20。

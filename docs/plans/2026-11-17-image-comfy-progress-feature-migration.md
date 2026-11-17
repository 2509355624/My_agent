# 2026-11-17 `image` feature：ComfyUI 进度迁移

## 目标

把 ComfyUI 进度解析收进生图域：

- `app/comfy_progress.py` → `app/features/image/comfy_progress.py`

## 调用方

`app/image_jobs.py`。

## 验证

`compileall`（app + tests）、`test_image_jobs.py` 仍 23 failures + 16 errors（与迁移前
逐条一致）、`/api/status` 200。

## 不做

- 不改进度条字段和 websocket 消息解析。

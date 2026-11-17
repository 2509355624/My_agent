# 2026-11-17 `image` feature：ComfyUI 启动迁移

## 目标

把 ComfyUI 启动命令收进生图域：

- `app/comfy_launch.py` → `app/features/image/comfy_launch.py`

## 调用方

`app/image_jobs.py`、`app/watchdog.py`；测试 `test_comfy_launch.py`。

## 验证

`compileall`（app + tests）、`test_comfy_launch.py` 8/8、`/api/status` 200。

## 不做

- 不改启动参数、端口和环境变量读取。

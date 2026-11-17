# 2026-11-17 `image` feature：ComfyUI 状态迁移

## 目标

把 ComfyUI 在线探测收进生图域：

- `app/comfy_status.py` → `app/features/image/comfy_status.py`

## 调用方

`app/main.py`、`app/agent_prompt.py`；测试 `test_comfy_status.py`、`test_agent_dedup.py`、
`test_agent_loop.py`、`test_cancel.py`、`test_image_claim.py`、`test_qq.py`、
`test_recent.py`、`test_web_api.py`。

## 验证

`compileall`（app + tests）、`test_comfy_status.py` 10/10、`/api/status` 200。

## 不做

- 不改 2 秒后台轮询、20 秒 TTL 和离线保守判定。

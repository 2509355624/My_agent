# 2026-11-17 `image` feature 第七轮迁移

## 目标

从 `image_jobs.py` 抽出发图 caption 与投递，不再让队列文件承担话术：

- `_caption` / `_caption_note_text` / `caption_note_default` / `_send_image`
  → `app/features/image/delivery.py`
- `image_jobs.py` 只保留 `from ... import` 再导出，旧测试仍能 patch
  `image_jobs._send_image`。

## 为什么先抽这一块

测试只 patch `image_jobs._send_image` / `_notice`，不 patch `_caption` 内部实现；
`_notice` 留在根目录，所以根目录的 `_send_text` 补丁仍然有效。队列/ComfyUI 那两块
仍被大量 patch，先不动。

## 验证

`compileall`；`test_image_jobs.py` 仍是 23 failures + 16 errors，失败清单与抽取前
逐条一致（缺 `skills/` 数据、测试假实现不接 `anima_draw`、`COMFY_WAIT_UP` 环境值）；
`/api/status` 200、`/api/characters` 200。

## 不做

- 不搬队列、worker、ComfyUI 提交/重启、恢复逻辑。
- 不改 caption 文案、审核顺序、发送重试语义。

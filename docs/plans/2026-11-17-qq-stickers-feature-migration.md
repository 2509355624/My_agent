# 2026-11-17 `qq` feature：表情包库迁移

## 目标

`app/stickers.py` → `app/features/qq/stickers.py`

表情包收藏依赖 QQ 群图直链与 QQ 识图，属于 QQ 域资产，不是通用图片处理。

## 调用方

`tools/normal/{collect,delete,send}_sticker.py`、`app/qq_bot.py`；
测试 `test_stickers.py`、`test_collect_sticker.py`、`test_image_i2i.py`、
`test_qq_api_robust.py`、`test_vision_prompt.py`。

## 验证

`compileall`（app + tests）；`test_stickers.py` 26/26、
`test_collect_sticker.py` 18/18、`test_qq.py` 199/199。

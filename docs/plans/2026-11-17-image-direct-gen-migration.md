# 2026-11-17 `image` feature：直接生图编排迁移

## 目标

`app/direct_gen.py` → `app/features/image/direct_gen.py`

直接生图的消息编排、渠道判定、提示词模板、回执文案都归 `image`；队列实现仍在
根 `image_jobs.py`，两边保持模块对象不变。

## 调用方

`qq_bot.py`、`features/card/bot.py`、`features/image/routes.py`、
`features/showcase/routes.py`、`features/bench/web.py`；测试 `test_direct_gen.py`、
`test_card_bot.py`、`test_char_channel.py`、`test_image_channels.py`、
`test_image_i2i_gate.py`、`test_outfit_api.py`、`test_showcase.py`。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_card_bot.py` 33/33、`test_showcase.py` 36/36、`test_qq.py` 199/199；
`test_direct_gen.py` 332 条中 83 failures + 28 errors（渠道/技能数据缺失基线）、
`test_char_channel.py` 2 failures、`test_outfit_api.py` 3 errors（缺 outfit 工作流）、
`test_image_i2i_gate.py` 3 failures，均与迁移前一致。

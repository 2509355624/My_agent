# 2026-11-17 `qq` feature：触发词与互动页迁移

## 目标

- `app/trigger.py` → `app/features/qq/trigger.py`
- `app/play_bot.py` → `app/features/qq/play_bot.py`
- `app/solo_bot.py` → `app/features/qq/solo_bot.py`
- `app/friend_site.py` → `app/features/qq/friend_site.py`

四者都只服务 QQ 消息/群友站：触发词解析、`/play` 互动页、单人页卡片、
隧道地址拼接。

## 调用方

`qq_bot.py`、`direct_gen.py`、`moss_bot.py`、`persona.py`、
`features/persona/galgame.py`、`main.py`、`tools/normal/generate_image.py`；
测试 `test_trigger.py`、`test_play_bot.py`、`test_solo_bot.py`、`test_persona.py`、
`test_image_receipt.py`、`test_direct_gen.py`、`test_outfit_api.py`。

## 验证

`compileall`（app + tests）。
`test_trigger.py` 5/5、`test_play_bot.py` 10/10、`test_solo_bot.py` 10/10、
`test_qq.py` 199/199、`test_persona.py` 379/379、`test_outfit_api.py` 50 条中
3 errors（缺 `skills/_qwen_outfit/workflow.json`，基线）。
`test_image_receipt.py` 38 条中 6 failures（回执文案/渠道断言基线，与本次迁移无关）。

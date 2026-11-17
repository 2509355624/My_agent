# 2026-11-17 `gateways`：群友鉴权迁移

## 目标

`app/friend_auth.py` → `app/gateways/friend_auth.py`

5175 群友网关的验证码、会话 cookie、限流锁定只服务网关入口，归 `gateways`。

## 调用方

`friend_app.py`（5175 网关）、`features/admin/routes.py`、`features/bench/web.py`、
`features/gallery/routes.py`、`features/image/outfit_api.py`、
`features/qq/friend_api.py`、`features/showcase/routes.py`、`persona_api.py`、
`direct_gen.py`、`tools/normal/generate_image.py`；测试 `test_friend_proxy.py`、
`test_friend_solo.py`、`test_gallery.py`、`test_outfit_api.py`、
`test_image_receipt.py`、`test_lora_presets.py`、`test_runbench_api.py`、
`test_comic_friend.py`。

## 验证

`compileall`（app + tests + tools + 根入口）。
`test_friend_proxy.py` 45/45（3 skip）、`test_comic_friend.py` 63/63、
`test_runbench_api.py` 22/22、`test_outfit_api.py` 50 条中 3 errors（缺
`skills/_qwen_outfit/workflow.json`，基线）；`test_image_receipt.py` 38 条中
6 failures（回执文案/渠道断言基线）；`test_lora_presets.py` 47 条中 1 failure
（缺 skill 数据，基线）。

# 2026-11-17 `qq` feature：群友生图接口迁移

## 目标

`app/friend_api.py` → `app/features/qq/friend_api.py`

群友生图页的内核 Blueprint（令牌、存/查、切公开、配额回传）只服务 QQ 群友站。

## 调用方

`app/main.py` 注册 `friend_api.bp`（保留模块名）；测试 `test_lora_presets.py`
的 `FriendApiTest` 走接口。

## 验证

`compileall`（app + tests）；`test_lora_presets.py` 47 条中 1 failure，与迁移前一致
（缺 skill 数据）。

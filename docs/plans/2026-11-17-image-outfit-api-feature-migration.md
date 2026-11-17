# 2026-11-17 `image` feature：双图换装接口迁移

## 目标

`app/outfit_api.py` → `app/features/image/outfit_api.py`

换装/换姿势是双图生图 Blueprint，归生图域。

## 路径修正

`_SKILL_DIR` 原按 `app/` 计算 `skills/_qwen_outfit`；进 feature 后多两层目录，
改为四级 `dirname` 回项目根。测试里读 `web/outfit.html` 的路径也改成从
`tests/` 上溯，不再从模块 `__file__` 拼 `../web`。

## 顺带修复

`friend_app.py`、`admin_app.py` 仍在 `from app.logsetup import`，改成
`from app.core.logsetup import`；这是上一提交把 `logsetup` 收进 core 的收尾。

## 验证

`compileall`（app + tests + 两个根入口）；`test_outfit_api.py` 50 条中 3 errors，
仍是缺 `skills/_qwen_outfit/workflow.json` 的既有基线，路径与接线错误已消除。

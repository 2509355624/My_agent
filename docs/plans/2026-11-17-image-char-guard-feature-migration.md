# 2026-11-17 `image` feature：角色内容守卫迁移

## 目标

`app/char_guard.py` → `app/features/image/char_guard.py`

它只服务生图提示词：剥离角色名、拦截未成年人/敏感设定，属于生图生成前置校验，
不是通用安全模块。

## 调用方

`app/tools/normal/generate_image.py`、`tests/test_char_guard.py`。

## 验证

`compileall`（app + tests）；`test_char_guard.py` 18/18。

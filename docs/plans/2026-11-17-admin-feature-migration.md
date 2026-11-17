# 2026-11-17 `admin` feature 第一轮迁移

## 目标

先把管理后台的两个独立入口收进 `app/features/admin/`：

- `persona_admin_api.py` → `routes.py`
- `admin_site.py` → `site.py`

`admin_auth.py` 继续留在 `app/core` 的职责范围，不跟着搬。

## 调用方

`app/main.py`、`app/qq_bot.py`、`tests/test_admin_link.py`、
`tests/test_persona_admin_api.py`、`tests/test_persona_billing.py`、
`tests/test_chat_bg.py`。

## 验证

`compileall`、`test_admin_link.py`、`test_persona_admin_api.py`、
`test_persona_billing.py`、`test_chat_bg.py`、`test_qq.py` 中与管理页相关段，
外加 `/api/status` 与 `/api/characters` 冒烟。

## 不做

- 不搬 `admin_auth.py` / `admin_app.py`（属于 core 安全与 5176 入口）。
- 不改任何 `/api/characters` 字段。

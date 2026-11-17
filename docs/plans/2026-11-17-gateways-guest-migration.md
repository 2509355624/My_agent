# 2026-11-17 `gateways`：访客会话迁移

## 目标

- `app/guest.py` → `app/gateways/guest.py`
- `app/guest_view.py` → `app/gateways/guest_view.py`

访客会话、名额、游客页渲染是网关侧入口逻辑，不写业务规则。

## 相对导入修正

原在 `app/` 根用 `from .config`、`from . import friend_auth`；迁入
`app/gateways/` 后改为 `from app.config`、`from app import friend_auth`。

## 调用方

`features/admin/routes.py`、`persona_api.py`、`main.py`、`direct_gen.py`；
测试 `test_guest.py`、`test_chat_bg.py`、`test_persona_admin_api.py`。

## 验证

`compileall`（app + tests）；`test_guest.py` 21/21、`test_chat_bg.py` 27/27、
`test_persona_admin_api.py` 15/15、`test_persona.py` 379/379。

# 2026-11-17 `core`：管理鉴权迁移

## 目标

`app/admin_auth.py` → `app/core/admin_auth.py`

管理页登录、会话 cookie、改密是跨入口的鉴权基础设施，归 `core`。

## 调用方

根目录 `admin_app.py`（5176 管理网关）、`tools/admin_passwd.py`；测试
`test_admin_app.py`、`test_admin_auth.py`。

## 验证

`compileall`（app + tests + admin_app + tools）；`test_admin_auth.py` 7/7、
`test_admin_app.py` 11/11。

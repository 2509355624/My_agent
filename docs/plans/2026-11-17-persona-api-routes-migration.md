# 2026-11-17 `persona` feature：网页角色扮演接口迁移

## 目标

`app/persona_api.py` → `app/features/persona/routes.py`

网页角色扮演 Blueprint（会话键 `web_<qq>`）只服务角色扮演域，保留 URL 与请求/
响应字段，注册时仍以 `persona_api` 别名引用。

## 调用方

`main.py`（注册 Blueprint）、`features/admin/routes.py`（`GUEST_CARD_ID`）；测试
`test_persona_api.py`、`test_persona.py`、`test_chat_bg.py`、`test_guest.py`、
`test_persona_billing.py`、`test_writing_style.py`。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_persona_api.py` 53/53、`test_persona.py` 379/379、`test_chat_bg.py` 27/27、
`test_guest.py` 21/21；`test_persona_billing.py` 41 条中 1 failure、
`test_writing_style.py` 37 条中 2 failures，均为既有文案/篇幅基线。

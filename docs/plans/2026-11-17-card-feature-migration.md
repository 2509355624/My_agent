# 2026-11-17 `card` feature 迁移

## 目标

把角色卡从 `app/` 根目录搬进 `app/features/card/`，让“角色卡”相关入口
在一个目录内可查：服务、HTTP 路由、QQ 造卡适配器。

不改变接口形状、不改变业务行为、不改变现有页面 URL。

## 映射

| 旧文件 | 新文件 | 职责 |
|---|---|---|
| `app/card_maker.py` | `app/features/card/service.py` | 文案生成、卡库读写、封面渲染、内置卡保护 |
| `app/card_api.py` | `app/features/card/routes.py` | `/card` 页面与 `/api/card/*` Blueprint |
| `app/card_bot.py` | `app/features/card/bot.py` | QQ `造卡` / 引用重画适配器 |

## 依赖方向

`routes.py` / `bot.py` → `service.py` → `app/llm.py`、`app/config.py`、
生图相关根模块。`service.py` 不 import Flask。

## 调用方

- `app/main.py` 注册 Blueprint：改成 `from app.features.card import routes as card_api`。
- `app/qq_bot.py` 调用造卡：把 `card_bot` 导入指向 `features.card.bot`。
- `app/persona_admin_api.py` 复用 `_may_write`：改成 feature 路径。
- `app/lora_bot.py`、`app/solo_bot.py`、`app/play_bot.py` 复用 `_bot_identity`：改成 feature 路径。
- `tests/test_card_bot.py`、`tests/test_card_maker.py` 改成 feature 路径。

## 验证

1. `python -m compileall -q app`
2. `test_card_bot.py`、`test_card_maker.py`、`test_builtin_cards.py`、`test_card_mobile.py`
3. `/card`、`/api/card/list` 冒烟
4. `git diff --check`
5. 提交并推送

## 不做

- 不把 `service.py` 再拆成 repository / prompts；这一轮只搬归属。
- 不改 `/api/card/*` 的请求/响应字段。
- 不改 `web/card_studio.html`。

# card

负责：角色卡生成、封面、五框、卡面导出。

不负责：生图渠道实现、漫画、QQ 消息、管理页 UI。

目录：

- `service.py`：文案生成、卡库读写、封面渲染、内置卡保护。
- `routes.py`：`/card` 页面与 `/api/card/*` Blueprint。
- `bot.py`：QQ `造卡` / 引用重画适配器。

状态：已从 `app/card_maker.py`、`app/card_api.py`、`app/card_bot.py`
迁入并接线；`main.py` 注册 `routes.bp`，`qq_bot.py` 调用 `bot`。
对外仍保持原 URL 和请求/响应字段不变。

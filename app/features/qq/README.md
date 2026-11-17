# qq

负责：QQ 事件、消息发送、账单、群友页内核、访客页内核。

不负责：5174 主站页面路由、5175/5176 网关反代、生图/漫画实现。

迁移起点：`qq_api.py`、`qq_bot.py`、`qq_bill.py`、`qq_names.py`、`qq_status.py`、
`friend_api.py`、`friend_site.py`、`guest.py`、`guest_view.py`。
完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`bill.py`（原 `qq_bill.py`）、`names.py`（原 `qq_names.py`）已迁入并接线；
`qq_api.py`、`qq_bot.py` 等仍在根目录。QQ 机器人不是 Flask 进程，不注册 blueprint；
事件循环放 `service.py`。

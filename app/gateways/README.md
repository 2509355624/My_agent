# app/gateways

负责 5175 群友网关、5176 管理网关：登录、会话、反代、CF 头、限流、最小静态页。

不负责：业务规则、生图/漫画/聊天实现、数据库读写。

约束：

- 网关只做“进来的人是谁、能不能进、转到哪个上游”。
- 新增接口必须登记限流桶；缺条目会让 `rate_ok()` 抛 `KeyError`。
- 不 import feature 内部文件；需要业务结果时走 HTTP 反代或对方 `service.py`。

状态：`guest.py`（原 `app/guest.py`，访客会话/名额）、`guest_view.py`（原
`app/guest_view.py`，游客页渲染）已迁入并接线；`admin_app.py`、`friend_app.py`、
`friend_auth.py`、`admin_auth.py` 仍暂留根目录，后续搬入。

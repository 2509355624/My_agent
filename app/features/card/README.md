# card

负责：角色卡生成、封面、五框、卡面导出。

不负责：生图渠道实现、漫画、QQ 消息、管理页 UI。

迁移起点：`card_api.py`、`card_bot.py`、`card_maker.py`。完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：空包，未接线。卡面生成依赖生图时，只调 `features/image/service.py` 的公开函数，不 import `image` 内部文件。

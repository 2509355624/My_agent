# admin

负责：用量统计、任务账本、provider/模型目录、预设导入导出、Agent 管理后台。

不负责：进程启动、网关反代、生图/漫画/聊天实现。

迁移起点：`admin_site.py`、`persona_admin_api.py`、`usage.py`、`job_ledger.py`、
`provider_store.py`、`model_catalog.py`、`agents.py`、`skills.py`、`recent.py`。
完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`site.py`（公网管理页地址）与 `routes.py`（`/api/characters` 管理接口）已迁入并接线；
`admin_auth.py` 仍留在 `app/core`，不跟 feature 走。管理页只做“看一眼就知道开关在哪、
提示词贴哪里”，不堆说明文字。

# persona

负责：角色扮演、人设、亲密度、计费、角色记忆、查看台、小游戏。

不负责：通用对话循环、生图渠道、QQ 协议、网关。

迁移起点：`persona.py`、`persona_api.py`、`persona_admin_api.py`、`persona_billing.py`、
`persona_rag.py`、`affection.py`、`affection_v2_adult.py`、`galgame.py`、`pregnancy.py`、
`achievements.py`、`items.py`。完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`billing.py`（原 `persona_billing.py`）已迁入并接线；`persona.py` 仍是 441 KB
God 文件，冻结增长，按“人设/记忆/计费/查看台”拆，不整文件搬。

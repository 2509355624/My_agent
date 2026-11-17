# persona

负责：角色扮演、人设、亲密度、计费、角色记忆、查看台、小游戏。

不负责：通用对话循环、生图渠道、QQ 协议、网关。

迁移起点：`persona.py`、`persona_api.py`、`persona_admin_api.py`、`persona_billing.py`、
`persona_rag.py`、`affection.py`、`affection_v2_adult.py`、`galgame.py`、`pregnancy.py`、
`achievements.py`、`items.py`。完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`billing.py`（原 `persona_billing.py`）、`adult.py`（原
`affection_v2_adult.py`）、`rag.py`（原 `persona_rag.py`）、`pregnancy.py`（原
`pregnancy.py`）、`items.py`（原 `items.py`）、`achievements.py`（原
`achievements.py`）、`writing_style.py`（原 `writing_style.py`）、`writing_skills.py`
（原 `app/writing_skills.py`，亲密写法规范切片）、`galgame.py`（原
`galgame.py`）、`routes.py`（原 `app/persona_api.py`，网页角色扮演 Blueprint）、
`service.py`（原 `app/persona.py`，角色池/扮演态/出图编排）、`affection.py`（原
`app/affection.py`，攻略模式好感数值/档位/结算）已迁入并接线；
`service.py` 仍是 441 KB 大文件，冻结增长，后续按“人设/记忆/计费/查看台”拆。

# 2026-11-17 `persona` feature：人设主逻辑迁移

## 目标

`app/persona.py` → `app/features/persona/service.py`

角色池、扮演态、会话状态、出图编排归 `persona` 域；模块对象通过
`from app.features.persona import service as persona` 保持别名不变。

## 调用方

`qq_bot.py`、`main.py`、`features/admin/routes.py`、`features/novel/novel.py`、
`features/persona/{galgame,routes}.py`；测试 20 余个文件。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_persona.py` 379/379、`test_persona_api.py` 53/53、`test_persona_rag.py` 52/52、
`test_affection.py` 176/176、`test_affection_v2_adult.py` 167/167；
`test_affection_v2.py` 63 条中 1 failure（孕期提示词基线）、
`test_pregnancy.py` 33 条中 1 failure（阶段规则基线）。
顺带修正 `test_affection_v2_adult.py` 读取 `persona_api.py` 的旧源码路径。

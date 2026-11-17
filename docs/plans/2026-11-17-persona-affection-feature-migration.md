# 2026-11-17 `persona` feature：好感数值层迁移

## 目标

`app/affection.py` → `app/features/persona/affection.py`

攻略模式的好感数值、档位、结算与提示词块归 `persona` 域；模块对象通过
`from app.features.persona import affection` 保持别名。

## 调用方

`features/persona/service.py`、`features/persona/{achievements,routes}.py`；
测试 `test_affection.py`、`test_affection_v2.py`、`test_affection_v2_adult.py`、
`test_persona*.py`。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_affection.py` 176/176、`test_affection_v2_adult.py` 167/167、
`test_persona.py` 379/379、`test_persona_api.py` 53/53；
`test_affection_v2.py` 63 条中 1 failure（孕期提示词基线）、
`test_forced_mode.py` 20 条中 2 failures（缺 `skills/writing/` 基线）。

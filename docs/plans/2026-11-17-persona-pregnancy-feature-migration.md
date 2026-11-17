# 2026-11-17 `persona` feature 第四轮迁移

## 目标

把怀孕/生产/周期收进角色域：

- `app/pregnancy.py` → `app/features/persona/pregnancy.py`

## 调用方

`app/persona.py`；测试 `test_pregnancy.py`。

## 验证

`compileall`（app + tests）、`test_pregnancy.py` 33 条中 1 条既有断言失败
（`test_set_phase_keeps_day_and_turns` 期望中途改阶段成功，实际被游戏阶段规则挡回），
与怀孕模块导入无关；其余 32 条通过。

## 不做

- 不改周期配置、受孕概率、孕期阶段和提示词块。

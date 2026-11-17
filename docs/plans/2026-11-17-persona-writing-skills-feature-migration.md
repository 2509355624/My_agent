# 2026-11-17 `persona` feature：写法规范切片迁移

## 目标

`app/writing_skills.py` → `app/features/persona/writing_skills.py`

按亲密度轴挑写作规范原文，只服务角色扮演，不参与小说模式。

## 路径修正

原 `_ROOT` 用两级 `dirname` 从 `app/writing_skills.py` 回到项目根；迁入
`app/features/persona/` 后层数不够，改为直接取 `app.config.BASE_DIR`，避免
`app/features/skills/writing` 这种错位。

## 调用方

`app/persona.py`；测试 `test_dev.py`、`test_forced_mode.py`、`test_persona.py`、
`test_writing_skills.py`。

## 验证

`compileall`（app + tests）；`test_persona.py` 379/379。
`test_writing_skills.py` 17 条中 20 failures（子测试）、`test_forced_mode.py` 20 条
中 2 failures：新工作树没有 `skills/writing/` 规范库（该目录被 gitignore），
读取失败走内置兜底，与迁移前同一数据缺口。顺带修复 `test_persona.py` 残留的
`from app import comfy_out`（该模块早已迁到 `features/image`）。

# 2026-11-17 `persona` feature 第七轮迁移

## 目标

把酒馆分层写法收进角色域：

- `app/writing_style.py` → `app/features/persona/writing_style.py`

## 调用方

`app/persona.py`、`app/persona_api.py`、`app/novel.py`；测试 `test_writing_style.py`。

## 验证

`compileall`（app + tests）、`test_persona.py` 379/379；`test_writing_style.py` 37 条中
2 条既有文案断言失败（篇幅锚点仍是旧文案、`tail(plot=400字)` 截到 320 而不是 300），
与模块位置无关。

## 不做

- 不改档位表、篇幅锚点和近因位拼接顺序。

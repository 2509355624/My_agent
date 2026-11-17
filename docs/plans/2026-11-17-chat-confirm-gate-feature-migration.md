# 2026-11-17 `chat` feature：确认门迁移

## 目标

`app/confirm_gate.py` → `app/features/chat/confirm_gate.py`

`confirm_gate` 处理「这轮用户是否在确认上一轮的工具调用」，属于对话回合状态，
不是生图/漫画业务。

## 调用方

`app/direct_gen.py`（`_ATTRIBUTION_RE`）、`app/moss_bot.py`、
`app/tools/normal/generate_image.py`、`app/qq_bot.py`；
测试 `test_confirm_gate.py`、`test_direct_gen.py`。

## 验证

`compileall`（app + tests）；`test_confirm_gate.py` 29/29；
`test_direct_gen.py` 332 条中 83 failures + 28 errors，与迁移前一致（渠道/技能
数据缺失基线）。

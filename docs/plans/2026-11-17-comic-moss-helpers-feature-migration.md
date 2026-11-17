# 2026-11-17 `comic` feature：Moss 辅助模块迁移

## 目标

把 Moss 的消息模板、提示词和连载状态收进漫画域：

- `app/moss_msgs.py` → `app/features/comic/moss_msgs.py`
- `app/moss_prompt.py` → `app/features/comic/moss_prompt.py`
- `app/moss_series.py` → `app/features/comic/moss_series.py`

## 调用方

`app/moss_bot.py`、`app/persona_api.py`、`app/tools/normal/generate_image.py`；测试
`test_moss_bot.py`、`test_moss_prompt.py`、`test_moss_series.py`。

## 验证

`compileall`（app + tests）；`test_moss_prompt.py` 10/10、`test_moss_series.py`
41/41、`test_moss_bot.py` 67/67。移动后修正了两处旧相对导入：
`from . import moss_series`、`from .config import BASE_DIR`。

## 不做

- 不改 Moss 消息文案、提示词和连载编号规则。

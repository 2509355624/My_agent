# 2026-11-17 `novel` feature：写作辅助迁移

## 目标

`app/novel_writing.py` → `app/features/novel/novel_writing.py`

续写/改写辅助只服务小说章节，不参与通用对话。

## 调用方

`app/novel.py`；测试 `test_novel.py` 的 6 处导入。

## 验证

`compileall`（app + tests）；`test_novel.py` 212 条中 6 failures + 7 errors，
与迁移前逐条一致（提示词文案、回滚/UI 接线基线）。

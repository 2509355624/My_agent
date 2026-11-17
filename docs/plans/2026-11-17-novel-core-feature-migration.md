# 2026-11-17 `novel` feature：小说主逻辑迁移

## 目标

`app/novel.py` → `app/features/novel/novel.py`

开书、续写、裁定链、世界书都归小说域；接口仍由 `features/novel/routes.py`
注册，URL 不变。

## 调用方

`features/novel/routes.py`；测试 `test_novel.py`（含 2 处源码路径断言）。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_novel.py` 212 条中 6 failures + 7 errors，与迁移前逐条一致（提示词文案、
页面/接口接线基线）。

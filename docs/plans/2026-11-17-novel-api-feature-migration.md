# 2026-11-17 `novel` feature 第一轮迁移

## 目标

把小说接口收进 `app/features/novel/`：

- `app/novel_api.py` → `app/features/novel/routes.py`

`novel.py` 主逻辑仍留在根目录，等接口稳定后再按“开书/续写/裁定/世界书”拆。

## 调用方

`app/main.py`；测试 `test_novel.py`。

## 验证

`compileall`、`test_novel.py` 212 条中 6 failures + 7 errors：失败点集中在
`NovelWritingSkillTest` 的提示词内容与 `UiSevenTest` 的页面/接口接线，属既有基线；
其余 199 条通过，导入与蓝图注册未受影响。

## 不做

- 不搬 `novel.py`。
- 不改小说 URL、确认口令、请求字段。

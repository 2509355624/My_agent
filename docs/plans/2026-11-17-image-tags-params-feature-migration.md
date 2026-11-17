# 2026-11-17 `image` feature：标签随机与参数规整迁移

## 目标

- `app/random_tags.py` → `app/features/image/random_tags.py`
- `app/gen_params.py` → `app/features/image/gen_params.py`

两者都是生图域纯函数，不依赖 Flask、不持有任务状态。

## 路径修正

`random_tags._ROOT` 原来按 `app/` 计算标签库和 `waifu_pool.json`；进 feature 后
多一层目录，改为四级 `dirname` 回到项目根，否则 `validate()` 会读
`app/features/skills/...`。

## 验证

`compileall`（app + tests）；`test_gen_params.py` 40/40（4 skipped）；
`test_random_tags.py` 10 条中 1 条失败，原因是新工作树缺
`skills/anima-tags/data/tags.tsv`，与迁移无关；`test_gen_api.py` 41 条中
16 failures + 11 errors、`test_direct_gen.py` 332 条中 83 failures + 28 errors，
均为渠道/技能数据缺失基线。

# 2026-11-17 `comic` feature 第五轮迁移

## 目标

把漫画头像生成收进漫画域：

- `app/comic_portrait.py` → `app/features/comic/portrait.py`

## 调用方

`app/comic_api.py`；测试 `test_comic_api.py`。

## 验证

`compileall`、`test_comic_api.py` 98 条中 3 failures + 3 errors，全部集中在
`ComicRenderOptsApiTest`（渲染模板/技能数据缺失导致默认值取不到），属既有基线；
其余 92 条通过，头像接口导入未受影响。

## 不做

- 不改 `MAX_SHOTS`、任务目录、prompt 保存格式和分镜重抽逻辑。

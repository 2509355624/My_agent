# 2026-11-17 `comic` feature 第六轮迁移

## 目标

把连续小漫画的故事生成收进漫画域：

- `app/comic_story.py` → `app/features/comic/story.py`

## 调用方

`app/direct_gen.py`、`app/tools/normal/generate_comic.py`；测试
`test_comic_story.py`。

## 验证

`compileall`、`test_comic_story.py` 33 条中 2 failures + 2 errors，全部是
`AnimaChannelTest` / `ComicChannelTest` 找不到 `skills/anima-*` 工作流导致的
`NoneType`，属既有数据基线；其余 29 条通过。

## 不做

- 不改 `MAX_PANELS`、渠道默认、`clamp_panels` 和渲染入口。

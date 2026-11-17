# 2026-11-17 `comic` feature：工作台接口迁移

## 目标

把漫画工作台 Blueprint 收进漫画域：

- `app/comic_api.py` → `app/features/comic/routes.py`

## 调用方

`app/main.py`；测试 `test_comic_api.py`、`test_comic_exclusive.py`、
`test_comic_friend.py`、`test_comic_prompt_suffix.py`、`test_comic_reseed.py`、
`test_comic_timing.py`。测试里读源码的两处路径同步改到 feature 目录。

## 验证

`compileall`（app + tests）；`test_comic_friend.py` 63/63、
`test_comic_exclusive.py` 20/20、`test_comic_prompt_suffix.py` 34/34、
`test_comic_reseed.py` 11/11、`test_comic_timing.py` 18/18；
`test_comic_api.py` 98 条中 3 failures + 3 errors，全部在 `ComicRenderOptsApiTest`，
与迁移前逐条一致；`test_comic_portrait.py` 15 条中 4 failures + 2 errors，均为缺
`skills/anima-line` 工作流。

顺带修正了两个此前残留的测试导入：`comic_render_opts`、`comic_portrait`
仍从 `app` 根导入，导致模块整体 ImportError。

## 不做

- 不改任何 `/api/comic/*` URL、请求/响应字段和任务目录规则。

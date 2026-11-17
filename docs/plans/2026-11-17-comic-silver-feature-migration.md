# 2026-11-17 `comic` feature：Silver 编剧迁移

## 目标

把 Silver 漫画的编剧/解析收进漫画域：

- `app/silver_comic.py` → `app/features/comic/silver_comic.py`

## 调用方

`app/comic_api.py`；测试 `test_silver_comic.py`、`test_comic_api.py`。
`test_comic_api.py` 里的 4 处 `mock.patch("app.silver_comic.write_script_detail")`
改为新路径。

## 验证

`compileall`（app + tests）、`test_silver_comic.py` 18/18；`test_comic_api.py` 98 条中
3 failures + 3 errors，全部集中在 `ComicRenderOptsApiTest`，与迁移前逐条一致。

## 不做

- 不改 Silver 分格上限、气泡/无气泡解析和 120 秒超时。

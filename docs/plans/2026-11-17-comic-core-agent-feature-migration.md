# 2026-11-17 `comic` feature：漫画核心与 Agent 迁移

## 目标

- `app/comic.py` → `app/features/comic/comic.py`
- `app/comic_agent.py` → `app/features/comic/agent.py`

分镜调度、阶段渲染、CLI 与漫画 Agent 循环都归漫画域；`app.comic_agent`
导入改用 `agent` 别名，调用方无需改函数名。

## 调用方

`features/card/service.py`、`features/comic/{delivery,portrait,routes,silver_comic,writer}.py`、
`features/image/routes.py`；测试 `test_comic*.py`、`test_gen_api.py`、
`test_gen_landscape.py`、`test_affection_v2_adult.py`。

## 验证

`compileall`（app + tests）；`import app.main` 通过。
`test_comic_friend.py` 63/63、`test_comic_exclusive.py` 20/20、
`test_comic_prompt_suffix.py` 34/34、`test_comic_reseed.py` 11/11、
`test_comic_timing.py` 18/18；`test_comic.py` 112 条中 3 failures、
`test_comic_agent.py` 211 条中 4 failures、`test_comic_api.py` 98 条中 3
failures + 3 errors、`test_comic_render_opts.py` 59 条中 11 failures + 38
errors、`test_gen_api.py` 41 条中 16 failures + 11 errors，均为缺 `skills/`
渠道数据的既有基线。

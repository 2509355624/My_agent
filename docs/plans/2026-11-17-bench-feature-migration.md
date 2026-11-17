# 2026-11-17 `bench` feature：跑分台迁移

## 目标

- `app/runbench_api.py` → `app/features/bench/api.py`
- `app/runbench_web.py` → `app/features/bench/web.py`

## 调用方

`app/main.py` 注册 `runbench_web`（保留别名）；`direct_gen.py`、`features/image/output_api.py`、
`tools/normal/generate_image.py` 引用 `runbench_api`（保留别名）；测试 `test_runbench_api.py`。

## 验证

`compileall`（app + tests）；`test_runbench_api.py` 22/22。

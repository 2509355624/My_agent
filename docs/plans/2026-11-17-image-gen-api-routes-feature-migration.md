# 2026-11-17 `image` feature：手动跑图接口迁移

## 目标

`app/gen_api.py` → `app/features/image/routes.py`

生图页/跑图台的 Blueprint 属于生图域，不应留在根目录。

## 调用方

`app/main.py`（注册 Blueprint）、`app/runbench_web.py`；
测试 `test_gen_api.py`、`test_gen_landscape.py`、`test_image_channels.py`。

导入统一保留别名：`from app.features.image import routes as gen_api`，这样
测试里 `gen_api.xxx` 和既有 patch 目标不用改语义。

## 验证

`compileall`（app + tests）；`test_gen_api.py` 41 条中 16 failures + 11 errors、
`test_gen_landscape.py` 18 条中 6 failures，均与迁移前一致，原因是新工作树缺
`skills/` 渠道数据，不是接线错误。

# 2026-11-17 `workers`：看门狗迁移

## 目标

`app/watchdog.py` → `app/workers/watchdog.py`

看门狗是独立常驻进程，不处理 HTTP，归 `workers`。

## 启动方式变更

`python -m app.watchdog` → `python -m app.workers.watchdog`。

同步更新 `启动看门狗.bat`、`clean_zombies.py` 的进程识别、`README.md` 启动说明；
`docs/handoff_20261108.md` 是历史交接文档，保留原命令不改。

## 调用方

独立进程；测试 `test_watchdog.py`（导入与 `mock.patch` 路径同步改为
`app.workers.watchdog`）。

## 验证

`compileall`（app + tests）；`test_watchdog.py` 62 条中 1 failure，与迁移前一致
（启动命令常量断言）。

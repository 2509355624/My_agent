# 2026-11-17 `image` feature：输出文件接口迁移

## 目标

`app/output_api.py` → `app/features/image/output_api.py`

输出文件浏览/缩略图接口只服务生图产物。

## 调用方

`app/main.py` 注册（保留模块名）；测试 `test_output_api.py`。

## 验证

`compileall`（app + tests）；`test_output_api.py` 14/14。

# 2026-11-17 `gallery` feature：画廊迁移

## 目标

- `app/gallery.py` → `app/features/gallery/gallery.py`
- `app/gallery_api.py` → `app/features/gallery/routes.py`

画廊扫描和 HTTP 接口同属一个域，URL 与响应字段不变。

## 调用方

`app/main.py` 注册 `gallery_api`（保留别名）；`features/gallery/routes.py` 内部
引用 `gallery`；测试 `test_gallery.py`。

## 验证

`compileall`（app + tests）；`test_gallery.py` 118 条中 1 failure
（`test_only_owner_can_scan`，与迁移前一致：`GALLERY_ROOTS=()` 时接口按设计 400）。

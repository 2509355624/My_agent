# 2026-11-17 `comic` feature：Soda 排字迁移

## 目标

把 Soda 程序排字收进漫画域：

- `app/soda_letter.py` → `app/features/comic/soda_letter.py`

## 调用方

`app/features/comic/moss_api.py`；测试 `test_soda_letter.py`。

## 验证

`compileall`（app + tests）、`test_soda_letter.py` 10/10、`/api/status` 200。

## 不做

- 不改 `MOSS_SODA_LETTER` 开关、气泡坐标和工作流节点改写。

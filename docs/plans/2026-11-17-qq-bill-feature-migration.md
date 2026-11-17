# 2026-11-17 `qq` feature 第一轮迁移

## 目标

把 QQ 账单收进 QQ 域：

- `app/qq_bill.py` → `app/features/qq/bill.py`

## 调用方

`app/main.py` 的两个账单接口；测试 `test_qq_bill.py`。

## 验证

`compileall`、`test_qq_bill.py` 8/8、`/api/status` 200。

## 不做

- 不搬 `qq_api.py`、`qq_bot.py`。这两块被大量测试 patch 内部函数，先等账单这类
  只读模块迁完，再单独定 patch 兼容策略。
- 不改账期、单价、高峰判定和 CSV 列顺序。

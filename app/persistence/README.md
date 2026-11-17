# app/persistence

负责 JSON/SQL 存取与任务账本，不判断业务。

```text
json_store.py   包住现有 state/*.json 的读写
job_ledger.py   任务账本
db.py           以后 SQLAlchemy 2.0
migrations/     以后 Alembic
```

约束：

- 不 import Flask，不读 `request`。
- 不写业务分支；只做 load/save/query。
- 路径统一走 `core/paths.py`，不硬编码机器路径。
- 迁移期先双读写对账，再切读；不一次删旧文件。

状态：空包，未接线。

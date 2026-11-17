# bench

负责：跑分/评测台的接口与页面（`api.py` 原 `runbench_api.py`，`web.py` 原
`runbench_web.py`）。

不负责：生图主队列、QQ 协议、通用管理页。

状态：已迁入并接线，URL 与响应字段不变。页面仍复用根目录 `friend_auth`、`qq_api`。

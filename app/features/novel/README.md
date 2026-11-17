# novel

负责：小说模式、世界书、章节写作、裁定链。

不负责：通用对话循环、生图、QQ 协议、管理页 UI。

迁移起点：`novel.py`、`novel_api.py`、`novel_writing.py`。完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`routes.py`（原 `novel_api.py` 的小说接口）已迁入并接线；`novel.py` 主逻辑仍在
根目录，等接口稳定后再按“开书/续写/裁定/世界书”拆 `service.py` 与
`repository.py`。

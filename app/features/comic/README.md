# comic

负责：漫画编剧、分镜、渲染、缩略图、交付、额度、作品管理。

不负责：单张生图渠道、LoRA 管理、QQ 消息、管理页 UI。

迁移起点：`comic.py`、`comic_agent.py`、`comic_api.py`、`comic_delivery.py`、
`comic_portrait.py`、`comic_prompt_suffix.py`、`comic_quota.py`、`comic_render_opts.py`、
`comic_story.py`、`comic_thumb.py`、`comic_writer.py`、`moss_api.py`、`moss_bot.py`、
`moss_comic.py`、`moss_msgs.py`、`moss_prompt.py`、`moss_series.py`、`silver_comic.py`。
完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：`quota.py`（原 `comic_quota.py`）、`thumb.py`（原 `comic_thumb.py`）、
`prompt_suffix.py`（原 `comic_prompt_suffix.py`）、`delivery.py`（原 `comic_delivery.py`）、
`portrait.py`（原 `comic_portrait.py`）、`story.py`（原 `comic_story.py`）、
`render_opts.py`（原 `comic_render_opts.py`）、`writer.py`（原 `comic_writer.py`）已迁入并
接线；`comic_spec.md` 随编剧模块一起放。漫画主流程仍在根目录，按“编剧/分镜/渲染/交付”
继续拆，不和生图互相 import 内部文件。

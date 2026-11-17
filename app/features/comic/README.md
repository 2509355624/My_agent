# comic

负责：漫画编剧、分镜、渲染、缩略图、交付、额度、作品管理。

不负责：单张生图渠道、LoRA 管理、QQ 消息、管理页 UI。

迁移起点：`comic.py`、`comic_agent.py`、`comic_api.py`、`comic_delivery.py`、
`comic_portrait.py`、`comic_prompt_suffix.py`、`comic_quota.py`、`comic_render_opts.py`、
`comic_story.py`、`comic_thumb.py`、`comic_writer.py`、`moss_api.py`、`moss_bot.py`、
`moss_comic.py`、`moss_msgs.py`、`moss_prompt.py`、`moss_series.py`、`silver_comic.py`。
完整映射见 `docs/project-skeleton.md` 第 4 节。

状态：空包，未接线。漫画和生图共用 ComfyUI，但业务边界分开，不互相 import 内部文件。

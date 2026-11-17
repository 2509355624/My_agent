# tools/generators

会生成正式资产或 ComfyUI 工作流的脚本。和生产代码的区别：它们不是服务运行时入口，只在需要重生成资产时手动运行。

当前包含：

- `_make_hd_channels.py`、`_make_i2i_workflows.py`：生成高清/图生图工作流。
- `_sync_comfy_workflow.py`：从 ComfyUI 导出工作流。
- `_build_qwen_charsheet_wf.py`、`_rebuild_charsheet_*.py`、`_adapt_character_sheet.py`：角色设定图相关工作流。
- `_add_4x_upscale.py`：给工作流补 4x 放大节点。

运行前先读脚本头部：输入、输出和覆盖行为都写在注释里。生成到 `app/tools/.../workflow*.json` 时，先确认 diff 再提交。

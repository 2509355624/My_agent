# tools 目录

这里放“不是运行时入口”的脚本。根目录只保留 9 个真正的入口与运维脚本。

## 子目录

- `legacy/`：一次性探针、验证、临时对比脚本。默认不参与运行，不要从业务代码 import。
- `generators/`：仍可能用来重生成资产/ComfyUI 工作流的脚本。运行前先看脚本头部说明，确认输出路径。

## 根目录入口（不要移动）

| 文件 | 用途 |
|---|---|
| `agent.py` | 5174 主站入口 |
| `admin_app.py` | 管理站入口 |
| `friend_app.py` | 5175 公网网关入口 |
| `friend_tunnel.py` | 隧道地址维护 |
| `clean_zombies.py` | 运维：清理残留进程 |
| `export_presets.py` | 运维：导出预设 |
| `import_presets.py` | 运维：导入预设 |
| `napcat_qr.py` | 运维：QQ 协议端登录 |
| `restart_servers.py` | 运维：手动重启 |

## 约定

1. 新增一次性脚本放 `tools/legacy/`，文件名仍可用 `_` 前缀。
2. 新增会生成正式资产的脚本放 `tools/generators/`，并在文件头写清输入/输出。
3. 业务代码、测试、web 页面都不应 import `tools/legacy`。
4. 不要在根目录新增 `_probe_*`、`_verify_*`、`_tmp_*`。

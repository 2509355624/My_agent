"""Web 配置域：主站端口、管理接口访问范围、画廊根目录、提示词工坊入口。

页面本身在 web/，路由在 app/main.py；这里只放启动和页面开关需要的默认值。
"""

import os

AGENT_PORT = int(os.getenv("AGENT_PORT", "5174"))

# 后台管理接口是否允许非本机访问。三档（大小写不敏感）：
#   "" / "0" / "false"  → 只允许回环地址（默认）
#   "lan"               → 回环 + 内网地址（手机/平板从同一个路由器进来）
#   "1" / "true"        → 任何来源
# 服务监听 0.0.0.0 且**没有任何鉴权**，一个能改 agent 配置的口子不该顺带
# 暴露到整个网络，所以默认收得最紧。「lan」是给「手机要能改配置」这种常见
# 诉求留的中间档：比 true 少暴露一层，端口转发/公网直连进来的请求会被挡掉
# （判定在 app/main.py 的 `_is_lan_addr`）。
_ADMIN_REMOTE_RAW = os.getenv("ADMIN_ALLOW_REMOTE", "false").strip().lower()
ADMIN_ALLOW_REMOTE = _ADMIN_REMOTE_RAW not in ("0", "false", "no", "")
ADMIN_ALLOW_REMOTE_LAN = _ADMIN_REMOTE_RAW == "lan"

# ─── 画廊 ───────────────────────────────────────────
#
# 站主磁盘上的图片根目录。**只登记路径，不复制文件**：那 60G 本来就在盘上，
# 画廊是「把磁盘挂出来看」，不是「再存一份」。
#
# 格式：多个用分号隔开，每项 `显示名|绝对路径`（显示名可省，则取目录名）。
# 不用冒号分隔，因为 Windows 盘符本身就带冒号。
def _parse_gallery_roots(raw):
    out = []
    for part in str(raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, path = part.partition("|")
        path = (path or name).strip()
        if not path:
            continue
        name = (name if _ else "").strip() or os.path.basename(path.rstrip("\\/")) or path
        out.append((name, path))
    return tuple(out)


GALLERY_ROOTS = _parse_gallery_roots(os.getenv("GALLERY_ROOTS", ""))

# 站主的 QQ：只有它登录能看到「扫描文件夹」「全部作品」这些管理动作。
# 留空 = 管理功能整体关闭（宁可关掉也不猜，猜错了等于把删除权限给了别人）。
GALLERY_OWNER_QQ = os.getenv("GALLERY_OWNER_QQ", "").strip()

# ─── 提示词工坊 ─────────────────────────────────────────
#
# 一个完全自包含的单文件 HTML（别人的工具，原样发出去，一个字都不改）。
# **不复制进 `web/`**：4.6MB 进 git 没意义，而且它按版本号换目录
# （1.3.5 → 1.3.6），配置一行比复制一份好维护。
#
# 留空 = 这个入口整个不存在（返回 404），不会在页面上留一个点不开的链接。
WORKSHOP_HTML = os.getenv("WORKSHOP_HTML", "").strip()

__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")]

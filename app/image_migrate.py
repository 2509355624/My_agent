# -*- coding: utf-8 -*-
r"""图片迁移：手工翻看一批图，挑中的直接**复制**一份到指定文件夹。

用户 2026-10-11 的需求：输入端填一个文件夹（比如 ComfyUI 的 output），
输出端填自己的精选文件夹；一批 3 张横向看过去，觉得好的点一下就复制过去。

设计取舍（都写在这儿，改之前先读）：
- **只复制、不移动**（用户原话「手法是直接复制一份过去」）⇒ 源文件夹永远不动。
- **同名就跳过**（用户拍板）：目标里已有同名文件时**一个字节都不写**，
  界面显示「已迁移」。⇒「迁没迁过」是**从目标目录推导出来的**，
  不落任何状态文件 —— 刷新 / 换浏览器 / 重启机器都对得上。
- **只扫顶层**，不递归子目录；只认图片扩展名（`IMG_EXTS`）。
- **出图只认「目录下的顶层文件名」**（`safe_name` 挡掉任何路径分隔符），
  再由 Flask 的 `send_from_directory` 兜底 —— 见 `app/main.py` 的路由。
- 路径一律 `abspath` 规范化，避免 `D:\a\..\b` 这种写法在界面上显示得前后不一致。
"""

import io
import json
import os
import shutil

from app.config import state_path

try:                       # Pillow 是 image_out 的硬依赖，这里只是别在缺了的时候炸
    from PIL import Image
except ImportError:        # pragma: no cover
    Image = None

# 只认这些扩展名（大小写不敏感）。故意**不含** mp4 / txt —— 这个页面是给图用的。
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}

CONFIG_FILE = "image_migrate.json"

DEFAULT_BATCH = 3
MIN_BATCH = 1
MAX_BATCH = 60          # 一批 60 张已经够翻的了；再大浏览器要同时拉 60 张原图

# 第一次打开时的输入目录猜测。猜错了无所谓 —— 用户改一次就存进配置了。
_GUESS_SRC = r"D:\AI\ComfyUI_v037\output"


def normalize_dir(p):
    """把界面上填的路径规范化成绝对路径；空串返回 None。

    顺手剥掉首尾空格和**成对的引号** —— 用户从资源管理器「复制文件地址」
    粘过来时带的是 `"D:\\foo bar"` 这种带引号的形式。
    """
    if not isinstance(p, str):
        return None
    s = p.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    if not s:
        return None
    return os.path.abspath(os.path.expanduser(s))


def safe_name(name):
    """只接受**纯文件名**。带任何路径成分（`/` `\\`、`.`、`..`）一律拒。

    这是出图接口的闸门：名字来自 URL 查询串，不挡住就能读到任意文件。
    """
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n or n in (".", "..") or "\x00" in n:
        return None
    if "/" in n or "\\" in n or ":" in n:
        return None
    if os.path.basename(n) != n:
        return None
    return n


def list_images(d):
    """目录**顶层**的图片文件名，按名字（忽略大小写）升序。

    目录读不了就返回空列表 —— 调用方靠 `os.path.isdir` 分辨「空目录」和
    「路径不存在」，这里不抛。
    """
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        if os.path.splitext(n)[1].lower() not in IMG_EXTS:
            continue
        try:
            if os.path.isfile(os.path.join(d, n)):
                out.append(n)
        except OSError:
            continue
    out.sort(key=lambda s: (s.lower(), s))
    return out


def clamp_batch(v):
    """批次大小：非法值退回默认，合法值夹在 [MIN_BATCH, MAX_BATCH]。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_BATCH
    return max(MIN_BATCH, min(n, MAX_BATCH))


def scan(src, dst="", offset=0, limit=DEFAULT_BATCH):
    """列一批。返回给界面用的完整状态（含 total / offset，界面不用自己算）。"""
    out = {
        "src": src or "", "dst": dst or "",
        "src_ok": False, "dst_ok": False,
        "total": 0, "offset": 0, "limit": clamp_batch(limit),
        "items": [],
    }

    s = normalize_dir(src)
    if not s or not os.path.isdir(s):
        out["error"] = "输入文件夹不存在：%s" % (src or "（还没填）")
        return out
    out["src"] = s
    out["src_ok"] = True

    d = normalize_dir(dst)
    out["dst_ok"] = bool(d and os.path.isdir(d))
    if d:
        out["dst"] = d

    names = list_images(s)
    out["total"] = len(names)

    # offset 夹到合法范围：夹完之后**仍指向有内容的那一批**
    # （末页被删空时不要跳回第 0 批）
    if out["total"]:
        offset = max(0, min(int(offset or 0), out["total"] - 1))
    else:
        offset = 0
    out["offset"] = offset

    for n in names[offset:offset + out["limit"]]:
        p = os.path.join(s, n)
        try:
            st = os.stat(p)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        out["items"].append({
            "name": n,
            "size": size,
            "mtime": mtime,
            # 「迁没迁过」= 目标里有没有同名文件。不落状态，天然一致。
            "migrated": bool(d and os.path.exists(os.path.join(d, n))),
        })
    return out


def migrate(src, name, dst):
    """把 src/name 复制到 dst/name。同名**跳过**（不覆盖）。

    返回 `{"ok": bool, ...}`；`ok=True` 且 `skipped=True` 表示「早就迁过了」。
    """
    s = normalize_dir(src)
    d = normalize_dir(dst)
    n = safe_name(name)

    if not s or not n:
        return {"ok": False, "error": "参数不合法"}
    if not d:
        return {"ok": False, "error": "请先填输出文件夹"}
    sp = os.path.join(s, n)
    if not os.path.isfile(sp):
        return {"ok": False, "error": "源文件不在了：%s" % n}
    if not os.path.isdir(d):
        return {"ok": False, "error": "输出文件夹不存在：%s" % d}

    dp = os.path.join(d, n)
    if os.path.exists(dp):
        return {"ok": True, "skipped": True, "name": n, "dst": dp,
                "message": "目标里已有同名文件，跳过（没动它）"}
    try:
        shutil.copy2(sp, dp)
    except OSError as e:
        return {"ok": False, "error": "复制失败：%s" % e}
    return {"ok": True, "skipped": False, "name": n, "dst": dp}


def thumbnail_bytes(path, max_side=900, quality=82):
    """把图缩成「长边 max_side」的 JPEG 字节，给界面当缩略图。

    为什么要缩：源目录里的图常常是 3072×4608 的 PNG（单张十几 MB），
    一批三张原图就是几十 MB，翻页会卡。缩略图只用于**快速扫一眼**；
    点开大图走的是不带 `w` 参数的原图。

    失败一律返回 None（图坏了 / 格式认不出 / Pillow 不在），调用方退回发原图 ——
    **缩略图是优化，不是前置条件**。
    """
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            im.load()                       # 必须在 with 里读完，否则是懒加载
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
    except Exception:
        return None
    return buf.getvalue()


# ─── 配置（输入/输出目录 + 批次大小）──────────────────────────
# 存 state/ 下，用 `state_path()` —— 测试进程会自动被导向临时目录，
# 跑测试不会覆盖真配置。

def default_config():
    return {
        "src": _GUESS_SRC if os.path.isdir(_GUESS_SRC) else "",
        "dst": "",
        "batch": DEFAULT_BATCH,
    }


def load_config():
    cfg = default_config()
    try:
        with open(state_path(CONFIG_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for k in ("src", "dst"):
            if isinstance(data.get(k), str):
                cfg[k] = data[k]
        if "batch" in data:
            cfg["batch"] = clamp_batch(data["batch"])
    return cfg


def save_config(data):
    """部分更新：只认 src / dst / batch 三个键，其余原样保留。"""
    cfg = load_config()
    if isinstance(data, dict):
        for k in ("src", "dst"):
            if isinstance(data.get(k), str):
                cfg[k] = data[k].strip()
        if "batch" in data:
            cfg["batch"] = clamp_batch(data["batch"])
    p = state_path(CONFIG_FILE)
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass
    return cfg

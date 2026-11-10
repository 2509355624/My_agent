"""群友漫画台：网页预览用的缩略图（服务端按需生成、落盘缓存）。

## 为什么必须有这一层

漫画格子原图单张 9MB 级别（2x 放大后），10 格全刷一遍 ≈ 140MB。群友页面走
5175 的 Cloudflare 隧道，把原图拉过去等于「国内 → 海外 → 国内」绕一圈，
而高清成品本来就已经通过 QQ 本机带宽直发了。所以：

- 网页：只看 960px WEBP 缩略图，单张几十 KB，整本 1~2MB，隧道秒开。
- QQ：`raw/` 底图包 + `final/` 成品包两个 zip，装的是高清原图，走本机带宽。

## 为什么放服务端磁盘而不是浏览器 Local Storage

Local Storage 有配额（通常 5~10MB），塞不下十几张缩略图；换设备、清缓存、
换浏览器就全没了，刷新也回不来。群友要「回网页点刷新看之前的作品」，
只有服务端落盘才成立。浏览器那份只存当前任务号（原页面既有行为），
一张图片字节都不进浏览器。

## 生成时机

`/api/comic/img` 收到 thumb 请求时 `ensure()` 懒生成：源文件比缩略图新就重画，
否则直接读缓存。换种子重跑 / 从底图重画后源文件 mtime 变了，下一帧自动刷新，
不需要在 comic.py 的多个落盘点各插一刀。整本跑完 QQ 发送的后台线程里再
`ensure_all()` 预热一次，群友第一次打开槽位墙不用等缩略图逐张生成。

参数沿用 `app/chat_bg.py` 那套已验证的 PIL 写法（LANCZOS + WEBP），
Pillow 本来就在 requirements.txt 里，零新依赖。
"""

import os

from PIL import Image, ImageOps

# 960 是气泡字还能看清的下限：480 时对话框里那几个字糊成一团。
# 960px WEBP q78 实测单张几十 KB，10 格也就 1~2MB。
THUMB_EDGE = 960
THUMB_QUALITY = 78
_KINDS = ("raw", "final", "portrait")


def rel(no, kind):
    """缩略图相对任务目录的路径。格式固定，`/api/comic/img` 的正则照它写。"""
    return "thumb/%s_%s_thumb.webp" % (str(no).zfill(2), kind)


def src_rel(no, kind):
    if kind == "raw":
        return "raw/%s_raw.png" % str(no).zfill(2)
    if kind == "final":
        return "final/%s_final.png" % str(no).zfill(2)
    return "portraits/%s.png" % str(no).zfill(2)


def parse_rel(f):
    """`"thumb/03_final_thumb.webp"` → `("03", "final")`；不认识返回 None。"""
    s = str(f or "").replace("\\", "/")
    if not s.startswith("thumb/"):
        return None
    name = s[6:]
    if not name.endswith("_thumb.webp"):
        return None
    body = name[:-len("_thumb.webp")]
    parts = body.rsplit("_", 1)
    if len(parts) != 2 or parts[1] not in _KINDS or not parts[0].isdigit():
        return None
    return parts[0].zfill(2), parts[1]


def thumb_path(task_dir, no, kind):
    return os.path.join(str(task_dir), "thumb",
                        "%s_%s_thumb.webp" % (str(no).zfill(2), kind))


def _fresh(src, dst):
    """缓存是否仍然有效：缩略图存在且写得不比原图早。"""
    try:
        a, b = os.stat(src), os.stat(dst)
        return b.st_size > 0 and b.st_mtime_ns >= a.st_mtime_ns
    except OSError:
        return False


def ensure(task_dir, no, kind):
    """保证这张缩略图是最新的 → 绝对路径；原图不存在/生成失败返回 None。

    原子写：先写 .tmp 再 replace，避免群友正好在重跑时读到半张 WEBP。
    """
    if kind not in _KINDS:
        return None
    task_dir = str(task_dir)
    src = os.path.join(task_dir, *src_rel(no, kind).split("/"))
    dst = thumb_path(task_dir, no, kind)
    if not os.path.isfile(src):
        return None
    if _fresh(src, dst):
        return dst
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with Image.open(src) as im:
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
            tmp = dst + ".tmp"
            im.save(tmp, "WEBP", quality=THUMB_QUALITY, method=4)
        os.replace(tmp, dst)
        return dst
    except Exception as exc:            # PIL 打不开 / 磁盘满都不该让整页 500
        import logging
        logging.getLogger("comic_thumb").warning(
            "缩略图生成失败 %s：%s", src, exc)
        return dst if os.path.isfile(dst) else None


def ensure_all(task_dir, panels=None, portraits=None):
    """整本跑完预热一遍。返回生成了几张。失败只跳过，不打断 QQ 发送。

    直接扫 `raw/ final/ portraits/` 三个目录，不依赖调用方把 `Task.panels`
    传下来——`comic_delivery.spawn_auto` 跑在渲染后台线程里，手上只有任务
    目录路径，没有 Task 对象；按目录扫也比猜格数可靠（换种子重跑、写真
    单张重跑都会落到同样的文件名）。
    """
    task_dir = str(task_dir)
    n = 0
    for kind, sub, suffix in (("raw", "raw", "_raw.png"),
                              ("final", "final", "_final.png"),
                              ("portrait", "portraits", ".png")):
        d = os.path.join(task_dir, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith(suffix):
                continue
            no = name[:-len(suffix)]
            if not no.isdigit():
                continue
            if ensure(task_dir, no.zfill(2), kind):
                n += 1
    return n

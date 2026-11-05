"""聊天背景的存储与图片处理（2026-11-05）。

三种背景，按优先级排：

    user_<hash>.webp    账号自己换的（游客没有）
    card_<hash>.webp    这张角色卡给所有人用的默认背景
    default.webp        全局默认（站主在 5174 管理页上传）
    web/assets/chat_bg_default.*   内置兜底图（站主自己放）

**只存文件，不存映射表**：有没有设置看文件在不在，换图直接覆盖，恢复默认
就删文件。账号/卡号进文件名前先哈希——不把 QQ 号、邮箱写进路径。

图片一律重编码成 WebP：长边缩到 1920，另存一张 480 的缩略图给右栏和设置页
用。这样原图 20MB、8000px 也不会拖慢聊天页。
"""
import hashlib
import io
import os
import urllib.parse

from PIL import Image, ImageOps

from app.config import BASE_DIR, WEB_DIR

ROOT = os.path.join(BASE_DIR, "state", "backgrounds")
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_EDGE = 1920
THUMB_EDGE = 480
QUALITY = 82
THUMB_QUALITY = 75

_KINDS = ("default", "card", "user", "builtin")
_BUILTIN_NAMES = ("chat_bg_default.webp", "chat_bg_default.jpg",
                  "chat_bg_default.png", "chat_bg_default.jpeg")


def _safe_id(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _builtin_path():
    for name in _BUILTIN_NAMES:
        p = os.path.join(WEB_DIR, "assets", name)
        if os.path.isfile(p):
            return p
    return None


def path_for(kind, ident="", thumb=False):
    """返回某类背景的落盘路径；`builtin` 返回内置图路径（可能 None）。"""
    kind = str(kind or "").strip().lower()
    if kind not in _KINDS:
        return None
    if kind == "builtin":
        return _builtin_path()
    if kind == "default":
        name = "default.webp"
    elif kind == "card":
        name = "card_%s.webp" % _safe_id(ident)
    else:
        name = "user_%s.webp" % _safe_id(ident)
    p = os.path.join(ROOT, name)
    if thumb:
        p = p[:-len(".webp")] + "_thumb.webp"
    return p


def _thumb_path(full):
    if not full:
        return None
    return full[:-len(".webp")] + "_thumb.webp" if full.endswith(".webp") else full


def _url(kind, ident, version, thumb=False):
    q = {"kind": kind, "v": str(version)}
    # 账号层的身份由服务端 session 认（`persona_api.background_file` 里强制覆盖
    # ident），**不要把 QQ 写进 URL**——会被浏览器历史和日志记下来。
    if kind == "card":
        q["id"] = str(ident or "")
    if thumb:
        q["thumb"] = "1"
    return "/api/persona/background/file?" + urllib.parse.urlencode(q)


def info(kind, ident=""):
    """给前端的信息：原图 / 缩略图 URL、版本号、是否存在。"""
    p = path_for(kind, ident)
    if not p or not os.path.isfile(p):
        return {"url": "", "thumb": "", "version": 0, "exists": False}
    version = int(os.stat(p).st_mtime_ns)
    thumb = _thumb_path(p)
    return {
        "url": _url(kind, ident, version),
        "thumb": _url(kind, ident, version, thumb=True) if os.path.isfile(thumb) else "",
        "version": version,
        "exists": True,
    }


def resolve(who="", card_id="", allow_user=True):
    """账号背景 > 卡默认 > 全局默认 > 内置图 > 没有。"""
    if allow_user and who:
        i = info("user", who)
        if i["exists"]:
            return dict(i, source="user")
    if card_id:
        i = info("card", card_id)
        if i["exists"]:
            return dict(i, source="card")
    i = info("default", "")
    if i["exists"]:
        return dict(i, source="default")
    i = info("builtin", "")
    if i["exists"]:
        return dict(i, source="builtin")
    return {"url": "", "thumb": "", "version": 0, "exists": False,
            "source": "none"}


def save(fs, kind, ident=""):
    """把上传的 FileStorage 处理并落盘，返回 `info()`。

    抛 `ValueError` 表示这张图不能用；调用方把它变成 400。旧图只有在
    新图完整写完之后才会被 `os.replace` 覆盖，不会出现半张图。
    """
    if fs is None or not getattr(fs, "filename", ""):
        raise ValueError("没有收到图片")
    data = fs.read(MAX_BYTES + 1)
    if not data:
        raise ValueError("图片是空的")
    if len(data) > MAX_BYTES:
        raise ValueError("图片不能超过 8MB")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("不是可识别的图片")
    if str(img.format or "").upper() not in ("JPEG", "PNG", "WEBP"):
        # iPhone 相册可能是 HEIC（Safari 有时会转成 JPEG，有时不会）。
        raise ValueError("只支持 JPG / PNG / WebP；iPhone 的 HEIC 照片请先导出为 JPG")
    if img.width * img.height > MAX_PIXELS:
        raise ValueError("图片像素太大")
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    target = path_for(kind, ident)
    if not target:
        raise ValueError("不认识的背景类型")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    try:
        img.save(tmp, "WEBP", quality=QUALITY, method=6)
        os.replace(tmp, target)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass

    thumb = img.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
    tp = _thumb_path(target)
    tmp_t = tp + ".tmp"
    try:
        thumb.save(tmp_t, "WEBP", quality=THUMB_QUALITY, method=6)
        os.replace(tmp_t, tp)
    finally:
        try:
            if os.path.exists(tmp_t):
                os.unlink(tmp_t)
        except OSError:
            pass
    return info(kind, ident)


def delete(kind, ident=""):
    """删原图和缩略图；文件本来就不在也当成功。"""
    p = path_for(kind, ident)
    if not p:
        return
    for f in (p, _thumb_path(p)):
        try:
            if f and os.path.isfile(f):
                os.unlink(f)
        except OSError:
            pass

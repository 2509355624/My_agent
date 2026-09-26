"""对外发图前的一道处理：ComfyUI 原图带着完整工作流，发出去等于公开配方。

## 它解决什么

ComfyUI 会往 PNG 的 tEXt 块里塞一份工作流 JSON（`prompt` 键，实测约 2.5 KB）。
对方把图存下来拖进 ComfyUI，整条工作流（底模、lora、采样参数、提示词）就
还原出来了。QQ 群里发图是给外人看的，这份配方不该跟着走。

## 做法

发送前把图重新编码成 JPEG，元数据自然留不住；顺带把体积压到原图的 1/7
左右（实测 1.19 MB → 0.18 MB），群里发得快，也不占相册。**ComfyUI output
目录里的原图一个字节都不动**，自己复现、调试照旧。

## 关键取舍

- **转不出来就回落**：任何一步失败都退化成 ComfyUI 的原图 URL。宁可
  带着元数据发出去，也不能让图卡在这一步发不出来。
- **产物放系统 temp**：一次性文件不往项目目录里堆。每次写入顺手清掉
  一小时前的旧产物——NapCat 读文件是异步的，删早了会发不出去。
- **带 alpha 的先铺白底**：PNG 有透明通道时直接 convert("RGB") 会把透明
  区压成黑色，看着像坏图。
"""

import io
import logging
import os
import tempfile
import time
import uuid
from urllib.parse import quote

import requests

from app.config import COMFYUI_URL

log = logging.getLogger("image_out")

# 92 是肉眼与 PNG 几乎无差的位置（实测 920×1232 只要 0.18 MB）。
JPEG_QUALITY = 92

# 产物的保留时长（秒）。NapCat 拉取 file:// 是异步的，留足余量再删。
KEEP_SECONDS = 3600


# 与 qq_api 同款：回环地址不该被环境变量 / 注册表里的代理劫持——本机常驻
# Clash 之类，一劫持就变成 502。
_session = requests.Session()
_session.trust_env = False


def _fallback(filename):
    """原来的行为：把 ComfyUI 的原图 URL 交给协议端，由它自己去拉。"""
    return COMFYUI_URL.rstrip("/") + "/view?filename=" + quote(str(filename))


def _out_dir():
    d = os.path.join(tempfile.gettempdir(), "wb_qq_img")
    os.makedirs(d, exist_ok=True)
    return d


def _sweep(d):
    """清掉过期的旧产物。删不掉的（正被读）跳过就是。"""
    cutoff = time.time() - KEEP_SECONDS
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        fp = os.path.join(d, name)
        try:
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        except OSError:
            pass


def _flatten(im):
    """转成 JPEG 能吃的 RGB。有透明通道的先铺白底再合。"""
    from PIL import Image
    if im.mode in ("RGBA", "LA") or (im.mode == "P"
                                     and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def prepare_for_send(filename):
    """把一张 ComfyUI 输出图转成可对外发送的 JPEG。

    成功返回本地文件路径（协议端用 file:// 读它，见 qq_api.image_segment）；
    任何一步失败都返回 ComfyUI 的原图 URL，保证图照样发得出去。
    """
    try:
        from PIL import Image

        resp = _session.get(COMFYUI_URL.rstrip("/") + "/view",
                            params={"filename": str(filename)}, timeout=30)
        resp.raise_for_status()
        im = Image.open(io.BytesIO(resp.content))
        im.load()

        d = _out_dir()
        _sweep(d)
        stem = os.path.splitext(os.path.basename(str(filename)))[0] or "img"
        dst = os.path.join(d, "%s_%s.jpg" % (stem, uuid.uuid4().hex[:8]))
        _flatten(im).save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return dst
    except Exception as exc:
        log.warning("转 JPEG 失败，按原图发出 %s：%s", filename, exc)
        return _fallback(filename)

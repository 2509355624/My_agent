"""对外发图前的一道处理：按管理页开关决定「发压缩图」还是「发带工作流的原图」。

## 它解决什么

ComfyUI 会往 PNG 的 tEXt 块里塞一份工作流 JSON（`prompt` 键，实测约 2.5 KB）。
对方把图存下来拖进 ComfyUI，整条工作流（底模、lora、采样参数、提示词）就
还原出来了。手上这些工作流里没有密钥，配方本身不算秘密——但群聊里随手发图
默认没必要把配方一起给出去。

## 做法

- **jpg（默认）**：重编码成 JPEG，元数据自然留不住，顺带把体积压到原图的
  1/7 左右（实测 1.19 MB → 0.18 MB）。
- **png**（2026-10-18 用户拍板）：**原样发 ComfyUI 的原图**，字节一个不改，
  tEXt 里的 `prompt` / `workflow` 一并带出去——对方存下来拖进 ComfyUI 就能
  逐项复现，这是要的效果，不是事故。像素无损，体积等于原图。
- 两条路都是「另存一份」，**ComfyUI output 目录里的原图一个字节都不动**。

## 关键取舍

- **两种格式都必须是「另存一份」**：png 也不是把 output 里的原图直接交给协议端
  ——那份要留着自己复现调试，发出去的是 temp 里的一份副本（png 字节完全相同）。
- **转不出来就回落**：任何一步失败都退化成 ComfyUI 的原图 URL。宁可
  带着元数据发出去，也不能让图卡在这一步发不出来。
- **产物放系统 temp**：一次性文件不往项目目录里堆。每次写入顺手清掉过期
  的旧产物——NapCat 读文件是异步的，删早了会发不出去。
- **带 alpha 的先铺白底**：PNG 有透明通道时直接 convert("RGB") 会把透明
  区压成黑色，看着像坏图。这条只对 JPEG 那条路成立——png 是无损保留 alpha 的。
"""

import io
import logging
import os
import tempfile
import time
import uuid
from urllib.parse import quote

import requests

from app import comfy_out
from app.config import COMFYUI_URL

log = logging.getLogger("image_out")

# 92 是肉眼与 PNG 几乎无差的位置（实测 920×1232 只要 0.18 MB）。
JPEG_QUALITY = 92

# 支持的对外格式。generate_image 那边的取值见 agents.image_send_format。
FORMATS = ("jpg", "png")
DEFAULT_FORMAT = "jpg"

# 产物的保留时长（秒）。NapCat 拉取 file:// 是异步的，留足余量再删。
#
# 2026-10-01 从 3600 提到 86400：用户回头引用「几小时前那张」是常事，而引用
# 机器人自己发的图时，协议端回传的就是这个 file:// 路径（见 vision.
# _local_image_path）—— 1 小时一过就取不到图了（实测 12:20 引用 11:17 那张
# Anima_00455，文件已被 _sweep 清掉）。24 小时按每天 ~100 张、单张 1~3MB 算，
# 稳态也就几百 MB，_sweep 照旧按时间清。
KEEP_SECONDS = 86400


# 与 qq_api 同款：回环地址不该被环境变量 / 注册表里的代理劫持——本机常驻
# Clash 之类，一劫持就变成 502。
_session = requests.Session()
_session.trust_env = False


def _fallback(filename):
    """原来的行为：把 ComfyUI 的原图 URL 交给协议端，由它自己去拉。"""
    name, sub = comfy_out.split_rel(filename)
    url = COMFYUI_URL.rstrip("/") + "/view?filename=" + quote(name)
    if sub:
        # 输出图按会话分目录后（2026-10-18），单给 filename 会在 output 根下找，
        # 必然 404。subfolder 保留斜杠：ComfyUI 就是按路径拼的。
        url += "&subfolder=" + quote(sub, safe="/")
    return url


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


def prepare_for_send(filename, fmt=DEFAULT_FORMAT):
    """把一张 ComfyUI 输出图变成可对外发送的副本（fmt = "jpg" 或 "png"）。

    - "jpg"（默认）：重编码成 JPEG，PNG tEXt 里的工作流 JSON 自然留不住。
    - "png"（2026-10-18 用户拍板）：**原样搬 ComfyUI 的原图字节**，tEXt 里的
      `prompt` / `workflow` 一起发出去——这些工作流没有密钥，对方存下来拖进
      ComfyUI 就能复现，这是要的效果。只借 PIL 确认它真是 PNG（ComfyUI 回一页
      错误 JSON 时不能当图发出去），不做任何重新编码。

    发的都是 temp 里的副本，ComfyUI output 里的原图一个字节都不动。

    `filename` 可以是相对路径（`qq/群_1/anima/Anima_0001_.png`，2026-10-18 起
    `image_jobs.output_images` 返回的就是这个形态）：这里拆成 filename + subfolder
    再打 `/view`，调用方传什么都是同一个字符串，签名不变。

    成功返回本地文件路径（协议端用 file:// 读它，见 qq_api.image_segment）；
    任何一步失败都返回 ComfyUI 的原图 URL，保证图照样发得出去。
    """
    if fmt not in FORMATS:
        fmt = DEFAULT_FORMAT
    name, sub = comfy_out.split_rel(filename)
    try:
        from PIL import Image

        params = {"filename": name}
        if sub:
            params["subfolder"] = sub
        resp = _session.get(COMFYUI_URL.rstrip("/") + "/view",
                            params=params, timeout=30)
        if sub and getattr(resp, "status_code", 200) == 404:
            # 自愈（2026-10-18）：两种图会被报成 404 ——
            # ① 分目录之前生成的老图，实际躺在 output 根目录；
            # ② `BatchPromptImageGenerator` 明知图在子目录里，history 却把
            #    subfolder 报成空串（真机实测）。退回裸文件名再取一次，
            #    取不到就照旧走下面的回落，反正不能把图卡死在这一步。
            log.info("带 subfolder 取 %s 是 404，退回 output 根目录再试", name)
            resp = _session.get(COMFYUI_URL.rstrip("/") + "/view",
                                params={"filename": name}, timeout=30)
        resp.raise_for_status()
        im = Image.open(io.BytesIO(resp.content))
        im.load()

        d = _out_dir()
        _sweep(d)
        stem = os.path.splitext(os.path.basename(str(filename)))[0] or "img"
        dst = os.path.join(d, "%s_%s.%s" % (stem, uuid.uuid4().hex[:8], fmt))
        if fmt == "png":
            if im.format == "PNG":
                # 带工作流的原图：字节原样落盘，一个 tEXt 都不动。
                with open(dst, "wb") as f:
                    f.write(resp.content)
                return dst
            # 不是 PNG（某渠道 SaveImage 出了别的格式）→ 老老实实转一份，
            # 免得扩展名和内容对不上。
            from PIL import PngImagePlugin
            im.save(dst, "PNG", pnginfo=PngImagePlugin.PngInfo(),
                    optimize=True)
        else:
            _flatten(im).save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return dst
    except Exception as exc:
        log.warning("转 %s 失败，按原图发出 %s：%s", fmt, filename, exc)
        return _fallback(filename)


def save_bytes(data, ext=DEFAULT_FORMAT, stem="img"):
    """把一段字节（如 NAI 返回的 PNG）写到 temp，返回本地路径。

    与 prepare_for_send 同目录、同过期清理——NapCat 拉取 file:// 是异步的，
    删早了会发不出去。返回的路径可直接交给 qq_api.send_image。
    """
    d = _out_dir()
    _sweep(d)
    dst = os.path.join(d, "%s_%s.%s" % (stem, uuid.uuid4().hex[:8], ext))
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def local_size(path):
    """本地图的**实际像素尺寸**，形如 "1024×1536"；读不出来返回 ""。

    量的是 `prepare_for_send` 的产物，也就是对方真正收到的那张——工作流里的
    EmptyLatentImage 只是中间尺寸，后面还有 LatentUpscaleBy，拿它当「分辨率」
    会说小一截。

    失败一律返回空串：这行字只是给图加的附注，读不出来就不写这一项，绝不能
    因为它把图卡在这一步。path 是回落用的 ComfyUI URL 时也走这条路。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return "%d×%d" % (w, h)
    except Exception:
        return ""

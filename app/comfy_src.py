"""图生图的源图准备：从会话里取图 → 缩放 → 交给 ComfyUI 当输入。

## 它解决什么

垫图工作流要一张「输入图」，而群里的图在机器人手上只有一个腾讯图床直链
（`multimedia.nt.qq.com.cn`），且**从不落盘**——识图时下载成字节、转成
data URL 喂给识图模型就丢了。所以图生图必须把那份字节再取一次、按目标
尺寸缩好、塞进 ComfyUI 的 input 目录，工作流的 LoadImage 才认得。

## 取哪张

**只认本轮引用的那张图。** 对方引用一条带图的消息、@机器人说「用这个图生图」，
qq_bot 在开跑前把被引消息里的图记进线程本地（`qq_api.bind_context` 的
`quoted_images`），这里取来用。模型看不见图片地址（上下文里图只渲染成
`[图片]`），所以它只报「第几张」——引用里通常就一张，报 1 即那张；一条消息
里配了好几张图时，2、3 按被引消息里的先后往后数。

**没有引用就没有源图，不退回「本会话最近的图」。** 随手垫一张不相干的图，
比直接说一句「请引用那张图」麻烦得多——对方以为改的是自己那张，拿到手的
却是别人几轮前发的图。

## 关键取舍

- **QQ 会话只收引用**：模型在群里本来就没有链接，让它给链接或本地路径，
  要么是它编的，要么是被人诱导去读服务器上的文件——两种都不该接。网页端
  没这个顾虑，直接收 http 链接和本地绝对路径。
- **缩放在这边做，不挂工作流节点**：群友的图尺寸完全不可控（可能
  4000×3000 也可能 200×200），在工作流里做动态尺寸要连 GetImageSize 加
  算尺寸的节点，远不如 Pillow 一把梭简单。
- **失败就报错，不静默降级**：取不到图时返回一句实话。绝不能悄悄改成
  文生图——用户以为改的是自己那张图，拿到的却是凭空画的一张。
"""

import io
import os
import uuid

import requests

from app.config import COMFYUI_URL

# 源图最长边缩到多少。太大跑得慢、吃显存（Anima 那边后面还有一段精修），
# 太小出图糊。1216 是 SDXL 档位附近、Anima 也吃得住的折中。
MAX_SIDE = 1216

# 尺寸取 8 的倍数：VAE 的下采样步长是 8，不是整数倍会被内部裁掉几个像素。
_ALIGN = 8

# 与 qq_api / image_out 同款：回环地址不该被环境变量 / 注册表里的代理劫持，
# 本机常驻 Clash 之类，一劫持就是 502。
_session = requests.Session()
_session.trust_env = False

# 「就那张」的几种写法（模型可能直接照抄说明里的字）
_LATEST = ("", "last", "最新", "最近", "这张", "第一张")


def _is_url(s):
    return s.lower().startswith(("http://", "https://"))


def _pick(spec, count):
    """在 count 张里挑一张，返回下标（0 = 被引消息里的第一张）。

    spec 填数字 n 就是第 n 张，**正序**——这里不是「倒数第几张」：源图范围
    是本轮引用圈定的一小份确定性列表，正着数比倒着数符合直觉。空串和「这张」
    一类也当第一张（工具层已挡住空串，这里只是兜底）。写错或越界抛
    RuntimeError，消息能直接转述给用户。
    """
    if spec in _LATEST:
        return 0
    try:
        n = int(spec)
    except ValueError:
        raise RuntimeError("source_image 只能填数字（1 = 引用里的第一张图，"
                           "依次往后），不能填链接或路径，收到：" + spec)
    if n < 1 or n > count:
        raise RuntimeError("引用的消息里有 %d 张图，没有第 %d 张。" % (count, n))
    return n - 1


def resolve(spec):
    """把 source_image 参数解析成图片原始字节，返回 (字节, 来源说明)。

    QQ 会话走「本轮引用的那张图」（见模块开头的「取哪张」）；网页端收链接
    或本地路径。来源说明是给模型转述用的一句话（「引用的那张图」），免得它
    把垫了哪张说错。失败抛 RuntimeError。
    """
    from app import qq_api
    from app.vision import fetch_image

    spec = str(spec or "").strip()
    target, target_id = qq_api.current_context()

    if target is not None:
        # QQ 会话：只认本轮引用的图，取不到就说实话，不翻历史缓冲。
        imgs = qq_api.current_quoted_images()
        if not imgs:
            raise RuntimeError(
                "没看到引用的图片，垫不了图。图生图只认引用：让对方"
                "**引用一条带图的消息**再 @ 你一次，把想要的效果说清楚。"
                "只是把图发出来不算。")
        idx = _pick(spec, len(imgs))
        note = ("引用的那张图" if len(imgs) == 1
                else "引用的消息里的第 %d 张图" % (idx + 1))
        return fetch_image(imgs[idx]), note

    # 网页端：链接或本地文件都收
    if _is_url(spec):
        return fetch_image(spec), "链接图片"
    if spec and os.path.isfile(spec):
        with open(spec, "rb") as f:
            return f.read(), "本地文件 " + os.path.basename(spec)
    if not spec:
        raise RuntimeError("网页端要垫图得给出源图：图片链接或本地路径。")
    raise RuntimeError("找不到这个文件：" + spec)


def _flatten(im):
    """转成 VAE 能吃的样子。有透明通道的先铺白底再合（直接转会变黑块）。"""
    from PIL import Image
    if im.mode in ("RGBA", "LA") or (im.mode == "P"
                                     and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def fit(raw, max_side=MAX_SIDE):
    """把源图等比缩到长边 max_side，返回 (PNG 字节, (宽, 高))。

    比例不变形；尺寸取 8 的倍数。小图也会被放大——垫图的输出分辨率就等于
    这里的尺寸，不放大等于主动出小图。失败抛 RuntimeError。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise RuntimeError("本机没装 Pillow，缩放不了源图。")

    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)    # 手机直出照片带方向标记
        im.load()
    except Exception as exc:
        raise RuntimeError("这张图读不出来（%s），换个格式再试。" % exc)

    w, h = im.size
    if w <= 0 or h <= 0:
        raise RuntimeError("这张图的宽高是 %dx%d，没法当源图。" % (w, h))

    scale = float(max_side) / max(w, h)
    nw = max(64, int(round(w * scale / _ALIGN)) * _ALIGN)
    nh = max(64, int(round(h * scale / _ALIGN)) * _ALIGN)
    if (nw, nh) != (w, h):
        im = im.resize((nw, nh), Image.LANCZOS)

    buf = io.BytesIO()
    _flatten(im).save(buf, "PNG")
    return buf.getvalue(), (nw, nh)


def upload(raw, suffix=".png"):
    """把图片字节送进 ComfyUI 的 input 目录，返回它在那边认得的名字。

    `/upload/image` 是 ComfyUI 自带接口（实测返回 {"name","subfolder",...}），
    工作流的 LoadImage 就认这个名字。失败抛 RuntimeError。
    """
    name = "i2isrc_" + uuid.uuid4().hex[:10] + suffix
    try:
        resp = _session.post(COMFYUI_URL.rstrip("/") + "/upload/image",
                             files={"image": (name, raw, "image/png")},
                             data={"overwrite": "true"}, timeout=60)
        resp.raise_for_status()
        info = resp.json()
    except Exception as exc:
        raise RuntimeError("源图传不进 ComfyUI（%s），稍后再试。" % exc)

    got = str(info.get("name") or name)
    sub = str(info.get("subfolder") or "")
    return (sub + "/" + got) if sub else got

"""
图片识别：把图变成文字，交给没有视觉能力的模型。

为什么需要这一层：火山那条线（v4flash / v4-pro）是纯文本模型。把 base64 图
直接喂进去不会报错，而是**整条请求挂死**——实测 ReadTimeout 卡满 180 秒才断。
模型把那一长串 base64 当普通文本读，token 涨到几万，服务端一直不返回。所以
带图请求必须先在这里过一道：图 → 文字 → 正常进 agent 循环。

本模块只做三件事：下载 / 压缩 → 转 data URL → 调 VISION_PROVIDER 要一段文字。
不落盘、不进 history、不碰 agent 循环的任何状态。
"""

import base64
import io
import logging

import requests

from app.config import (PROVIDERS, VISION_MAX_EDGE, VISION_MODEL,
                        VISION_PROVIDER, VISION_TIMEOUT)

log = logging.getLogger("vision")

# 提示词：描述画面 + 原样提取文字。实测输出约 340 token，信息密度够用。
# 「原样」和「不要翻译」两句不能省——少了它们，模型会顺手把报错截图里的
# 英文翻成中文，而 agent 后续要靠原文去搜错误码。
_PROMPT = (
    "请描述这张图片的内容，并原样提取其中的所有文字。\n"
    "文字部分不要翻译、不要改写、不要总结，保持原有换行与顺序。"
    "如果图片里没有文字，只描述画面即可。"
)

# 压缩后再兜一道的字节上限。1024 长边 + JPEG q85 通常在 200KB 以内，
# 这里只是防 PNG 截图之类压不下来的东西把请求体撑大。
_MAX_BYTES = 4 * 1024 * 1024


# ─── 取图与压缩 ──────────────────────────────────────

def sniff_mime(raw):
    """从文件头判断图片类型（不依赖文件名后缀）。"""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def shrink_image(raw):
    """把图片压到最长边 VISION_MAX_EDGE，返回 (字节, mime)。

    PIL 不可用或图无法解码时原样返回——宁可让请求自己失败，也不要在这里
    抛异常把整轮对话打断（调用方按"看不到这张图"降级）。
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("PIL 不可用，图片不做压缩")
        return raw, sniff_mime(raw)

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:
        log.warning("图片无法解码，原样返回：%s", exc)
        return raw, sniff_mime(raw)

    w, h = im.size
    long_edge = max(w, h)
    if long_edge > VISION_MAX_EDGE > 0:
        scale = VISION_MAX_EDGE / float(long_edge)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                       Image.LANCZOS)

    # 带透明通道的图直接存 JPEG 会变黑底（截图、贴纸尤其明显），先铺白底
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    out = _encode_jpeg(im, 85)
    if len(out) > _MAX_BYTES:
        out = _encode_jpeg(im, 60)
    return out, "image/jpeg"


def _encode_jpeg(im, quality):
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def to_data_url(raw):
    """原始图片字节 → 可直接放进 messages 的 data URL。"""
    data, mime = shrink_image(raw)
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def fetch_image(url, timeout=None, max_bytes=None):
    """下载一张网络图片，返回原始字节。失败抛 RuntimeError。

    QQ 的图片段带的是腾讯图床的直链（multimedia.nt.qq.com.cn），实测 GET
    即可拿到 JPEG，不需要额外的鉴权头。
    """
    from app.config import QQ_IMAGE_MAX_BYTES, QQ_IMAGE_TIMEOUT

    limit = QQ_IMAGE_MAX_BYTES if max_bytes is None else max_bytes
    try:
        resp = requests.get(url, timeout=timeout or QQ_IMAGE_TIMEOUT)
    except Exception as exc:
        raise RuntimeError("图片下载失败：%s" % exc)
    if resp.status_code >= 400:
        raise RuntimeError("图片下载失败 HTTP %d" % resp.status_code)

    raw = resp.content
    if limit and len(raw) > limit:
        raise RuntimeError("图片过大（%d 字节，上限 %d）" % (len(raw), limit))
    if not raw:
        raise RuntimeError("图片内容为空")
    return raw


# ─── 识图 ────────────────────────────────────────────

def describe(data_url, timeout=None):
    """调 VISION_PROVIDER 识图，返回文字。失败抛 RuntimeError。

    只暴露"成功拿到文字"和"失败"两种结果，让调用方能用一句 try/except
    覆盖全部异常——识图失败不该让整轮对话挂掉，降级成"看不到这张图"即可。
    """
    pid = (VISION_PROVIDER or "").lower()
    cfg = PROVIDERS.get(pid)
    if not cfg:
        raise RuntimeError("识图 provider 未配置：%r" % VISION_PROVIDER)

    model = VISION_MODEL or cfg["model"]
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    headers = {
        "Authorization": "Bearer " + cfg["api_key"],
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=timeout or VISION_TIMEOUT)
    except Exception as exc:
        raise RuntimeError("识图请求失败：%s" % exc)

    if resp.status_code >= 400:
        raise RuntimeError("识图失败 HTTP %d：%s"
                           % (resp.status_code, resp.text[:200]))

    try:
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        raise RuntimeError("识图响应无法解析：%s" % exc)

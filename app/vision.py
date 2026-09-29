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
import time

import requests
# 直接引 urllib3 的 Timeout：requests 的 timeout 参数写数字时只是「单个 socket
# 操作」的上限，要「整次请求的总上限」只能用这个对象（requests 会原样透传，
# 见 requests/adapters.py 的 HTTPAdapter.send）。
from urllib3.util import Timeout as _UrllibTimeout

from app.config import (PROVIDERS, VISION_MAX_EDGE, VISION_MODEL,
                        VISION_PROVIDER, VISION_TIMEOUT)

log = logging.getLogger("vision")

# 下载图片与调识图接口都不走本机系统代理：本机常驻 Clash 类工具会把代理写进
# 注册表，代理进程一换端口或被杀，这两个出网口就全挂——图生图取图失败、识图
# 失败都是它引起的（2026-09-26 实撞：ProxyError 指向没人监听的 65532）。
# 与 qq_api / comfy_src / image_out / model_catalog 同款。
_session = requests.Session()
_session.trust_env = False

# ─── 总超时闸门 ──────────────────────────────────────
# requests 的 timeout 传数字时，那是**单个 socket 操作**的上限，不是整次请求的
# 上限：建连可以烧满一次，读又能再烧满一次。实测最坏一次识图吃掉 240 秒
# （120 建连 + 120 读），把整条会话线堵死——2026-09-29 群聊一轮 298 秒就是这么
# 来的（240 秒卡在识图，后面还有几十秒在等对话模型）。
# urllib3 的 Timeout(total=...) 才是真正的总闸门：读的配额 = total - 已耗时，
# 整次调用封顶在 total 之内（urllib3/connectionpool.py 里 read_timeout 就是
# 这么算出来再 settimeout 到 socket 上的）。
_CONNECT_TIMEOUT = 10


def _deadline(total, connect=_CONNECT_TIMEOUT):
    """把「整次请求的总时长上限」包成 requests 认识、urllib3 会真正执行的超时。

    建连单独给短值：连不上就快点失败，别把总预算耗在建连上。total 比 connect
    还小时取 total，免得构造出「连接超时 > 总超时」这种自相矛盾的配置。
    """
    total = float(total)
    return _UrllibTimeout(total=total, connect=min(connect, total), read=total)


# 提示词：描述画面 + 原样提取文字。实测输出约 340 token，信息密度够用。
# 「原样」和「不要翻译」两句不能省——少了它们，模型会顺手把报错截图里的
# 英文翻成中文，而 agent 后续要靠原文去搜错误码。
_PROMPT = (
    "请描述这张图片的内容，并原样提取其中的所有文字。\n"
    "文字部分不要翻译、不要改写、不要总结，保持原有换行与顺序。"
    "如果图片里没有文字，只描述画面即可。"
)

# 带用户问题的版本。识图模型看不见用户说了什么，只按上面那句通用指令读图，
# 下游文本模型拿到的就是一段泛泛的描述——用户问「这报错怎么解决」，它却可能
# 只答了画面里有几个人。把问题带进去，描述才服务于真实需求。
#
# 但「原样提取文字」这条不能因为加了问题就松掉（见 _PROMPT 的注释）。所以
# 是「先全量描述、再补需求重点」，而不是「只回答用户问的那一点」——用户问得
# 窄时，后者会把图里的版本号、错误码一并丢掉。
#
# 第一句是在点明「输出给谁看」：不自报身份，而是说明读者是个看不见图的模型，
# 这样它才会把话说全，而不是用「如图所示」这种对人有效、对模型无意义的措辞。
_PROMPT_WITH_QUESTION = (
    "你在替一个看不见图片的文本模型读图，你的输出是它唯一的眼睛。\n"
    "用户的需求：%s\n\n"
    "请先完整描述画面内容，并原样提取其中的所有文字"
    "（不要翻译、不要改写、不要总结，保持原有换行与顺序）；"
    "再针对上面的需求补充用户关心的细节。"
)

# 用户问题进 prompt 前的长度上限。QQ 群聊传进来的是「昵称：内容」多行拼接，
# 一轮静默窗口合并几条就能上千字，全塞进去只会挤占识图自己的输出预算。
QUESTION_MAX_CHARS = 300


def build_prompt(question="", index=0, total=1):
    """按用户问题组装识图 prompt。

    没问题时退回通用的 _PROMPT——「只发图不打字」是常见用法（"帮我看下
    这个"），不能因此拼出一个空的「用户的需求：」。

    total > 1 时在开头标出这是第几张：一批多图是逐张送进去的，不标的话
    模型会以为手上这张就是全部。
    """
    q = (question or "").strip()
    if not q:
        return _PROMPT
    if len(q) > QUESTION_MAX_CHARS:
        q = q[:QUESTION_MAX_CHARS] + "…"
    head = ""
    if total > 1:
        head = "（这是第 %d 张，共 %d 张）\n" % (index, total)
    return head + _PROMPT_WITH_QUESTION % q


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
        resp = _session.get(url, timeout=_deadline(timeout or QQ_IMAGE_TIMEOUT))
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

def describe(data_url, timeout=None, prompt=None):
    """调 VISION_PROVIDER 识图，返回文字。失败抛 RuntimeError。

    prompt 不传用默认的「描述画面+原样提取文字」；表情包打标签等场景
    传自己的。只暴露"成功拿到文字"和"失败"两种结果，让调用方能用一句
    try/except 覆盖全部异常——识图失败不该让整轮对话挂掉。
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
                {"type": "text", "text": prompt or _PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    headers = {
        "Authorization": "Bearer " + cfg["api_key"],
        "Content-Type": "application/json",
    }

    # 识图是**隐形调用**（每张图都要跑一次，请求数远多于对话轮数），但它自己
    # 发 HTTP、不经过 app/llm.py，所以从前完全不产生 `[llm]` 行——排查「哪家
    # 模型在拖时间」时会漏掉这一大块。打同一个前缀，`grep '\[llm\]'` 就能捞全。
    log.info("[llm] %s / %s vision", pid, model)

    started = time.monotonic()
    try:
        resp = _session.post(url, json=payload, headers=headers,
                             timeout=_deadline(timeout or VISION_TIMEOUT))
    except Exception as exc:
        raise RuntimeError("识图请求失败（耗时 %.1fs）：%s"
                           % (time.monotonic() - started, exc))

    if resp.status_code >= 400:
        raise RuntimeError("识图失败 HTTP %d：%s"
                           % (resp.status_code, resp.text[:200]))

    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError("识图响应无法解析：%s" % exc)

    # 识图是隐形调用，token 账不进主轮的 [cache] 统计——在这里单独归账，
    # 挂「vision」类别（跟 LLM 后台对账时，账就齐了）。
    try:
        from app import usage as usage_stats
        u = data.get("usage") or {}
        hit = u.get("prompt_cache_hit_tokens")
        miss = u.get("prompt_cache_miss_tokens")
        if hit is None:
            details = u.get("prompt_tokens_details") or {}
            hit = details.get("cached_tokens", 0)
            miss = (u.get("prompt_tokens") or 0) - (hit or 0)
        if (hit or 0) + (miss or 0) > 0:
            with usage_stats.scope("vision"):
                usage_stats.record(hit or 0, miss or 0,
                                   output=int(u.get("completion_tokens") or 0),
                                   provider=pid, model=model)
    except Exception:
        pass

    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        raise RuntimeError("识图响应无法解析（耗时 %.1fs）：%s"
                           % (time.monotonic() - started, exc))

    # 完成日志：从前只有开始那一条，一次识图卡住时日志上只剩个孤零零的起点，
    # 分不清是「卡在识图」还是「卡在后面的对话」（2026-09-29 排查就吃了这个亏：
    # 群聊那轮 298 秒，日志里只有一条 16:49:02 的识图起点，之后全静音）。
    # 故意不带 `[llm]` 前缀——那个前缀的语义是「一行 = 一次模型调用」，
    # 补一行完成日志不该让它变成两行。
    log.info("识图完成 %.1fs（%d 字）", time.monotonic() - started, len(text))
    return text

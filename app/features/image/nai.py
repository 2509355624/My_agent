"""NovelAI 云端生图客户端。

## 它解决什么

群里有个群主自己付钱的 NovelAI 账号，只想给**他那个群**用，不能让别的群
蹭。所以它跟本机 ComfyUI 完全隔离：不进全局串行队列的 ComfyUI 分支、不碰
显存/内存、不消耗本机算力——图由 NovelAI 的服务器直接出，worker 只负责把
字节发回原群。

## 做法

`generate(prompt, wide=False)` 把 prompt + 一套**写死**的参数（模型/尺寸/步数/
采样器/负向提示词……）POST 给 `https://image.novelai.net/ai/generate-image`，
返回体是一个 zip（里面 `image_0.png`），解出来就是 PNG 字节。prompt 之外的一切
都由这里定，模型那一侧只传 prompt。

**尺寸是唯一有岔路的地方，而且分成两套规则：**

- 文生图看**渠道**：竖版 832×1216（渠道 `nai`）和横版 1216×832（渠道
  `nai_wide`，2026-10-03 加）。两者像素数完全相同，所以耗时和成本一样。
- 图生图（垫图）看**源图比例**，跟渠道无关（2026-10-03 用户定）：垫图本就是
  「改造这张」，对方发了张竖图就不该被裁成横的。`prepare_image` 按源图比例
  折算出 NAI 能用的尺寸，并把这个尺寸一起返回给调用方。

## 关键取舍

- **代理**：本机所有出网口都 `trust_env=False`（为了 localhost 的 ComfyUI 不被
  注册表代理劫持，见 app/config 注释）。NAI 是外网，必须走代理——这里单独
  读注册表（`Internet Settings\\ProxyServer`）或显式 `NAI_PROXY`，不跟别的混。
- **群主 token 只进 .env**（`NAI_API_KEY`），绝不进仓库（.env 在 .gitignore）。
- **零真实调用进测试**：测试用 requests 的 mock 替换掉 Session，HTTP 一行都不发。
- **失败就抛**：429 限流 / 5xx / 网络错都原样抛给 image_jobs，由它转成一句
  对方能看的话。基础版不做重试（群主说群友会斟酌，先跑通再说）。
"""

import base64
import io
import logging
import math
import os
import random
import zipfile

import requests

try:
    import winreg
except ImportError:          # 非 Windows 上压根没有这个模块
    winreg = None

from app.config import IMAGE_GEN_TIMEOUT, NAI_API_KEY, NAI_PROXY

log = logging.getLogger("nai")

# NovelAI 当前的生图端点（旧的 api.novelai.net 已废弃，会在响应里让你刷新）。
NAI_ENDPOINT = "https://image.novelai.net/ai/generate-image"

# 默认写死的参数。模型只传 prompt，其余全在这里定。
# 2026-10-05 用户拍板：从 `nai-diffusion-5-curated`（V5 精选版）切到
# `nai-diffusion-5-full`（V5 完整版）——同一个 V5 架构、请求体一字不用改，
# 只是不套那层精选过滤。`nai` / `nai_wide` 两个渠道共用这一条。
NAI_MODEL = "nai-diffusion-5-full"             # V5 完整版
NAI_WIDTH = 832                                 # 竖版（默认，渠道 `nai`）
NAI_HEIGHT = 1216
# 横版（渠道 `nai_wide`，2026-10-03 加）：就是竖版转 90°，像素数完全相同，
# 所以单张耗时 / 额度成本跟竖版一模一样，只是构图方向不同。
NAI_WIDE_WIDTH = 1216
NAI_WIDE_HEIGHT = 832
NAI_STEPS = 23
NAI_SCALE = 5.0
NAI_SAMPLER = "k_euler"
NAI_NOISE_SCHEDULE = "karras"

# 一套通用的负向提示词（提高出图稳定性，不针对特定内容）。
NAI_NEGATIVE = (
    "lowres, bad anatomy, worst quality, blurry, bad hands, missing fingers, "
    "extra digits, fewer digits, cropped, jpeg artifacts, watermark, signature, "
    "text, error, mutated, deformed"
)

# 生成超时（秒）：(connect, read)。read 是「两次 socket 读之间」的上限，不是整
# 请求总时限——NAI 是「服务端憋着算完才吐字节」的用法，所以它实际就是「最多等
# 多久还没出图」。
#
# 2026-09-29：原来写死 240，比 ComfyUI 渠道的 IMAGE_GEN_TIMEOUT(180) 还宽，
# 于是 NAI 反而成了唯一没有 180 秒兜底的渠道。改成跟 IMAGE_GEN_TIMEOUT 同一个
# 数：两个渠道口径一致，以后改 .env 一处就同时生效。
NAI_TIMEOUT = (30, IMAGE_GEN_TIMEOUT)

# 图生图默认重绘强度（NAI 的 strength = 加多少噪声：小 = 贴着原图，大 = 改得
# 狠）。模型可用 denoise 参数覆盖，钳制在 [_I2I_STRENGTH_MIN, _I2I_STRENGTH_MAX]。
NAI_I2I_STRENGTH = 0.7
_I2I_STRENGTH_MIN, _I2I_STRENGTH_MAX = 0.1, 0.9

# 图生图的出图尺寸**不跟渠道走，跟源图比例走**（2026-10-03 用户定：垫一张
# 竖图进横屏渠道，不该被裁成横的——垫图本来就是「改造这张」，构图该跟原图）。
# NAI 硬要求宽高都是 64 的倍数，且像素越多越慢越贵，所以按源图比例把总像素
# 折算到「一块竖版基准」上。极端比例（很长的全景 / 很细的长条）会被各维上下限
# 夹住、比例略有偏差，此时 `prepare_image` 会先按最终比例做一次**最小**居中
# 裁剪再缩放，所以送进 NAI 的那张始终不变形。
_NAI_SIZE_MIN, _NAI_SIZE_MAX, _NAI_SIZE_STEP = 512, 1536, 64


def _snap_size(value):
    """吸到最近的 64 倍数，再钳进 [_NAI_SIZE_MIN, _NAI_SIZE_MAX]。"""
    snapped = int(round(value / float(_NAI_SIZE_STEP))) * _NAI_SIZE_STEP
    return max(_NAI_SIZE_MIN, min(_NAI_SIZE_MAX, snapped))


def fit_size(width, height):
    """源图 (宽, 高) → NAI 能用的出图尺寸 (宽, 高)，**保持源图比例**。

    总像素对齐竖版基准（832×1216 = 1011712），所以垫一张普通图的开销跟文生图
    相当，不会因为对方发了张 4K 大图就翻几十倍。读不出尺寸就用竖版兜底。
    """
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return NAI_WIDTH, NAI_HEIGHT
    if w <= 0 or h <= 0:
        return NAI_WIDTH, NAI_HEIGHT
    ratio = w / h
    budget = float(NAI_WIDTH * NAI_HEIGHT)
    tw = math.sqrt(budget * ratio)
    th = math.sqrt(budget / ratio)
    # 先**等比**压回上限——只砍长边不动短边的话，比例当时就歪了
    cap = min(_NAI_SIZE_MAX / tw, _NAI_SIZE_MAX / th, 1.0)
    tw, th = tw * cap, th * cap
    return _snap_size(tw), _snap_size(th)


def _nai_proxies():
    """决定 NAI 走哪条代理。

    显式配了 NAI_PROXY 就直接用；没配就读本机注册表里的系统代理（用户开 VPN
    后写在这儿）。读不到 / 没开就返回 None——直连（多半连不上，由调用方报错）。
    """
    if NAI_PROXY:
        return {"http": NAI_PROXY, "https": NAI_PROXY}
    if winreg is None:
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
        except Exception:
            server = ""
    except Exception:
        return None
    # 用户开的 VPN（nano）会把本地出口代理写进 ProxyServer，但 ProxyEnable
    # 常是 0（走虚拟网卡路由、没挂系统代理）。NAI 是外网，必须借这个本地代理
    # 出网——所以只要 ProxyServer 非空就用它，不卡 ProxyEnable。NAI_PROXY 仍能
    # 显式覆盖；直接连 image.novelai.net 多半超时（实测就是）。
    if not server:
        return None
    # server 可能是纯地址 "127.0.0.1:65532"，也可能是
    # "http=127.0.0.1:65532;https=127.0.0.1:65532" 这种分协议写法。
    if "=" in server:
        picked = None
        for part in server.split(";"):
            part = part.strip()
            if part.startswith("https="):
                picked = part[len("https="):]
                break
            if part.startswith("http="):
                picked = part[len("http="):]
        if picked is None:
            return None
        return {"http": picked, "https": picked}
    return {"http": server, "https": server}


def _build_body(prompt, seed, image_b64=None, strength=None,
                width=NAI_WIDTH, height=NAI_HEIGHT):
    """V5 必需的请求体。params_version=3 + v4_prompt/v4_negative_prompt
    两条 caption 结构是 V5 的硬性要求，缺了会 500。

    传 image_b64 就是图生图（action=img2img）：image 是**不带 data: 前缀**
    的纯 base64，strength = 重绘噪声（小 = 贴原图），noise 固定 0。

    `width`/`height` 由调用方定：文生图按渠道（竖版 / `nai_wide` 横版），
    图生图按源图比例（见 `fit_size`）。
    """
    body = {
        "input": prompt,
        "model": NAI_MODEL,
        "action": "img2img" if image_b64 else "generate",
        "parameters": {
            "params_version": 3,
            "prompt": prompt,
            "negative_prompt": NAI_NEGATIVE,
            "width": width,
            "height": height,
            "scale": NAI_SCALE,
            "sampler": NAI_SAMPLER,
            "steps": NAI_STEPS,
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": False,
            "dynamic_thresholding": False,
            "controlnet_strength": 1.0,
            "legacy": False,
            "add_original_image": False,
            "cfg_rescale": 0.0,
            "noise_schedule": NAI_NOISE_SCHEDULE,
            "skip_cfg_above_sigma": None,
            "use_coords": False,
            "legacy_v3_extend": False,
            "seed": seed,
            "v4_prompt": {
                "caption": {"base_caption": prompt, "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {"base_caption": NAI_NEGATIVE, "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
        },
    }
    if image_b64:
        body["parameters"]["image"] = image_b64
        body["parameters"]["strength"] = strength
        body["parameters"]["noise"] = 0.0
    return body


def _post_png(body, timeout, seed):
    """发请求 → 解包。文生图 / 图生图共用这一段（差异全在 body 里）。"""
    session = requests.Session()
    # 关键：不读环境变量 / 注册表（避免 localhost 的 ComfyUI 被劫持），
    # 代理由我们自己显式指定。
    session.trust_env = False
    proxies = _nai_proxies()
    if proxies:
        session.proxies = proxies

    params = body.get("parameters") or {}
    log.info("NAI 请求生成：action %s，模型 %s，%sx%s，seed %d，代理 %s",
             body.get("action"), NAI_MODEL, params.get("width"),
             params.get("height"), seed, ("是" if proxies else "否"))

    resp = session.post(
        NAI_ENDPOINT,
        headers={
            "Authorization": "Bearer " + NAI_API_KEY,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()

    # 不能靠 Content-Type：NAI 实际返回的是 binary/octet-stream（不是
    # application/zip），按 ctype 判断会把整个 zip 当 png 发出去——群里
    # 收的就是个坏图。看内容：ZIP 文件头 4 字节固定是 PK\x03\x04。
    data = resp.content
    if data[:4] == b"PK\x03\x04":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            names = z.namelist()
            if not names:
                raise RuntimeError("NAI 返回的 zip 是空的")
            return z.read(names[0])
        except zipfile.BadZipFile as exc:
            raise RuntimeError("NAI 返回的 zip 解不开：" + str(exc))
    # 极少数情况下直接返回 png 字节，也兜住。
    return data


def _checked_prompt(prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("NAI 需要非空的 prompt")
    return prompt


def generate(prompt, wide=False, timeout=NAI_TIMEOUT):
    """生成一张图（文生图），返回 PNG 字节；任何失败都抛异常。

    `wide=True` 出横版 1216×832（渠道 `nai_wide`），默认竖版 832×1216。除此之外
    只接 prompt（模型传来的画面描述），其余参数全写死（见模块注释）。token 缺失
    时直接抛，不让请求发出去撞 401。
    """
    if not NAI_API_KEY:
        raise RuntimeError("NAI_API_KEY 未配置（群主 token 应在 .env 里）")

    width, height = ((NAI_WIDE_WIDTH, NAI_WIDE_HEIGHT) if wide
                     else (NAI_WIDTH, NAI_HEIGHT))
    seed = random.randint(0, 2 ** 31 - 1)
    body = _build_body(_checked_prompt(prompt), seed,
                       width=width, height=height)
    return _post_png(body, timeout, seed)


def generate_img2img(prompt, image_b64, strength=NAI_I2I_STRENGTH,
                     width=NAI_WIDTH, height=NAI_HEIGHT, timeout=NAI_TIMEOUT):
    """图生图（垫图）：image_b64 是 prepare_image 产出的纯 base64。

    `width`/`height` 必须跟 `prepare_image` 那次算出来的**一致**——NAI 按请求里
    的尺寸出图，两边不一致就等于「垫一张 A 尺寸的图、要一张 B 尺寸的图」，构图
    会被重新摊开。调用方（image_jobs._process_nai）从入队快照里取这两个数。

    strength 语义见 NAI_I2I_STRENGTH 的注释；调用方（generate_image 工具）
    已做过钳制，这里不再二次裁剪——写错就直接让 NAI 报错，比悄悄改参数好。
    """
    if not NAI_API_KEY:
        raise RuntimeError("NAI_API_KEY 未配置（群主 token 应在 .env 里）")
    if not (image_b64 or "").strip():
        raise ValueError("NAI 图生图需要非空的源图 base64")

    seed = random.randint(0, 2 ** 31 - 1)
    body = _build_body(_checked_prompt(prompt), seed,
                       image_b64=image_b64, strength=strength,
                       width=width, height=height)
    return _post_png(body, timeout, seed)


def prepare_image(raw):
    """源图字节 → `(纯 base64, 宽, 高)`——后两个是**这次图生图的出图尺寸**。

    尺寸**跟着源图比例走**（2026-10-03 用户定），不跟渠道的横竖走：垫图本就是
    「改造这张」，构图该跟原图一致。折算规则见 `fit_size`（总像素对齐竖版基准、
    宽高吸到 64 的倍数）。返回的 base64 不带 data: 前缀。

    裁剪只为消掉 64 倍数取整引入的那点比例偏差（通常不到 5%），所以这里仍会
    居中裁一刀——不裁的话缩放就会轻微拉伸，脸会看得出来。带透明通道的先铺白底
    （同 vision/comfy_src 的做法，直接转会变黑块）。
    """
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:
        raise RuntimeError("这张图读不出来，垫不了图：%s" % exc)

    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    target_w, target_h = fit_size(im.size[0], im.size[1])

    # 按最终宽高比做最小居中裁剪
    target_ratio = target_w / float(target_h)
    w, h = im.size
    if w / float(h) > target_ratio:      # 太宽 → 裁两边
        new_w = max(1, int(round(h * target_ratio)))
        x0 = (w - new_w) // 2
        im = im.crop((x0, 0, x0 + new_w, h))
    elif w / float(h) < target_ratio:    # 太高 → 裁上下
        new_h = max(1, int(round(w / target_ratio)))
        y0 = (h - new_h) // 2
        im = im.crop((0, y0, w, y0 + new_h))
    if im.size != (target_w, target_h):
        im = im.resize((target_w, target_h), Image.LANCZOS)

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii"), target_w, target_h

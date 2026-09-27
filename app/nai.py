"""NovelAI 云端生图客户端。

## 它解决什么

群里有个群主自己付钱的 NovelAI 账号，只想给**他那个群**用，不能让别的群
蹭。所以它跟本机 ComfyUI 完全隔离：不进全局串行队列的 ComfyUI 分支、不碰
显存/内存、不消耗本机算力——图由 NovelAI 的服务器直接出，worker 只负责把
字节发回原群。

## 做法

`generate(prompt)` 把 prompt + 一套**写死**的参数（模型/尺寸/步数/采样器/负向
提示词……）POST 给 `https://image.novelai.net/ai/generate-image`，返回体是
一个 zip（里面 `image_0.png`），解出来就是 PNG 字节。prompt 之外的一切都
由这里定，模型那一侧只传 prompt。

## 关键取舍

- **代理**：本机所有出网口都 `trust_env=False`（为了 localhost 的 ComfyUI 不被
  注册表代理劫持，见 app/config 注释）。NAI 是外网，必须走代理——这里单独
  读注册表（`Internet Settings\\ProxyServer`）或显式 `NAI_PROXY`，不跟别的混。
- **群主 token 只进 .env**（`NAI_API_KEY`），绝不进仓库（.env 在 .gitignore）。
- **零真实调用进测试**：测试用 requests 的 mock 替换掉 Session，HTTP 一行都不发。
- **失败就抛**：429 限流 / 5xx / 网络错都原样抛给 image_jobs，由它转成一句
  对方能看的话。基础版不做重试（群主说群友会斟酌，先跑通再说）。
"""

import io
import logging
import os
import random
import zipfile

import requests

try:
    import winreg
except ImportError:          # 非 Windows 上压根没有这个模块
    winreg = None

from app.config import NAI_API_KEY, NAI_PROXY

log = logging.getLogger("nai")

# NovelAI 当前的生图端点（旧的 api.novelai.net 已废弃，会在响应里让你刷新）。
NAI_ENDPOINT = "https://image.novelai.net/ai/generate-image"

# 默认写死的参数。模型只传 prompt，其余全在这里定。
NAI_MODEL = "nai-diffusion-5-curated"          # V5 精选版
NAI_WIDTH = 832
NAI_HEIGHT = 1216
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

# 生成超时（秒）：connect 30s + read 240s。V5 出一张通常几十秒，给足余量。
NAI_TIMEOUT = (30, 240)


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


def _build_body(prompt, seed):
    """V5 必需的请求体。params_version=3 + v4_prompt/v4_negative_prompt
    两条 caption 结构是 V5 的硬性要求，缺了会 500。"""
    return {
        "input": prompt,
        "model": NAI_MODEL,
        "action": "generate",
        "parameters": {
            "params_version": 3,
            "prompt": prompt,
            "negative_prompt": NAI_NEGATIVE,
            "width": NAI_WIDTH,
            "height": NAI_HEIGHT,
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


def generate(prompt, timeout=NAI_TIMEOUT):
    """生成一张图，返回 PNG 字节；任何失败都抛异常。

    只接 prompt（模型传来的画面描述）；其余参数全写死（见模块注释）。token
    缺失时直接抛，不让请求发出去撞 401。
    """
    if not NAI_API_KEY:
        raise RuntimeError("NAI_API_KEY 未配置（群主 token 应在 .env 里）")

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("NAI 需要非空的 prompt")

    seed = random.randint(0, 2 ** 31 - 1)
    body = _build_body(prompt, seed)

    session = requests.Session()
    # 关键：不读环境变量 / 注册表（避免 localhost 的 ComfyUI 被劫持），
    # 代理由我们自己显式指定。
    session.trust_env = False
    proxies = _nai_proxies()
    if proxies:
        session.proxies = proxies

    log.info("NAI 请求生成：模型 %s，%dx%d，seed %d，代理 %s",
             NAI_MODEL, NAI_WIDTH, NAI_HEIGHT, seed,
             ("是" if proxies else "否"))

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

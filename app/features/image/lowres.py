"""工作台的「低分辨率档」：画布降一档采样，放大链按算术补回来。

## 它解决什么

工作台（以及将来的访客模式）发出去的图本来就要压到 ≤1MB 的 WebP，可
ComfyUI 那边还是老老实实跑 1024×1536 再 2x 放大，出一张七八兆的原图，
然后我们把它缩掉九成再发出去。用户 2026-11-26 的原话：「为什么要先生成
一个完整的七八兆的原图，然后再压缩发送？为什么不干脆直接生成低分辨率的
图片？」

**只降画布不行**。各渠道的放大链是**倍率**（`LatentUpscaleBy.scale_by`
= 1.2、`ImageUpscaleWithModel` = 2x 模型），画布一降，成品跟着等比变小，
成品就比现在那档糊一圈。所以这里做的是：画布降一档、倍率乘回去，
**成品尺寸不变**，省掉的只是第一段在大画布上的采样。

## 硬边界

- 成品像素 = baseline 档。想要「更糊但更快」是同一个旋钮的另一个方向，
  调 `CANVAS_RATIO`，不是删掉倍率补偿。
- 画布维度必须能被 8 整除（VAE 下采样 8 倍），否则 ComfyUI 直接报错。
- 只认工作流里真有的两类节点；认不出就**原样不动**，当成「这个渠道不吃
  这一档」，绝不猜、绝不硬塞。
- 补不回来（需要的倍率太大）也**原样不动**——宁可跑原来那档，也不做二阶
  放大再延迟采样那种赔本买卖。
"""

import logging

log = logging.getLogger(__name__)

# 画布边长缩到原来的几分之一（线性）。0.5 ≈ 1024×1536 → 512×768。
CANVAS_RATIO = 0.5

# 延迟倍率（LatentUpscaleBy）上限。超过它就是在高分辨率上重采样，省下来的
# 采样时间又赔回去。
_MAX_LATENT = 4.0

# 模型放大（ImageUpscaleWithModel）上限：一个节点 2x，最多两级 4x。
_MAX_MODEL = 8.0


def _round8(v):
    return max(8, int(round(float(v) / 8.0)) * 8)


def _setup_nodes(workflow):
    """挑出「画布 + 放大」两类节点 id。

    画布有两类写法：`Empty*Latent*`（大多数动漫系）和
    `BatchPromptImageGenerator`（jank / miao / image_gen_v1 那批）。两者都
    带 width/height，都按 8 的倍数出图，同一套算术就能处理。
    """
    empty, latents, upscalers = [], [], []
    for nid, node in (workflow or {}).items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type") or ""
        ins = node.get("inputs") or {}
        if ("width" in ins and "height" in ins
                and (ct.startswith("Empty") or "PromptImageGenerator" in ct)):
            empty.append(nid)
        elif ct == "LatentUpscaleBy":
            latents.append(nid)
        elif ct == "ImageUpscaleWithModel":
            upscalers.append(nid)
    return empty, latents, upscalers


def supports(workflow):
    """这个工作流吃不吃「降一档」（有画布就吃；放大链没有也能降，只是更糊）。"""
    empty, latents, upscalers = _setup_nodes(workflow)
    return bool(empty and (latents or upscalers))


def _factor(workflow, latents, upscalers):
    """这条链现在一共放大了多少倍。"""
    f = 1.0
    for lid in latents:
        try:
            f *= float(workflow[lid]["inputs"].get("scale_by") or 1.0)
        except (TypeError, ValueError, KeyError):
            pass
    return f * (2.0 ** len(upscalers))


def plan(workflow):
    """只算不改：返回降档方案 dict。`on=False` 表示这个渠道不动。"""
    empty, latents, upscalers = _setup_nodes(workflow)
    if not empty:
        return {"on": False, "why": "没有画布节点"}
    if not (latents or upscalers):
        return {"on": False, "why": "没有放大链，降了成品会变小"}

    try:
        bw = int(workflow[empty[0]]["inputs"].get("width") or 0)
        bh = int(workflow[empty[0]]["inputs"].get("height") or 0)
    except (TypeError, ValueError):
        return {"on": False, "why": "画布宽高不是数字"}
    if not bw or not bh:
        return {"on": False, "why": "画布宽高为 0"}

    cw, ch = _round8(bw * CANVAS_RATIO), _round8(bh * CANVAS_RATIO)
    if cw >= bw or ch >= bh:
        return {"on": False, "why": "画布本来就不大"}

    # 成品 = 画布 × 总倍率。画布缩了 k 倍，总倍率就得乘 k 倍才补得回来。
    k = float(bw) / cw
    cur = _factor(workflow, latents, upscalers)
    need = cur * k

    # 模型放大先吃（不占采样时间），剩下的落到延迟倍率上。
    model = min(_MAX_MODEL, max(cur, need / min(_MAX_LATENT, need / cur)))
    if model * _MAX_LATENT < need:
        return {"on": False,
                "why": "要 %.1fx 才能补回成品，超过上限" % need}

    lat = need / model
    if not latents:
        # 只有模型放大：模型放大是整数级的（一个节点 = 2x），补不出 1.15
        # 这种零头。这时候允许成品略小于原来那档——仍然远高于手机屏，
        # 而且这就是「降一档」本来的意思。
        lat = 1.0
    if lat > _MAX_LATENT:
        return {"on": False, "why": "延迟倍率 %.1fx 太高" % lat}

    return {"on": True,
            "canvas": (cw, ch),
            "from": (bw, bh),
            "latent": round(lat, 2),
            "model": int(round(model / 2.0)) if upscalers else 0,
            "est": (int(cw * lat * model), int(ch * lat * model)),
            "scale": round(k, 2)}


def apply(workflow, **kw):
    """原地把工作流改成降档版；返回 `plan()` 的结果（`on=False` 时一字未动）。"""
    p = plan(workflow)
    if not p.get("on"):
        return p
    try:
        empty, latents, upscalers = _setup_nodes(workflow)
        ins = workflow[empty[0]]["inputs"]
        ins["width"], ins["height"] = p["canvas"]
        if "hires_width" in ins and "hires_height" in ins:
            ins["hires_width"], ins["hires_height"] = p["canvas"]
        if latents and p["latent"] != 1.0:
            for lid in latents:
                workflow[lid]["inputs"]["scale_by"] = p["latent"]
        # 模型放大不用改节点：`ImageUpscaleWithModel` 本身就是吃模型倍率，
        # 「2x 模型连挂两级」在模板里是两条节点，这里只改画布和延迟倍率。
    except (KeyError, TypeError, ValueError):
        log.warning("低分辨率档改写失败，按原工作流跑", exc_info=True)
        return {"on": False, "why": "改写工作流时出错"}
    return p

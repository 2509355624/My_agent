"""工作台的「低分辨率档」：画布降一档采样，延迟放大按算术补回来。

## 它解决什么

工作台（以及将来的访客模式）发出去的图本来就要压到 ≤1MB 的 WebP，可
ComfyUI 那边还是老老实实跑 1024×1536 再 2x 放大，出一张七八兆的原图，
然后我们把它缩掉九成再发出去。用户 2026-11-26 的原话：「为什么要先生成
一个完整的七八兆的原图，然后再压缩发送？为什么不干脆直接生成低分辨率的
图片？」

**只降画布不行**。画布一降，`LatentUpscaleBy` 后面那段跟着等比变小，
成品就比现在那档糊一圈。所以这里做的是：画布降一档、`scale_by` 乘回去，
**成品尺寸不变**，省掉的只是第一段在大画布上的采样。

## 关键：只补「采样段」，不补模型放大

`ImageUpscaleWithModel` 的倍率由**模型文件**定死（2x_Ani4Kv2 就是 2x），
模板里没有旋钮。它作用在采样结果上、跟画布无关，所以**不该参与补偿**：

    原尺寸  1024 ×lat1.2 → 1229 → 对齐8 1232 → ×2模型 = 2464
    降档     512 ×lat2.41 → 1232 → ×2模型 = 2464   ✓

早期写成「原总倍率 × 缩放系数」（1.2×2×2 = 4.8），把模型那 2x 也补了一
遍，成品直接从 2464×3680 掉到 2048×3072（2026-11-26 实测抓到的）。

## 硬边界

- 成品尺寸 = baseline 档。想要「更糊但更快」是另一个旋钮（`CANVAS_RATIO`）。
- 画布维度必须能被 8 整除（VAE 下采样 8 倍），否则 ComfyUI 直接报错。
- **只有模型放大、没有 `LatentUpscaleBy`** 的渠道不降：没有可调的倍率，
  画布减半成品就减半。宁可跑原尺寸。
- 认不出画布 / 需要的倍率超上限 → **原样不动**，绝不猜、绝不硬塞。
"""

import logging

log = logging.getLogger(__name__)

# 画布边长缩到原来的几分之一（线性）。0.5 ≈ 1024×1536 → 512×768。
CANVAS_RATIO = 0.5

# 延迟倍率（LatentUpscaleBy）上限。超过它就是在高分辨率上重采样，省下来的
# 采样时间又赔回去。
_MAX_LATENT = 4.0


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


def _latent_factor(workflow, latents):
    """延迟放大那一段（采样后、模型放大前）单独的倍率。

    **模型放大不参与补偿**：`ImageUpscaleWithModel` 的倍率由模型文件定死
    （2x_Ani4Kv2 就是 2x），模板里改不了它的倍数。要补的是「采样出来的
    那张图」的像素，不是成品像素——成品那一段本来就不受画布影响。
    """
    f = 1.0
    for lid in latents:
        try:
            f *= float(workflow[lid]["inputs"].get("scale_by") or 1.0)
        except (TypeError, ValueError, KeyError):
            pass
    return f


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

    # ── 要补回来的是什么 ───────────────────────────────────
    # 只补「采样出来的那张图」（模型放大之前）。模型放大是 2x 模型，倍率
    # 由模型文件定死、模板里改不了，而它作用在采样结果上、跟画布无关：
    #
    #   原尺寸  1024 ×lat1.2 → 1229 → 对齐8 1232 → ×2模型 = 2464
    #   降档     512 ×lat? → 要 1232 → lat = 1232/512 = 2.41 → ×2模型 = 2464
    #
    # 所以 lat 的算法是「原链采样段像素 / 新画布」，**不是**「原总倍率 × k」。
    # 早期写成后者（1.2×2×2 = 4.8）会把模型那 2x 也算进去补一遍，成品直接
    # 缩一半——2026-11-26 实测 2464×3680 变 2048×3072 就是这么来的。
    if not latents:
        # 只有模型放大、没有 LatentUpscaleBy：**不降**。
        # `ImageUpscaleWithModel` 的倍率由模型文件定死（2x_Ani4Kv2 就是 2x），
        # 模板里没有旋钮可调，画布减半就真的只剩一半成品（2048→1024）。
        # 这一类（qwen / jank / miao / anima-flesh 等 8 个）宁可跑原尺寸。
        return {"on": False, "why": "只有模型放大，倍率不可调，降了成品会小一半"}

    base_lat = _latent_factor(workflow, latents)
    # 原链采样段的像素（对齐 8：latent 空间是 8 倍下采样的，对不齐会被截断）
    target_w = _round8(bw * base_lat)
    target_h = _round8(bh * base_lat)

    lat = max(target_w / float(cw), target_h / float(ch))
    if lat > _MAX_LATENT:
        return {"on": False, "why": "延迟倍率 %.1fx 太高" % lat}

    model = 2.0 ** len(upscalers)
    return {"on": True,
            "canvas": (cw, ch),
            "from": (bw, bh),
            "latent": round(lat, 3),
            "model": int(round(model / 2.0)) if upscalers else 0,
            "est": (_round8(cw * lat) * int(model), _round8(ch * lat) * int(model)),
            "scale": round(float(bw) / cw, 2)}


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
        if latents:
            # 走到这里 latents 必然非空（没 LatentUpscaleBy 的已经在 plan 里拒了）
            for lid in latents:
                workflow[lid]["inputs"]["scale_by"] = p["latent"]
        # 模型放大不用改节点：`ImageUpscaleWithModel` 本身就是吃模型倍率，
        # 「2x 模型连挂两级」在模板里是两条节点，这里只改画布和延迟倍率。
    except (KeyError, TypeError, ValueError):
        log.warning("低分辨率档改写失败，按原工作流跑", exc_info=True)
        return {"on": False, "why": "改写工作流时出错"}
    return p

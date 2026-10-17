# -*- coding: utf-8 -*-
"""外来工作流体检：拿 object_info 比对节点类型 + 查每个模型在不在。

用法：python _health_anima2.py
"""
import json
import os
import urllib.request

BASE = "http://127.0.0.1:8188"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)


def oi(node):
    with urllib.request.urlopen(BASE + "/object_info/" + node, timeout=20) as r:
        return json.load(r).get(node)


# 节点类型 -> (取哪个输入的名字当「可选值列表」)
NEEDED_NODES = {
    "UNETLoader": "unet_name",
    "LoraLoaderModelOnly": "lora_name",
    "CLIPLoader": "clip_name",
    "VAELoader": "vae_name",
    "UpscaleModelLoader": "model_name",
    "KSampler": "sampler_name",
    "LatentUpscaleBy": "upscale_method",
    "EmptyLatentImage": None,
    "CLIPTextEncode": None,
    "VAEDecode": None,
    "SaveImage": None,
    "ImageUpscaleWithModel": None,
}

NEEDED_MODELS = {
    "UNETLoader": ["miaomiaoRealskin_anima13.safetensors",
                   "miaomiaoAnimeReality_ani11_3087842.safetensors",
                   "silvermoonmixAnimaEvolved_v2329BTurbo.safetensors"],
    "LoraLoaderModelOnly": ["star cat-000022.safetensors",
                            "ANIMA_MODARE_V2.safetensors",
                            "_cowani_epoch_10.safetensors",
                            "reina_v1_epoch14.safetensors",
                            "anima_context_detailer_base10.safetensors",
                            "Anima_in_real_update_v2_epoch_20.safetensors"],
    "CLIPLoader": ["qwen_3_06b_base.safetensors"],
    "VAELoader": ["qwen_image_vae.safetensors"],
    "UpscaleModelLoader": ["2x_Ani4Kv2_G6i2_Compact_107500.pth"],
}

print("=" * 70)
print("节点类型体检")
print("=" * 70)
info = {}
for node, field in NEEDED_NODES.items():
    try:
        d = oi(node)
    except Exception as e:
        print("  %-24s 查询失败 %s" % (node, e))
        continue
    if not d:
        print("  %-24s ✗ 节点不存在" % node)
        continue
    info[node] = d
    inp = d.get("input", {}).get("required", {})
    if field and field not in inp:
        print("  %-24s ✗ 节点在，但没有输入 %s" % (node, field))
    else:
        print("  %-24s ✓" % node)

def _options(d, field):
    """取某个输入的可选值列表。

    ⚠️ 新版 ComfyUI 有些节点把可选值包成 ["COMBO", {"options": [...]}]，
    老格式是 [[...]]。两种都要认——只认后者会把「有 7 个模型」读成 0 个。
    """
    spec = d["input"]["required"].get(field)
    if not spec:
        return []
    if isinstance(spec[0], list):
        return spec[0]
    if isinstance(spec[0], str) and spec[0] == "COMBO" and len(spec) > 1:
        return (spec[1] or {}).get("options") or []
    return []


print("=" * 70)
print("模型体检")
print("=" * 70)
for node, names in NEEDED_MODELS.items():
    d = info.get(node)
    if not d:
        print("  [%s] 拿不到 object_info，跳过" % node)
        continue
    field = NEEDED_NODES[node]
    avail = _options(d, field)
    print("  [%s] 可选 %d 个" % (node, len(avail)))
    for n in names:
        hit = n in avail
        print("      %s %s" % ("✓" if hit else "✗ 缺失", n))
        if not hit:
            near = [a for a in avail if os.path.basename(n).split(".")[0][:14].lower()
                    in a.lower()]
            for x in near[:4]:
                print("          ~ 相近: %s" % x)

print()
print("=" * 70)
print("KSampler 支持的采样器里有 euler / er_sde 吗")
print("=" * 70)
d = info.get("KSampler")
if d:
    s = _options(d, "sampler_name")
    for want in ("euler", "er_sde"):
        print("  %-8s %s" % (want, "✓" if want in s else "✗"))
d = info.get("LatentUpscaleBy")
if d:
    u = _options(d, "upscale_method")
    print("  LatentUpscaleBy nearest-exact: %s" % ("✓" if "nearest-exact" in u else "✗"))

# -*- coding: utf-8 -*-
"""决定性验证：不加载权重，用 meta 设备造出 qwen_image21 的模型结构，
拿到它真实的 state_dict 键名，再**照抄 ComfyUI comfy/lora.py 的键映射逻辑**
（通用分支 + QwenImage 分支），看 LoRA 的每个键能不能被映射到。

meta 设备 = 只占形状不占内存，安全。
"""
import sys
import struct
import json
import collections

sys.path.insert(0, r"D:\AI\ComfyUI_v037")

import torch  # noqa: E402

GGUF = r"D:\AI\ComfyUI-backup\models\diffusion_models\qwen-image-2.1-UC-Q4_K_M.gguf"
LORAS = [
    r"D:\AI\ComfyUI-backup\models\loras\Qwen-Image-2.1 Manga to Photo V1.safetensors",
    r"D:\AI\ComfyUI-backup\models\loras\qwen-image-2.1-restore.safetensors",
]

T = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?",
     10: "Q", 11: "q", 12: "d"}


def rd(f, fmt):
    return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]


def rd_str(f):
    return f.read(rd(f, "<Q")).decode("utf-8", "replace")


def rd_val(f, t):
    if t == 8:
        return rd_str(f)
    if t == 9:
        et = rd(f, "<I")
        n = rd(f, "<Q")
        return [rd_val(f, et) for _ in range(n)]
    return rd(f, "<" + T[t])


with open(GGUF, "rb") as f:
    assert f.read(4) == b"GGUF"
    rd(f, "<I")
    n_tensor = rd(f, "<Q")
    n_kv = rd(f, "<Q")
    for _ in range(n_kv):
        rd_str(f)
        rd_val(f, rd(f, "<I"))
    gg = []
    for _ in range(n_tensor):
        nm = rd_str(f)
        nd = rd(f, "<I")
        dims = [rd(f, "<Q") for _ in range(nd)]
        rd(f, "<I")
        rd(f, "<Q")
        gg.append((nm, list(reversed(dims))))   # gguf 维度是反的 → 转成 torch 顺序

shape = {n: d for n, d in gg}

# ---- 用 GGUF 的形状直接推 dit_config（照 model_detection.py:962-981 的口径）----
inner_dim = shape["img_in.weight"][0]
head_dim = shape["transformer_blocks.0.attn.norm_q.weight"][0]
num_layers = len({n.split(".")[1] for n in shape if n.startswith("transformer_blocks.")})
gate_up = shape.get("transformer_blocks.0.img_mlp.gate_up.weight")
cfg = dict(
    in_channels=shape["img_in.weight"][1],
    out_channels=shape["proj_out.weight"][0],
    num_layers=num_layers,
    attention_head_dim=head_dim,
    num_attention_heads=inner_dim // head_dim,
    context_in_dim=shape["txt_in.text_norm.weight"][0],
    mlp_ratio=(gate_up[0] // 2 // inner_dim) if gate_up else
              (shape["transformer_blocks.0.img_mlp.proj.weight"][0] // inner_dim),
    fused_mlp=gate_up is not None,
)
print("推断 dit_config：")
for k, v in cfg.items():
    print("   %-22s = %s" % (k, v))
print("   img_mlp 在 gguf 里的形态：",
      "gate_up（融合）" if gate_up else "gate_layer + proj（未融合）")

# ---- 在 meta 上造模型，拿真实键名 ----
import comfy.ops  # noqa: E402
from comfy.ldm.qwen_image21.model import QwenImage21Transformer2DModel  # noqa: E402

model = QwenImage21Transformer2DModel(
    **cfg, dtype=torch.float16, device="meta",
    operations=comfy.ops.disable_weight_init)
mkeys = list(model.state_dict().keys())
print("模型 state_dict 键 %d 个；img_mlp 样例：" % len(mkeys))
for k in mkeys:
    if "img_mlp" in k:
        print("   ", k, tuple(model.state_dict()[k].shape))
        break

# ---- 照抄 comfy/lora.py 的映射（通用分支 + QwenImage 分支）----
sd = {"diffusion_model." + k: model.state_dict()[k] for k in mkeys}
key_map = {}
# 通用分支（lora.py:191-198）
for k in sd:
    if k.startswith("diffusion_model."):
        if k.endswith(".weight"):
            key_map["lora_unet_{}".format(k[16:-7].replace(".", "_"))] = k
            key_map["{}".format(k[:-7])] = k
        else:
            key_map["{}".format(k)] = k
# QwenImage 分支（lora.py:326-339）
for k in sd:
    if k.startswith("diffusion_model.") and k.endswith(".weight"):
        key_lora = k[16:-7]
        targets = [(key_lora, k)]
        if key_lora.endswith(".img_mlp.gate_up"):
            half = sd[k].shape[0] // 2
            targets = [(key_lora.replace(".gate_up", ".gate_layer"), (k, (0, 0, half))),
                       (key_lora.replace(".gate_up", ".proj"), (k, (0, half, half)))]
        for kl, to in targets:
            key_map["{}".format(kl)] = to
            key_map["transformer.{}".format(kl)] = to
            key_map["lycoris_{}".format(kl.replace(".", "_"))] = to

print()
for lp in LORAS:
    with open(lp, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        head = json.loads(f.read(n).decode("utf-8"))
    keys = [k for k in head if k != "__metadata__"]
    hit = miss = 0
    miss_sample = []
    for k in keys:
        b = k
        for suf in (".lora_A.weight", ".lora_B.weight"):
            if b.endswith(suf):
                b = b[:-len(suf)]
                break
        if b in key_map or ("transformer." + b) in key_map:
            hit += 1
        else:
            miss += 1
            if len(miss_sample) < 6:
                miss_sample.append(b)
    print("=" * 78)
    print(lp.rsplit("\\", 1)[-1])
    print("  张量 %d：能映射 %d，映射不上 %d" % (len(keys), hit, miss))
    if miss_sample:
        print("  映射不上的样例：", miss_sample)

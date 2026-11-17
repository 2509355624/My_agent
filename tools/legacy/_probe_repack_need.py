# -*- coding: utf-8 -*-
"""决定性验证（只读）：
   1) 本机 GGUF 的 img_mlp 是融合(gate_up)还是未融合(gate_layer+proj)
   2) 待装的 restore LoRA 的 img_mlp 键名与形状
   3) 照抄 comfy/lora.py 的映射逻辑，确认到底丢哪些键
"""
import sys, json, struct

sys.path.insert(0, r"D:\AI\ComfyUI_v037")
import torch  # noqa: E402

GGUF = r"D:\AI\ComfyUI-backup\models\diffusion_models\qwen-image-2.1-UC-Q4_K_M.gguf"
LORA = r"D:\AI\temp\qwen-image-2.1-restore.safetensors"

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


# ---------- 1) GGUF 形状 ----------
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
        gg.append((nm, list(reversed(dims))))

shape = {n: d for n, d in gg}
print("=" * 78)
print("GGUF img_mlp 相关键（block 0）：")
for n in sorted(shape):
    if "img_mlp" in n and ".0." in n:
        print("   %-60s %s" % (n, shape[n]))
gate_up = shape.get("transformer_blocks.0.img_mlp.gate_up.weight")
print("   >>> 融合(gate_up)?", gate_up is not None)

inner_dim = shape["img_in.weight"][0]
head_dim = shape["transformer_blocks.0.attn.norm_q.weight"][0]
num_layers = len({n.split(".")[1] for n in shape if n.startswith("transformer_blocks.")})
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
print("   推断 cfg：", cfg)

# ---------- 2) LoRA 头 ----------
with open(LORA, "rb") as f:
    n = struct.unpack("<Q", f.read(8))[0]
    head = json.loads(f.read(n).decode("utf-8"))
keys = [k for k in head if k != "__metadata__"]
print("=" * 78)
print("LoRA:", LORA.rsplit("\\", 1)[-1], "张量数:", len(keys))
print("  block0 img_mlp 键：")
for k in sorted(keys):
    if "img_mlp" in k and ".0." in k:
        print("   %-72s %s" % (k, head[k]["shape"]))
gu = [k for k in keys if "gate_up" in k]
print("  gate_up 键数:", len(gu))

# ---------- 3) 模型真实键名 ----------
import comfy.ops  # noqa: E402
from comfy.ldm.qwen_image21.model import QwenImage21Transformer2DModel  # noqa: E402

model = QwenImage21Transformer2DModel(
    **cfg, dtype=torch.float16, device="meta",
    operations=comfy.ops.disable_weight_init)
msd = model.state_dict()
print("=" * 78)
print("模型 state_dict 的 img_mlp 键（block 0）：")
for k in msd:
    if "img_mlp" in k and ".0." in k:
        print("   %-60s %s" % (k, tuple(msd[k].shape)))

sd = {"diffusion_model." + k: msd[k] for k in msd}
key_map = {}
for k in sd:
    if k.startswith("diffusion_model."):
        if k.endswith(".weight"):
            key_map["lora_unet_{}".format(k[16:-7].replace(".", "_"))] = k
            key_map["{}".format(k[:-7])] = k
        else:
            key_map["{}".format(k)] = k
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
        if len(miss_sample) < 8:
            miss_sample.append(b)
print("=" * 78)
print("映射结果：能映射 %d / 映射不上 %d（共 %d）" % (hit, miss, len(keys)))
print("  映射不上的样例：", miss_sample)

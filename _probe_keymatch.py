# -*- coding: utf-8 -*-
"""静态键名比对：GGUF 里有哪些张量 vs LoRA 想改哪些张量。
不加载权重、不跑图 —— 只读文件头。

判据（来自 ComfyUI comfy/lora.py）：
  - 通用分支把模型 sd 里每个 `diffusion_model.X.weight` 注册成 lora 键
    `diffusion_model.X`（带前缀）与 `lora_unet_X`（点换下划线）。
  - QwenImage 分支额外处理 `img_mlp.gate_up`：模型侧是**融合**的
    gate_up，LoRA 侧可能写 gate_layer / proj（两半）。
"""
import json
import struct
import collections

GGUF = r"D:\AI\ComfyUI-backup\models\diffusion_models\qwen-image-2.1-UC-Q4_K_M.gguf"
LORAS = [
    r"D:\AI\ComfyUI-backup\models\loras\Qwen-Image-2.1 Manga to Photo V1.safetensors",
    r"D:\AI\ComfyUI-backup\models\loras\qwen-image-2.1-restore.safetensors",
]

# ---------- 1. 读 GGUF 的张量名 ----------
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
    ver = rd(f, "<I")
    n_tensor = rd(f, "<Q")
    n_kv = rd(f, "<Q")
    for _ in range(n_kv):
        rd_str(f)
        rd_val(f, rd(f, "<I"))
    names = []
    for _ in range(n_tensor):
        nm = rd_str(f)
        nd = rd(f, "<I")
        dims = [rd(f, "<Q") for _ in range(nd)]
        rd(f, "<I")   # type
        rd(f, "<Q")   # offset
        names.append((nm, dims))

print("GGUF 张量 %d 个（version=%d）" % (len(names), ver))
pref = collections.Counter(".".join(n.split(".")[:3]) for n, _ in names)
print("--- 前三级前缀 TOP12 ---")
for p, c in pref.most_common(12):
    print("   %-52s %d" % (p, c))

# 归一：去掉结尾 .weight，得到「基准键」
unet_base = {}
for n, d in names:
    unet_base[n[:-len(".weight")] if n.endswith(".weight") else n] = d

# 看看 img_mlp 到底是融合还是分开
mlp = sorted({n for n, _ in names if "img_mlp" in n})[:8]
print("--- img_mlp 相关张量（样例）---")
for m in mlp:
    print("   ", m, unet_base.get(m[:-len('.weight')] if m.endswith('.weight') else m))

print()
# ---------- 2. 逐个 LoRA 比对 ----------
for lp in LORAS:
    with open(lp, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        head = json.loads(f.read(n).decode("utf-8"))
    keys = [k for k in head if k != "__metadata__"]
    # 收集 LoRA 的「基准键」：去掉 .lora_A/.lora_B .weight
    bases = set()
    for k in keys:
        b = k
        for suf in (".lora_A.weight", ".lora_B.weight", ".lora_A", ".lora_B"):
            if b.endswith(suf):
                b = b[:-len(suf)]
                break
        if b.startswith("diffusion_model."):
            b = b[len("diffusion_model."):]
        bases.add(b)

    hit, miss = [], []
    for b in sorted(bases):
        if b in unet_base:
            hit.append(b)
        elif b.endswith(".img_mlp.gate_up"):
            # 融合/拆半：模型侧若有 gate_layer+proj 也算命中
            alt = b.replace(".gate_up", ".gate_layer")
            alt2 = b.replace(".gate_up", ".proj")
            (hit if (alt in unet_base or alt2 in unet_base) else miss).append(b)
        else:
            miss.append(b)

    print("=" * 78)
    print(lp.rsplit("\\", 1)[-1])
    print("  LoRA 基准键 %d 个 → 命中 %d / 未命中 %d"
          % (len(bases), len(hit), len(miss)))
    if miss:
        print("  未命中样例（最多 12）：")
        for m in miss[:12]:
            print("     ", m)

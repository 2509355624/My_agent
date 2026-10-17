# -*- coding: utf-8 -*-
"""把「融合版」Qwen-Image-2.1 LoRA 重打包成「未融合版」，好让本机 Q4 GGUF 能 100% 吃下。

背景（已用 _probe_repack_need.py 实测确认）：
  · 本机 GGUF 的 img_mlp 是**未融合**：gate_layer[12288,4096] + proj[12288,4096] + out[4096,12288]
  · ai-toolkit 出的 LoRA 是**融合**：gate_up.lora_A[32,4096] + gate_up.lora_B[24576,32] + out.*
  · comfy/lora.py 只会做「未融合 LoRA → 融合模型」这一个方向；
    反方向（融合 LoRA → 未融合模型）映射不上 → 384 张量里丢 64 个（32 block × A/B），丢 16.7%。

做法：把 gate_up 的 A/B 拆成 gate_layer + proj 两套（A 直接复制，B 按行对半切）。
  gate_layer.lora_B = gate_up.lora_B[0:12288]
  proj.lora_B       = gate_up.lora_B[12288:24576]

用法：
  python _repack_restore_lora.py <src.safetensors> <dst.safetensors>
"""
import sys
import json
import struct

import torch
from safetensors import safe_open
from safetensors.torch import save_file

SRC = sys.argv[1] if len(sys.argv) > 1 else r"D:\AI\temp\qwen-image-2.1-restore.safetensors"
DST = sys.argv[2] if len(sys.argv) > 2 else \
    r"D:\AI\ComfyUI-backup\models\loras\qwen-image-2.1-restore-comfy.safetensors"

# ---- 读元数据（safetensors 头）----
with open(SRC, "rb") as f:
    n = struct.unpack("<Q", f.read(8))[0]
    head = json.loads(f.read(n).decode("utf-8"))
src_meta = head.get("__metadata__", {}) or {}
keys = [k for k in head if k != "__metadata__"]

out = {}
n_gate_up = 0
n_other = 0
with safe_open(SRC, framework="pt") as fo:
    for k in keys:
        if ".img_mlp.gate_up.lora_A.weight" in k:
            a = fo.get_tensor(k)                       # [rank, in]
            base = k.replace(".img_mlp.gate_up.lora_A.weight", ".img_mlp.")
            out[base + "gate_layer.lora_A.weight"] = a.clone()
            out[base + "proj.lora_A.weight"] = a.clone()
            n_gate_up += 1
        elif ".img_mlp.gate_up.lora_B.weight" in k:
            b = fo.get_tensor(k)                       # [2*inner, rank]
            half = b.shape[0] // 2
            base = k.replace(".img_mlp.gate_up.lora_B.weight", ".img_mlp.")
            out[base + "gate_layer.lora_B.weight"] = b[:half].clone().contiguous()
            out[base + "proj.lora_B.weight"] = b[half:].clone().contiguous()
            n_gate_up += 1
        else:
            out[k] = fo.get_tensor(k).clone()
            n_other += 1

print("源张量 %d 个（gate_up 侧 %d / 其余 %d）" % (len(keys), n_gate_up, n_other))
print("重打包后 %d 个" % len(out))
assert not any("gate_up" in k for k in out), "还有 gate_up 残留"
gl = [k for k in out if "gate_layer" in k]
pj = [k for k in out if ".img_mlp.proj." in k]
print("  gate_layer 键 %d / proj 键 %d" % (len(gl), len(pj)))

# 形状自检：block 0
print("  样例：")
for k in sorted(out):
    if "img_mlp" in k and ".0." in k:
        print("    %-72s %s" % (k, tuple(out[k].shape)))

# ---- 元数据（全转 str，保留来源信息）----
meta = {str(k): str(v)[:500] for k, v in src_meta.items()}
meta["repacked_for"] = "comfy unfused qwen_image21 img_mlp (gate_up -> gate_layer + proj)"
meta["repacked_from"] = SRC.rsplit("\\", 1)[-1]

save_file(out, DST, metadata=meta)
print("已写出：", DST)

# -*- coding: utf-8 -*-
"""只读：把两个 LoRA 里 img_mlp 相关张量的形状打出来，确认 gate_up 的拆半方式。"""
import json
import struct

LORAS = [
    r"D:\AI\ComfyUI-backup\models\loras\Qwen-Image-2.1 Manga to Photo V1.safetensors",
    r"D:\AI\ComfyUI-backup\models\loras\qwen-image-2.1-restore.safetensors",
]
for lp in LORAS:
    with open(lp, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        head = json.loads(f.read(n).decode("utf-8"))
    print("=" * 78)
    print(lp.rsplit("\\", 1)[-1])
    print("--- block 0 的全部键与形状 ---")
    for k in sorted(head):
        if k == "__metadata__":
            continue
        if ".0." in k:
            print("   %-72s %s" % (k, head[k]["shape"]))
    print("--- img_mlp.gate_up 的 A/B 形状（block 0）---")
    for k in sorted(head):
        if "img_mlp.gate_up" in k and ".0." in k:
            print("   %-72s %s" % (k, head[k]["shape"]))

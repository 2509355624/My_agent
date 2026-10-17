# -*- coding: utf-8 -*-
"""读两个 qwen LoRA 的 safetensors 头，判断它们是为哪个底模训的。
safetensors 头 = 前 8 字节小端 uint64 头长 + 该长度的 JSON。
只读头，不加载权重。
"""
import json
import struct
import sys

FILES = [
    r"D:\AI\ComfyUI-backup\models\loras\Qwen-Image-2.1 Manga to Photo V1.safetensors",
    r"D:\AI\ComfyUI-backup\models\loras\qwen-image-2.1-restore.safetensors",
]

for path in FILES:
    print("=" * 78)
    print(path)
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        head = json.loads(f.read(n).decode("utf-8"))

    meta = head.get("__metadata__", {})
    print("--- __metadata__ ---")
    for k in sorted(meta):
        v = meta[k]
        if len(str(v)) > 300:
            v = str(v)[:300] + " …"
        print("  %-32s = %s" % (k, v))

    keys = [k for k in head if k != "__metadata__"]
    print("--- 张量数 %d ---" % len(keys))
    # 前 5 个键名
    for k in keys[:5]:
        print("   ", k, head[k].get("shape"), head[k].get("dtype"))
    # 键名特征：Qwen-Image 的 transformer 块前缀
    import collections
    pref = collections.Counter()
    for k in keys:
        pref[".".join(k.split(".")[:2])] += 1
    print("--- 前两级前缀 TOP15 ---")
    for p, c in pref.most_common(15):
        print("   %-50s %d" % (p, c))
    # 是否有 lora_A/lora_B
    a = sum(1 for k in keys if "lora_A" in k or "lora_down" in k)
    b = sum(1 for k in keys if "lora_B" in k or "lora_up" in k)
    print("--- lora_A/down=%d  lora_B/up=%d ---" % (a, b))
    # 抓几个含 img / txt / visual 的键，判断是不是 edit 模型（双塔）
    interesting = [k for k in keys if any(
        s in k for s in ("visual", "img_", "image_", "txt_", "text_"))]
    print("--- 含 visual/img/text 的键 %d 个，样例 ---" % len(interesting))
    for k in interesting[:8]:
        print("   ", k)
    print()

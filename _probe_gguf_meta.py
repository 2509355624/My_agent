# -*- coding: utf-8 -*-
"""1) 读 qwen gguf 的 GGUF 头（架构名），2) 读一个能用的 krea2 LoRA 的元数据做对照。"""
import glob
import json
import os
import struct

GGUF_DIRS = [
    r"D:\AI\ComfyUI-backup\models\unet",
    r"D:\AI\ComfyUI-backup\models\diffusion_models",
    r"D:\AI\ComfyUI-backup\models\models",
]

print("=== 找 qwen 的 gguf ===")
for d in GGUF_DIRS:
    for p in glob.glob(os.path.join(d, "*.gguf")):
        print("  ", p, "%.2f GB" % (os.path.getsize(p) / 1e9))

# --- GGUF 头解析 ---
T = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?",
     10: "Q", 11: "q", 12: "d"}


def rd(f, fmt):
    n = struct.calcsize(fmt)
    return struct.unpack(fmt, f.read(n))[0]


def rd_str(f):
    n = rd(f, "<Q")
    return f.read(n).decode("utf-8", "replace")


def rd_val(f, t):
    if t == 8:
        return rd_str(f)
    if t == 9:  # ARRAY
        et = rd(f, "<I")
        n = rd(f, "<Q")
        return [rd_val(f, et) for _ in range(n)]
    return rd(f, "<" + T[t])


for d in GGUF_DIRS:
    for p in glob.glob(os.path.join(d, "*qwen*.gguf")):
        print()
        print("=" * 78)
        print(p)
        with open(p, "rb") as f:
            magic = f.read(4)
            ver = rd(f, "<I")
            n_tensor = rd(f, "<Q")
            n_kv = rd(f, "<Q")
            print("  magic=%r version=%d tensors=%d kv=%d"
                  % (magic, ver, n_tensor, n_kv))
            for _ in range(n_kv):
                k = rd_str(f)
                t = rd(f, "<I")
                v = rd_val(f, t)
                if len(str(v)) > 200:
                    v = str(v)[:200] + " …"
                print("  %-42s = %s" % (k, v))

# --- 对照：krea2 能用的 LoRA ---
print()
print("=" * 78)
print("对照：krea2_奶油.safetensors（krea2 渠道每天在用，格式必然被本机接受）")
p = r"D:\AI\ComfyUI-backup\models\loras\krea2_奶油.safetensors"
with open(p, "rb") as f:
    n = struct.unpack("<Q", f.read(8))[0]
    head = json.loads(f.read(n).decode("utf-8"))
meta = head.get("__metadata__", {})
for k in sorted(meta):
    print("  %-32s = %s" % (k, str(meta[k])[:200]))
keys = [k for k in head if k != "__metadata__"]
print("  张量数 %d；样例：" % len(keys))
for k in keys[:3]:
    print("    ", k, head[k].get("shape"))

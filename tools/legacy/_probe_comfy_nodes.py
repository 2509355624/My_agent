# -*- coding: utf-8 -*-
"""问一下本机 ComfyUI 有哪些 LoRA 加载节点（只读 API，不生成）。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8188"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


try:
    st = get("/system_stats")
    sysd = st.get("system", {})
    print("ComfyUI 在线。RAM total = %.1f GB, free = %.1f GB"
          % (sysd.get("ram_total", 0) / 1e9, sysd.get("ram_free", 0) / 1e9))
    for d in st.get("devices", []):
        print("  GPU %s  vram_total=%.1f GB  vram_free=%.1f GB"
              % (d.get("name"), d.get("vram_total", 0) / 1e9,
                 d.get("vram_free", 0) / 1e9))
except Exception as e:
    print("ComfyUI 不在线/取不到 system_stats：%r" % (e,))

print()
try:
    oi = get("/object_info")
except Exception as e:
    print("取不到 object_info：%r" % (e,))
    raise SystemExit(0)

print("=== 和 LoRA 相关的节点 ===")
for name in sorted(oi):
    if "lora" in name.lower():
        node = oi[name]
        req = node.get("input", {}).get("required", {})
        opt = node.get("input", {}).get("optional", {})
        print("  %-28s required=%s optional=%s"
              % (name, list(req.keys()), list(opt.keys())))

print()
print("=== qwen / gguf 相关节点 ===")
for name in sorted(oi):
    low = name.lower()
    if "qwen" in low or "gguf" in low:
        print("  ", name)

print()
print("=== LoraLoaderModelOnly 的 lora_name 候选里有没有这两个文件 ===")
node = oi.get("LoraLoaderModelOnly") or oi.get("LoraLoader")
if node:
    cand = (node.get("input", {}).get("required", {})
            .get("lora_name", [None])[0])
    if isinstance(cand, list):
        hits = [c for c in cand if "qwen" in c.lower()]
        print("  共 %d 个 lora；含 qwen 的：%s" % (len(cand), hits))
    else:
        print("  lora_name 不是列表：", cand)
else:
    print("  没有 LoraLoaderModelOnly / LoraLoader 节点！")

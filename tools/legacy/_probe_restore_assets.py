import json, struct
from collections import Counter

print("=== WORKFLOW: D:/AI/temp/qwenImage21Tiled_v10.json ===")
d = json.load(open("D:/AI/temp/qwenImage21Tiled_v10.json", encoding="utf-8"))
print("top keys:", list(d.keys())[:25])
if isinstance(d, dict) and "nodes" in d:
    nodes = d["nodes"]
    print("FORMAT: UI | nodes:", len(nodes))
    types = Counter(n.get("type") for n in nodes)
    for t, c in types.most_common():
        print(f"  {c:3d}  {t}")
    subs = d.get("definitions", {}).get("subgraphs", [])
    print("subgraphs:", len(subs))
    print("-- loader / sampler widgets --")
    for n in nodes:
        t = n.get("type")
        wv = n.get("widgets_values")
        if t in ("UNETLoader", "UnetLoaderGGUF", "CLIPLoader", "VAELoader",
                 "KSampler", "KSamplerAdvanced", "LoraLoaderModelOnly",
                 "CheckpointLoaderSimple", "EmptyLatentImage", "ImageScale",
                 "ImageUpscaleWithModel", "UpscaleModelLoader", "LoadImage",
                 "LatentUpscale", "LatentUpscaleBy", "ImageScaleBy"):
            print(f"  [{t}] id={n.get('id')} widgets={wv}")
else:
    print("FORMAT: API | nodes:", len(d))
    types = Counter(v.get("class_type") for v in d.values() if isinstance(v, dict))
    for t, c in types.most_common():
        print(f"  {c:3d}  {t}")

print()
print("=== LORA: D:/AI/temp/qwen-image-2.1-restore.safetensors ===")
with open("D:/AI/temp/qwen-image-2.1-restore.safetensors", "rb") as f:
    n = struct.unpack("<Q", f.read(8))[0]
    hdr = json.loads(f.read(n))
meta = hdr.get("__metadata__", {})
for k, v in meta.items():
    print(f"  {k} = {str(v)[:200]}")
keys = [k for k in hdr if k != "__metadata__"]
print("  tensor count:", len(keys))
gu = [k for k in keys if "gate_up" in k]
gl = [k for k in keys if "gate_layer" in k or ".proj." in k]
print("  gate_up keys:", len(gu), "| gate_layer/proj keys:", len(gl))
if gu:
    print("  sample gate_up:", gu[:2])
    print("  shape:", hdr[gu[0]].get("shape"))
print("  sample keys:", keys[:5])

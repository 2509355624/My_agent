# -*- coding: utf-8 -*-
"""体检两个 ComfyUI 工作流 JSON：
1) 判断是 UI 格式还是 API 格式
2) 列出用到的节点类型，并和本机 ComfyUI 的 object_info 比对（缺哪些节点）
3) 抓出所有像文件名的 widget 值，检查模型文件在不在
"""
import glob
import json
import os
import urllib.request
import collections

DIR = r"D:\AI\temp\QwenImage21Character_qwenImage21V30"
BASE = "http://127.0.0.1:8188"

# 模型搜索根（本机真实布局）
ROOTS = [
    r"D:\AI\ComfyUI-backup\models",
    r"D:\AI\ComfyUI-master\ComfyUI\models",
    r"D:\AI\ComfyUI_v037\models",
]
EXT = (".safetensors", ".gguf", ".pth", ".pt", ".ckpt", ".bin", ".sft")


def find_model(name):
    hits = []
    for r in ROOTS:
        if not os.path.isdir(r):
            continue
        for dp, dn, fn in os.walk(r):
            if os.path.basename(name) in fn:
                hits.append(os.path.join(dp, basename_of := os.path.basename(name)))
    return hits


try:
    with urllib.request.urlopen(BASE + "/object_info", timeout=25) as r:
        OI = json.loads(r.read().decode("utf-8"))
    print("ComfyUI 在线，节点类型 %d 个\n" % len(OI))
except Exception as e:
    OI = None
    print("ComfyUI 不在线（%r）→ 跳过节点检查\n" % (e,))

for path in sorted(glob.glob(os.path.join(DIR, "*.json"))):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print("=" * 78)
    print(os.path.basename(path))

    # ---- 判格式 ----
    if isinstance(data, dict) and "nodes" in data:
        fmt = "UI"
        nodes = data["nodes"]
        types = [n.get("type") for n in nodes]
        widgets = []
        for n in nodes:
            for w in (n.get("widgets_values") or []):
                if isinstance(w, str):
                    widgets.append(w)
                elif isinstance(w, list):
                    widgets.extend([x for x in w if isinstance(x, str)])
    elif isinstance(data, dict):
        fmt = "API"
        nodes = list(data.values())
        types = [n.get("class_type") for n in nodes]
        widgets = []
        for n in nodes:
            for v in (n.get("inputs") or {}).values():
                if isinstance(v, str):
                    widgets.append(v)
    else:
        print("  未知结构：", type(data))
        continue

    print("  格式=%s  节点数=%d" % (fmt, len(nodes)))
    hist = collections.Counter(types)
    print("  --- 节点类型 ---")
    for t, c in hist.most_common():
        mark = ""
        if OI is not None:
            mark = "  ✅" if t in OI else "  ❌ 本机没有"
        print("     %-44s x%-3d%s" % (t, c, mark))

    # ---- 模型文件 ----
    cand = sorted({w for w in widgets if w.lower().endswith(EXT)})
    print("  --- 引用到的模型文件 %d 个 ---" % len(cand))
    for c in cand:
        hits = find_model(c)
        print("     %-56s %s" % (c, "✅ 在 " + hits[0] if hits else "❌ 找不到"))
    print()

# -*- coding: utf-8 -*-
"""深挖：节点名到底有没有、UUID 节点是不是子图、工作流整体干什么。"""
import glob
import json
import os
import urllib.request

DIR = r"D:\AI\temp\QwenImage21Character_qwenImage21V30"

with urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=25) as r:
    OI = json.loads(r.read().decode("utf-8"))

print("=== 名字里含 markdown / note 的节点 ===")
for k in sorted(OI):
    if "markdown" in k.lower() or k.lower() == "note":
        print("   ", k)

print("\n=== 名字里含 resize 的节点（前 25）===")
n = 0
for k in sorted(OI):
    if "resize" in k.lower():
        print("   ", k)
        n += 1
        if n >= 25:
            break

print("\n=== 名字里含 kj 的节点（前 20）===")
n = 0
for k in sorted(OI):
    if "kj" in k.lower():
        print("   ", k)
        n += 1
        if n >= 20:
            break

for path in sorted(glob.glob(os.path.join(DIR, "*.json"))):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    print()
    print("=" * 78)
    print(os.path.basename(path))
    print("  顶层键：", sorted(d.keys()))
    defs = d.get("definitions") or {}
    print("  definitions 键：", sorted(defs.keys()) if isinstance(defs, dict) else type(defs))
    subs = (defs.get("subgraphs") if isinstance(defs, dict) else None) or []
    print("  子图 %d 个：" % len(subs))
    for s in subs:
        print("     id=%s  name=%s  内部节点=%d"
              % (s.get("id"), s.get("name"), len(s.get("nodes") or [])))
        inner = [x.get("type") for x in (s.get("nodes") or [])]
        print("        内部节点类型：", inner)

    print("  --- 16 个顶层节点（id / type / title）---")
    for nd in d.get("nodes", []):
        print("     %-5s %-44s %s"
              % (nd.get("id"), nd.get("type"), (nd.get("title") or "")[:40]))
    # 提示词文本
    print("  --- 文本类 widget（前 12 条，截断 160 字）---")
    cnt = 0
    for nd in d.get("nodes", []):
        for w in (nd.get("widgets_values") or []):
            if isinstance(w, str) and len(w) > 25 and not w.lower().endswith(
                    (".safetensors", ".gguf", ".pth", ".pt", ".ckpt", ".bin")):
                print("     [%s] %s" % (nd.get("type"), w[:160].replace("\n", " ⏎ ")))
                cnt += 1
                if cnt >= 12:
                    break
        if cnt >= 12:
            break

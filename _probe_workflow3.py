# -*- coding: utf-8 -*-
"""把顶层 + 子图内部的**所有**节点类型都拿去和本机 object_info 比对，
并把子图里引用的模型/参数打出来。"""
import glob
import json
import os
import urllib.request

DIR = r"D:\AI\temp\QwenImage21Character_qwenImage21V30"

with urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=25) as r:
    OI = json.loads(r.read().decode("utf-8"))


def walk(nodes, out):
    for n in nodes or []:
        out.append(n)


for path in sorted(glob.glob(os.path.join(DIR, "*.json"))):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    print("=" * 78)
    print(os.path.basename(path))

    allnodes = []
    walk(d.get("nodes"), allnodes)
    subs = ((d.get("definitions") or {}).get("subgraphs")) or []
    for s in subs:
        walk(s.get("nodes"), allnodes)

    types = sorted({n.get("type") for n in allnodes})
    print("  --- 全部节点类型 %d 种 ---" % len(types))
    missing = []
    for t in types:
        ok = t in OI
        if not ok:
            missing.append(t)
        print("     %-44s %s" % (t, "OK" if ok else "❌ 本机没有"))
    print("  >>> 缺 %d 种：%s" % (len(missing), missing))

    # 子图里的关键节点参数
    for s in subs:
        print("  --- 子图 [%s] 的节点参数 ---" % s.get("name"))
        for n in s.get("nodes") or []:
            w = n.get("widgets_values")
            if n.get("type") in ("UNETLoader", "CLIPLoader", "VAELoader", "KSampler",
                                 "TextGenerate", "CLIPLoaderGGUF"):
                print("     %-22s %s" % (n.get("type"), w))
    print()

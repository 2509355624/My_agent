# -*- coding: utf-8 -*-
"""打印要改的那几个节点的完整原文。"""
import json

P = r"D:\AI\temp\QwenImage21Character_qwenImage21V30\Character_Sheet_Production.json"
d = json.load(open(P, encoding="utf-8"))

WANT = {"UNETLoader", "CLIPLoader", "VAELoader", "VAEDeGrid", "TextEncodeQwenImage21",
        "EmptyLatentImage", "KSampler", "ComfySwitchNode", "PrimitiveStringMultiline"}
for s in d["definitions"]["subgraphs"]:
    print("#" * 78)
    print("子图：", s["name"])
    for n in s["nodes"]:
        if n["type"] in WANT:
            print("--- [%s] id=%s ---" % (n["type"], n["id"]))
            print(json.dumps(n, ensure_ascii=False, indent=1)[:1800])
            print()

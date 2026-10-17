# -*- coding: utf-8 -*-
"""看子图的原始 JSON 结构（outputs 怎么接线、promoted widget 怎么存）。"""
import json

P = r"D:\AI\temp\QwenImage21Character_qwenImage21V30\Character_Sheet_Production.json"
with open(P, "r", encoding="utf-8") as f:
    d = json.load(f)

print("顶层键：", sorted(d.keys()))
print("version=%s revision=%s" % (d.get("version"), d.get("revision")))
print("config:", json.dumps(d.get("config"), ensure_ascii=False)[:400])
print("extra:", json.dumps(d.get("extra"), ensure_ascii=False)[:300])
print("seed_widgets:", json.dumps(d.get("seed_widgets"), ensure_ascii=False)[:300])
print()

for s in d["definitions"]["subgraphs"]:
    print("=" * 78)
    print("子图 %s  name=%s" % (s["id"], s.get("name")))
    print("  键：", sorted(s.keys()))
    print("  inputs 原文：", json.dumps(s.get("inputs"), ensure_ascii=False)[:900])
    print("  outputs 原文：", json.dumps(s.get("outputs"), ensure_ascii=False)[:600])
    print("  内部节点 id：", [n["id"] for n in s["nodes"]])
    print("  内部 links 样例：", json.dumps((s.get("links") or [])[:3], ensure_ascii=False))
    # 找一个内部节点看完整字段
    for n in s["nodes"]:
        if n["type"] in ("TextGenerate", "VAEDeGrid", "KSampler"):
            print("  --- 内部节点样例 [%s] ---" % n["type"])
            print("   ", json.dumps(n, ensure_ascii=False)[:900])
            break
    # 顶层那个子图节点的原文
for n in d["nodes"]:
    if n["type"] in (s["id"] for s in d["definitions"]["subgraphs"]):
        print("=" * 78)
        print("顶层子图节点 %s 原文：" % n["type"])
        print(json.dumps(n, ensure_ascii=False)[:1500])

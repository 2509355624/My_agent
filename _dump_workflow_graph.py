# -*- coding: utf-8 -*-
"""把 UI 格式工作流的连线图打印成人能读的样子，并输出全部 MarkdownNote 正文。"""
import glob
import json
import os

DIR = r"D:\AI\temp\QwenImage21Character_qwenImage21V30"


def norm_link(L):
    """新前端里 links 是 dict，老格式是 list。统一成 6 元组。"""
    if isinstance(L, dict):
        return (L.get("id"), L.get("origin_id"), L.get("origin_slot"),
                L.get("target_id"), L.get("target_slot"), L.get("type"))
    return tuple(L)


def dump(path):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    nodes = {n["id"]: n for n in d.get("nodes", [])}
    # 子图也并进来（子图内部节点 id 全局唯一吗？不一定，所以分开标）
    subs = ((d.get("definitions") or {}).get("subgraphs")) or []
    subnodes = {}
    for s in subs:
        for n in s.get("nodes") or []:
            subnodes[(s["id"], n["id"])] = n

    def label(nid):
        n = nodes.get(nid)
        if n:
            return "#%s %s%s" % (nid, n.get("type"),
                                ("[" + n["title"] + "]") if n.get("title") else "")
        return "#%s(?)" % nid

    print("=" * 78)
    print(os.path.basename(path))
    print("--- 顶层节点连线 ---")
    links = [norm_link(L) for L in d.get("links", [])]
    for nid in sorted(nodes, key=lambda x: (str(x))):
        n = nodes[nid]
        ins = []
        for i in n.get("inputs") or []:
            src = ""
            if i.get("link") is not None:
                for L in links:
                    if L[0] == i["link"]:
                        src = label(L[1])
                        break
            ins.append("%s ← %s" % (i.get("name"), src or "—"))
        outs = []
        for o in n.get("outputs") or []:
            tg = []
            for lid in (o.get("links") or []):
                for L in links:
                    if L[0] == lid:
                        tg.append(label(L[3]))
            outs.append("%s → %s" % (o.get("name"), ",".join(tg) or "—"))
        print("  %-46s %s" % (label(nid), n.get("mode", 0)))
        for s in ins:
            print("        IN : %s" % s)
        for s in outs:
            print("        OUT: %s" % s)
        if n.get("widgets_values"):
            print("        W  : %s" % (n["widgets_values"],))

    for s in subs:
        print("--- 子图 [%s] 内部连线 ---" % s.get("name"))
        sn = {n["id"]: n for n in s.get("nodes") or []}
        slinks = [norm_link(L) for L in (s.get("links") or [])]
        for nid in sorted(sn, key=lambda x: str(x)):
            n = sn[nid]
            ins = []
            for i in n.get("inputs") or []:
                src = ""
                if i.get("link") is not None:
                    for L in slinks:
                        if L[0] == i["link"]:
                            src = "#%s %s" % (L[1], sn.get(L[1], {}).get("type"))
                            break
                ins.append("%s ← %s" % (i.get("name"), src or "—"))
            outs = []
            for o in n.get("outputs") or []:
                tg = []
                for lid in (o.get("links") or []):
                    for L in slinks:
                        if L[0] == lid:
                            tg.append("#%s %s" % (L[3], sn.get(L[3], {}).get("type")))
                outs.append("%s → %s" % (o.get("name"), ",".join(tg) or "—"))
            print("  #%-5s %-32s mode=%s" % (nid, n.get("type"), n.get("mode", 0)))
            for x in ins:
                print("        IN : %s" % x)
            for x in outs:
                print("        OUT: %s" % x)
            if n.get("widgets_values"):
                print("        W  : %s" % (n["widgets_values"],))
        print("  子图输入/输出定义：")
        print("    inputs :", [(i.get("name"), i.get("type")) for i in (s.get("inputs") or [])])
        print("    outputs:", [(o.get("name"), o.get("type")) for o in (s.get("outputs") or [])])


    print("--- MarkdownNote 正文 ---")
    for nid in nodes:
        n = nodes[nid]
        if n.get("type") == "MarkdownNote":
            print("  [%s]" % (n.get("title") or nid))
            print("    " + (n["widgets_values"][0] or "").replace("\n", "\n    "))
    print()


for p in sorted(glob.glob(os.path.join(DIR, "*.json"))):
    dump(p)

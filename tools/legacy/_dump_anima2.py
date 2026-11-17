# -*- coding: utf-8 -*-
"""把这两个 UI 格式 anima 工作流的连线图打印出来（含 LoRA 链、两个 UNETLoader 的去向）。"""
import json
import os

DIR = r"D:\AI\ComfyUI_v037\user\default\workflows"
FILES = ["anima柔和.json", "anima写实.json"]


def norm_link(L):
    if isinstance(L, dict):
        return (L.get("id"), L.get("origin_id"), L.get("origin_slot"),
                L.get("target_id"), L.get("target_slot"), L.get("type"))
    return tuple(L)


for fn in FILES:
    path = os.path.join(DIR, fn)
    d = json.load(open(path, encoding="utf-8"))
    nodes = {n["id"]: n for n in d.get("nodes", [])}
    links = {}
    for L in d.get("links", []):
        lid, oid, oslot, tid, tslot, ltype = norm_link(L)
        links[(tid, tslot)] = (oid, oslot, ltype)

    print("=" * 78)
    print(fn)
    print("=" * 78)
    for nid in sorted(nodes):
        n = nodes[nid]
        t = n.get("type")
        ttl = n.get("title") or ""
        wv = n.get("widgets_values")
        ins = n.get("inputs") or []
        outs = n.get("outputs") or []

        def src(i):
            s = links.get((nid, i))
            if not s:
                return None
            o = nodes.get(s[0])
            return "#%s %s" % (s[0], (o.get("title") or o.get("type")) if o else "?")

        in_desc = []
        for i, port in enumerate(ins):
            nm = port.get("name") if isinstance(port, dict) else str(port)
            s = src(i)
            if s:
                in_desc.append("%s<-%s" % (nm, s))
        out_desc = []
        for port in outs:
            if not isinstance(port, dict):
                continue
            for lk in (port.get("links") or []):
                for L in d.get("links", []):
                    lid, oid, oslot, tid, tslot, ltype = norm_link(L)
                    if lid == lk:
                        o = nodes.get(tid)
                        out_desc.append("%s->#%s %s" % (
                            port.get("name"), tid,
                            (o.get("title") or o.get("type")) if o else "?"))
        print("\n#%-3s %-26s %s" % (nid, t, ttl))
        if wv is not None:
            print("      widgets: %s" % (wv,))
        if in_desc:
            print("      in : %s" % ", ".join(in_desc))
        if out_desc:
            print("      out: %s" % ", ".join(out_desc))
    print()

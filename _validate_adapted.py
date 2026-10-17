# -*- coding: utf-8 -*-
"""静态体检改造后的工作流：
 - 所有节点类型本机是否都有
 - 每条 link 的 origin/target 节点是否都存在（-10/-20 是子图代理，跳过）
 - 每个 input.link 是否都能在 links 里找到
 - 模型 widget 是否都在对应 combo 候选里
"""
import glob
import json
import os
import urllib.request

DIR = r"D:\AI\temp\QwenImage21Character_qwenImage21V30"
with urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=25) as r:
    OI = json.loads(r.read().decode("utf-8"))


def norm(L):
    """顶层 links 是 list，子图 links 是 dict。统一成 dict。"""
    if isinstance(L, dict):
        return L
    return {"id": L[0], "origin_id": L[1], "origin_slot": L[2],
            "target_id": L[3], "target_slot": L[4], "type": L[5]}


def check(path):
    d = json.load(open(path, encoding="utf-8"))
    print("=" * 78)
    print(os.path.basename(path))
    subs = {s["id"]: s for s in (d.get("definitions") or {}).get("subgraphs", [])}
    problems = []

    # --- 顶层 ---
    top = {n["id"]: n for n in d["nodes"]}
    links = {norm(L)["id"]: norm(L) for L in d.get("links", [])}
    for nid, n in top.items():
        if n["type"] in subs:
            continue                      # 子图节点，类型是 UUID，正常
        if n["type"] not in OI:
            problems.append("顶层 #%s 节点类型本机没有：%s" % (nid, n["type"]))
    for lid, L in links.items():
        for end in (L["origin_id"], L["target_id"]):
            if end not in top:
                problems.append("link %s 指向不存在的节点 %s" % (lid, end))
    for nid, n in top.items():
        for i in n.get("inputs") or []:
            if i.get("link") is not None and i["link"] not in links:
                problems.append("顶层 #%s 输入 %s 的 link %s 不存在"
                                % (nid, i.get("name"), i["link"]))
        for o in n.get("outputs") or []:
            for lid in (o.get("links") or []):
                if lid not in links:
                    problems.append("顶层 #%s 输出 %s 的 link %s 不存在"
                                    % (nid, o.get("name"), lid))

    # --- 子图 ---
    for sid, s in subs.items():
        sn = {n["id"]: n for n in s["nodes"]}
        sl = {norm(L)["id"]: norm(L) for L in (s.get("links") or [])}
        for nid, n in sn.items():
            if n["type"] not in OI:
                problems.append("子图[%s] #%s 节点类型本机没有：%s"
                                % (s.get("name"), nid, n["type"]))
        for lid, L in sl.items():
            for end in (L["origin_id"], L["target_id"]):
                if end in (-10, -20):
                    continue
                if end not in sn:
                    problems.append("子图[%s] link %s 指向不存在的节点 %s"
                                    % (s.get("name"), lid, end))
        for nid, n in sn.items():
            for i in n.get("inputs") or []:
                if i.get("link") is not None and i["link"] not in sl:
                    problems.append("子图[%s] #%s 输入 %s 的 link %s 不存在"
                                    % (s.get("name"), nid, i.get("name"), i["link"]))
        # 子图声明的输入/输出 linkIds 要能解析
        for i in s.get("inputs") or []:
            for lid in (i.get("linkIds") or []):
                if lid not in sl:
                    problems.append("子图[%s] 输入 %s 的 linkId %s 不存在"
                                    % (s.get("name"), i.get("name"), lid))
        for o in s.get("outputs") or []:
            for lid in (o.get("linkIds") or []):
                if lid not in sl:
                    problems.append("子图[%s] 输出 %s 的 linkId %s 不存在"
                                    % (s.get("name"), o.get("name"), lid))

    # --- 模型 combo 校验 ---
    def combo(node, key):
        v = OI.get(node, {}).get("input", {}).get("required", {}).get(key)
        if not v:
            return None
        return v[0] if isinstance(v[0], list) else None

    for scope, nodeset in [("顶层", top)] + [("子图[%s]" % s.get("name"), {n["id"]: n for n in s["nodes"]})
                                             for s in subs.values()]:
        for nid, n in nodeset.items():
            t = n["type"]
            if t not in OI:
                continue
            wvn = n.get("widgets_values_named") or {}
            for key in ("unet_name", "clip_name", "vae_name", "lora_name"):
                if key in wvn:
                    opts = combo(t, key)
                    if opts is None:
                        continue
                    if wvn[key] not in opts:
                        problems.append("%s #%s 的 %s='%s' 不在候选里"
                                        % (scope, nid, key, wvn[key]))

    if problems:
        print("  ❌ %d 个问题：" % len(problems))
        for p in problems:
            print("     -", p)
    else:
        print("  ✅ 没问题：节点类型齐全、连线完整、模型都在候选里")


for p in sorted(glob.glob(os.path.join(DIR, "*.local.json"))):
    check(p)

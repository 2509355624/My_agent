# -*- coding: utf-8 -*-
"""把 Character_Sheet_*.json 改造成本机能跑的样子（不动原文件，产出 *.local.json）。

改造思路：**把缺的东西整个拿掉，而不是留个 bypass 的壳**
（未知节点类型即使 bypass，前端也可能拒绝排队）。

 1. Image Edit 子图内 UNETLoader → UnetLoaderGGUF（本机 Q4 GGUF；int8 版没有）
 2. Image Edit 子图内 CLIPLoader.clip_name → qwen3vl_8b_w4a8.safetensors（bf16 版没有）
 3. Image Edit 子图内 VAELoader.vae_name → qwen_image_2.1_vae_bf16.safetensors
 4. Image Edit 子图内 VAEDeGrid **删掉**，把它的输出直接接到 VAEDecode（本机没装 DeGrid）
 5. 顶层 ImageResizeKJv2 **删掉**，LoadImage 直连下游（本机没装 KJNodes；
    下游 TextEncodeQwenImage21 自己会按 resolution=1536 缩参考图，所以不缩也安全）
 6. 顶层 Prompt Maker 子图节点 **整个换成 PrimitiveStringMultiline**（装它自带的静态提示词），
    连带删掉「实体名」节点；Prompt Maker 子图定义一并从 definitions 里移除
    （本机没有 PE 大模型，而原作者本来就说可以用静态提示词代替）
 7. 顶层 6 个 MarkdownNote 删掉（本机没有这个节点类型；纯文档框，无连线）
"""
import json
import os
import urllib.request

SRC = r"D:\AI\temp\QwenImage21Character_qwenImage21V30"
UNET = "qwen-image-2.1-UC-Q4_K_M.gguf"
CLIP = "qwen3vl_8b_w4a8.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
IE_ORDER = ["prompt", "resolution", "steps", "width", "height", "scheduler",
            "scheduler_1", "seed", "unet_name", "clip_name", "vae_name"]

with urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=25) as r:
    OI = json.loads(r.read().decode("utf-8"))


def combo(node, key):
    v = OI[node]["input"]["required"][key][0]
    return v if isinstance(v, list) else []


for node, key, val in (("UnetLoaderGGUF", "unet_name", UNET),
                       ("CLIPLoader", "clip_name", CLIP),
                       ("VAELoader", "vae_name", VAE)):
    assert val in combo(node, key), "%s.%s 里没有 %s" % (node, key, val)


def norm(L):
    if isinstance(L, dict):
        return dict(L)
    return {"id": L[0], "origin_id": L[1], "origin_slot": L[2],
            "target_id": L[3], "target_slot": L[4], "type": L[5]}


def to_list(L):
    return [L["id"], L["origin_id"], L["origin_slot"],
            L["target_id"], L["target_slot"], L["type"]]


def rebuild(nodes, links):
    """按 links 重算每个节点的 inputs[].link 与 outputs[].links，保证一致。"""
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        for i in n.get("inputs") or []:
            i["link"] = None
        for o in n.get("outputs") or []:
            o["links"] = None
    for L in links:
        o = by_id.get(L["origin_id"])
        if o is not None:
            outs = o.get("outputs") or []
            if L["origin_slot"] < len(outs):
                cur = outs[L["origin_slot"]].get("links") or []
                outs[L["origin_slot"]]["links"] = cur + [L["id"]]
        t = by_id.get(L["target_id"])
        if t is not None:
            ins = t.get("inputs") or []
            if L["target_slot"] < len(ins):
                ins[L["target_slot"]]["link"] = L["id"]


for src_name in ("Character_Sheet_Production.json", "Character_Sheet_Simple.json"):
    path = os.path.join(SRC, src_name)
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    log = []
    subs = d["definitions"]["subgraphs"]

    # ================= 1~4：Image Edit 子图 =================
    ie = [s for s in subs if "Image Edit" in (s.get("name") or "")][0]
    ilinks = [norm(L) for L in (ie.get("links") or [])]
    ie_nodes = ie["nodes"]
    by_id = {n["id"]: n for n in ie_nodes}
    drop = set()

    for n in ie_nodes:
        t = n["type"]
        if t == "UNETLoader":
            n["type"] = "UnetLoaderGGUF"
            n["inputs"] = [i for i in n["inputs"] if i["name"] == "unet_name"]
            n["widgets_values"] = [UNET]
            n["widgets_values_named"] = {"unet_name": UNET}
            p = n.setdefault("properties", {})
            p["Node name for S&R"] = "UnetLoaderGGUF"
            p.pop("models", None)
            log.append("① UNETLoader → UnetLoaderGGUF（%s）" % UNET)
        elif t == "CLIPLoader":
            n["widgets_values"] = [CLIP, "qwen_image", "default"]
            n["widgets_values_named"] = {"clip_name": CLIP, "type": "qwen_image",
                                        "device": "default"}
            log.append("② CLIPLoader → %s" % CLIP)
        elif t == "VAELoader":
            n["widgets_values"] = [VAE]
            n["widgets_values_named"] = {"vae_name": VAE}
            log.append("③ VAELoader → %s" % VAE)
        elif t == "VAEDeGrid":
            src = next((L for L in ilinks if L["target_id"] == n["id"]), None)
            drop.add(n["id"])
            if src:
                for L in ilinks:
                    if L["origin_id"] == n["id"]:
                        L["origin_id"] = src["origin_id"]
                        L["origin_slot"] = src["origin_slot"]
            log.append("④ VAEDeGrid 删掉，VAEDecode 直连子图输出（本机没装 DeGrid）")

    if drop:
        ie["nodes"] = [n for n in ie_nodes if n["id"] not in drop]
        ilinks = [L for L in ilinks
                  if L["origin_id"] not in drop and L["target_id"] not in drop]
    rebuild(ie["nodes"], ilinks)
    ie["links"] = [to_list(L) for L in ilinks]

    # ================= 6：摘掉 Prompt Maker 子图 =================
    pm = [s for s in subs if "Prompt Maker" in (s.get("name") or "")]
    static_text = None
    pm_id = None
    if pm:
        pm = pm[0]
        pm_id = pm["id"]
        by = {n["id"]: n for n in pm["nodes"]}
        sw = next((n for n in pm["nodes"] if n["type"] == "ComfySwitchNode"), None)
        if sw:
            lf = next(i["link"] for i in sw["inputs"] if i["name"] == "on_false")
            Lf = next(L for L in pm["links"] if L["id"] == lf)
            static_text = by[Lf["origin_id"]]["widgets_values"][0]
    if static_text is None:
        raise SystemExit("没找到静态提示词，%s 结构变了" % src_name)

    tnodes = d["nodes"]
    tlinks = [norm(L) for L in d["links"]]
    tby = {n["id"]: n for n in tnodes}
    tdrop = set()

    # 6a. Prompt Maker 子图节点 → 换成一个 PrimitiveStringMultiline
    pm_node = next(n for n in tnodes if n["type"] == pm_id)
    # ⚠️ 要的是「从子图节点**出去**」的那条（→ PreviewAny），不是进来的那条。
    feeder = next(L for L in tlinks if L["origin_id"] == pm_node["id"])
    new_id = max(tby) + 1
    new_node = {
        "id": new_id,
        "type": "PrimitiveStringMultiline",
        "pos": [pm_node["pos"][0], pm_node["pos"][1] + 560],
        "size": [620, 600],
        "flags": {},
        "order": pm_node.get("order", 0),
        "mode": 0,
        "inputs": [],
        "outputs": [{"localized_name": "STRING", "name": "STRING",
                     "type": "STRING", "links": [feeder["id"]]}],
        "properties": {"cnr_id": "comfy-core", "ver": "0.38.0",
                       "Node name for S&R": "PrimitiveStringMultiline"},
        "widgets_values": [static_text],
    }
    feeder["origin_id"] = new_id
    feeder["origin_slot"] = 0
    tnodes.append(new_node)
    tby[new_id] = new_node
    tdrop.add(pm_node["id"])
    log.append("⑤ Prompt Maker 子图节点 → PrimitiveStringMultiline（自带静态提示词，%d 字）"
               % len(static_text))

    # 6b. 删掉「实体名 / 角色描述」那个 PrimitiveString*（它只喂给 PE 子图）
    for n in list(tnodes):
        if n["id"] == pm_node["id"]:
            continue
        if n["type"] in ("PrimitiveString", "PrimitiveStringMultiline") and n["id"] != new_id:
            used = [L for L in tlinks if L["origin_id"] == n["id"]]
            if used and all(L["target_id"] == pm_node["id"] for L in used):
                tdrop.add(n["id"])
                log.append("⑥ 删掉 #%s %s（只喂 PE 子图）" % (n["id"], n["type"]))

    # 7. MarkdownNote
    for n in tnodes:
        if n["type"] == "MarkdownNote":
            tdrop.add(n["id"])
    if any(n["type"] == "MarkdownNote" for n in tnodes):
        log.append("⑦ 删掉 MarkdownNote ×%d（本机没有这个节点类型）"
                   % sum(1 for n in tnodes if n["type"] == "MarkdownNote"))

    # 5. ImageResizeKJv2 删掉
    for n in tnodes:
        if n["type"] == "ImageResizeKJv2":
            src = next((L for L in tlinks if L["target_id"] == n["id"]), None)
            tdrop.add(n["id"])
            if src:
                for L in tlinks:
                    if L["origin_id"] == n["id"]:
                        L["origin_id"] = src["origin_id"]
                        L["origin_slot"] = src["origin_slot"]
            log.append("⑧ ImageResizeKJv2 删掉，LoadImage 直连（本机没装 KJNodes）")

    tlinks = [L for L in tlinks
              if L["origin_id"] not in tdrop and L["target_id"] not in tdrop]
    tnodes = [n for n in tnodes if n["id"] not in tdrop]

    # 8. 顶层 Image Edit 子图节点的 unet/clip/vae
    for n in tnodes:
        wvn = n.get("widgets_values_named")
        if isinstance(wvn, dict) and "unet_name" in wvn:
            wvn["unet_name"] = UNET
            wvn["clip_name"] = CLIP
            wvn["vae_name"] = VAE
            wv = n.get("widgets_values") or []
            n["widgets_values"] = [wvn[k] if k in wvn
                                   else (wv[i] if i < len(wv) else None)
                                   for i, k in enumerate(IE_ORDER)]
            log.append("⑨ 顶层子图 #%s 的 unet/clip/vae 换成 %s / %s / %s"
                       % (n["id"], UNET, CLIP, VAE))

    rebuild(tnodes, tlinks)
    d["nodes"] = tnodes
    d["links"] = [to_list(L) for L in tlinks]
    d["definitions"]["subgraphs"] = [s for s in subs if s["id"] != pm_id]

    out = os.path.join(SRC, src_name.replace(".json", ".local.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print("=" * 78)
    print("%s → %s  (%.1f KB)" % (src_name, os.path.basename(out),
                                  os.path.getsize(out) / 1024))
    for x in log:
        print("   ", x)

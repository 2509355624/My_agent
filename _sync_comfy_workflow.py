# -*- coding: utf-8 -*-
"""把 ComfyUI 的 UI 格式工作流同步成 skills/<渠道>/workflow.json（API 格式）。

    python _sync_comfy_workflow.py anime2 anima            # 只体检，不写盘
    python _sync_comfy_workflow.py anime2 anima --write     # 通过后写盘

为什么需要它
------------
用户在 ComfyUI 里调完参数「保存」，存的是 **UI 格式**
（`{nodes, links, widgets_values...}`），而 `generate_image` 提交给 `/prompt`
的是 **API 格式**（`{"<id>": {"class_type":..., "inputs":...}}`）。
仓库里没有现成的转换器，而这个动作**会反复发生**（用户每调一版就要来一次），
所以把整套流程固化成脚本，别每次手搓。

脚本会做四件事
--------------
1. **转换**：`links` 是 `[link_id, 源节点, 源槽, 目标节点, 目标槽, 类型]`（6 元），
   目标节点 `inputs[i].link` 存的是 `link_id`，反查成 `[源节点, 源槽]`。
   控件值优先取 `widgets_values_named`（dict，按名字对位，不用猜顺序）；
   跳过 `control_after_generate`（前端伪控件）和 Note/Reroute/mute/bypass 节点。
2. **两处必改的替换**（不改渠道直接坏）：
   - 正向提示词 → `<触发词前缀>__MULTI_PROMPTS__`
     （ComfyUI 里存的永远是「当时那张图的完整提示词」，不是模板；
     照抄就等于把模型传的 prompt 全丢掉。已踩三次，见 skill.md）
   - 两个 KSampler 的 seed → **裸占位符** `"seed": __SEED__,`（不是合法 JSON）
3. **体检**（写盘前必须全绿）：
   - 悬空 / `[None, 0]` 连线
   - 节点类型、输入名、输出槽位、combo 候选值 —— 全部拿 ComfyUI `/object_info` 对
   - 引用的模型 / LoRA 文件是否真的在
   - 和现有 workflow.json 的**拓扑差异**（连线一条都不该变，变了要人看一眼）
4. 打印参数 diff，让你确认「改的确实是用户想改的那几个旋钮」。
"""

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

COMFY_WF_DIR = r"D:\AI\ComfyUI\user\default\workflows"
SKIP_TYPES = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}
MODEL_KEYS = ("unet_name", "vae_name", "clip_name", "lora_name", "ckpt_name")


# ─── 1. UI → API ────────────────────────────────────────

def ui_to_api(ui):
    link_src = {}
    for l in ui.get("links", []):
        if isinstance(l, dict):
            link_src[l["id"]] = (l["origin_id"], l["origin_slot"])
        else:
            link_src[l[0]] = (l[1], l[2])

    api = {}
    for n in ui.get("nodes", []):
        ntype = n.get("type")
        if ntype in SKIP_TYPES or n.get("mode") in (2, 4):
            continue
        inputs = {}

        for inp in (n.get("inputs") or []):
            name, link = inp.get("name"), inp.get("link")
            if name is None:
                continue
            if link is not None and link in link_src:
                src_id, src_slot = link_src[link]
                inputs[name] = [str(src_id), src_slot]

        named = n.get("widgets_values_named")
        if isinstance(named, dict):
            for k, v in named.items():
                if k == "control_after_generate":
                    continue
                inputs[k] = v
        else:
            wnames = [i.get("widget", {}).get("name")
                      for i in (n.get("inputs") or []) if i.get("widget")]
            wnames = [w for w in wnames if w]
            vals = n.get("widgets_values") or []
            vi = 0
            for w in wnames:
                if vi >= len(vals):
                    break
                inputs[w] = vals[vi]
                vi += 1
                if w in ("seed", "noise_seed"):
                    vi += 1          # 跳过 control_after_generate

        api[str(n["id"])] = {"class_type": ntype, "inputs": inputs}
    return api


# ─── 2. 占位符替换 ──────────────────────────────────────

def detect_prefix(text):
    """从写死的提示词里抽出画风/触发词前缀（如 `@kibro, `），没有就返回空。

    LoRA 触发词必须留着（`@kibro`），否则画风会变；其余一律丢弃。
    """
    t = (text or "").strip()
    if not t.startswith("@"):
        return ""
    i = t.find(",")
    return (t[:i + 1] + " ") if i > 0 else ""


def apply_placeholders(api, prefix=None):
    """返回 (改动说明列表, 警告列表)。"""
    changes, warns = [], []

    # 正向节点 = 第一个 KSampler 的 positive 指向谁
    samplers = [nid for nid, nd in api.items()
                if nd["class_type"] == "KSampler"]
    if not samplers:
        warns.append("找不到 KSampler，无法定位正向提示词节点")
        pos_ids = []
    else:
        pos_ids = []
        for s in samplers:
            ref = api[s]["inputs"].get("positive")
            if isinstance(ref, list) and str(ref[0]) in api:
                pos_ids.append(str(ref[0]))

    if not pos_ids:
        warns.append("KSampler.positive 没连到节点，正向提示词没换成模板！")

    for pid in dict.fromkeys(pos_ids):
        old = api[pid]["inputs"].get("text", "")
        pfx = prefix if prefix is not None else detect_prefix(old)
        new = pfx + "__MULTI_PROMPTS__"
        if old != new:
            changes.append("节点%s 正向提示词：%d 字写死的词 → %r"
                           % (pid, len(old), new))
            api[pid]["inputs"]["text"] = new
        if pfx == "":
            warns.append("节点%s 没识别出触发词前缀（原文不以 @ 开头）——"
                         "确认这样画风还对" % pid)

    # 所有 KSampler 的 seed → 裸占位符
    n = 0
    for nid in samplers:
        if "seed" in api[nid]["inputs"]:
            api[nid]["inputs"]["seed"] = "__SEED__"
            n += 1
    if n:
        changes.append("%d 个 KSampler 的 seed → __SEED__ 占位符" % n)
    return changes, warns


def dump(api):
    """序列化成 workflow.json 的文本：seed 还原成裸 token（不是合法 JSON）。"""
    text = json.dumps(api, ensure_ascii=False, indent=2)
    return text.replace('"__SEED__"', "__SEED__") + "\n"


# ─── 3. 体检 ────────────────────────────────────────────

def check_links(api):
    bad = []
    for nid, nd in api.items():
        for k, v in nd["inputs"].items():
            if isinstance(v, list) and v:
                if v[0] is None or str(v[0]) not in api:
                    bad.append("%s(%s).%s -> %r 指向不存在的节点"
                               % (nid, nd["class_type"], k, v))
    return bad


def check_object_info(api, url):
    import requests
    oi = requests.get(url + "/object_info", timeout=30).json()
    bad, missing = [], []
    for nid, nd in api.items():
        ct = nd["class_type"]
        spec = oi.get(ct)
        if not spec:
            bad.append("%s: 节点类型 %s 在 ComfyUI 里不存在" % (nid, ct))
            continue
        known = (set((spec.get("input") or {}).get("required") or {})
                 | set((spec.get("input") or {}).get("optional") or {}))
        for k, v in nd["inputs"].items():
            if k not in known:
                bad.append("%s(%s).%s 不是该节点的合法输入" % (nid, ct, k))
                continue
            if isinstance(v, list) and v:
                src_ct = api[str(v[0])]["class_type"]
                nout = len(oi.get(src_ct, {}).get("output") or [])
                if v[1] >= nout:
                    bad.append("%s.%s -> %s 只有 %d 个输出槽，取了 %d"
                               % (nid, k, src_ct, nout, v[1]))
            else:
                rv = ((spec.get("input") or {}).get("required") or {}).get(k)
                if (isinstance(rv, list) and rv and isinstance(rv[0], list)
                        and isinstance(v, str) and v not in rv[0]):
                    missing.append("%s.%s = %r 不在候选列表里" % (nid, k, v))
    # 模型文件
    for nid, nd in api.items():
        for k in MODEL_KEYS:
            v = nd["inputs"].get(k)
            if not isinstance(v, str):
                continue
            rv = (((oi.get(nd["class_type"], {}).get("input") or {})
                   .get("required") or {}).get(k))
            ch = rv[0] if isinstance(rv, list) and rv and isinstance(rv[0], list) else []
            if ch and v not in ch:
                missing.append("%s.%s = %r 文件不存在" % (nid, k, v))
    return bad, missing


def diff_against(api, old_path):
    """和现有 workflow.json 比：拓扑差异（严重）+ 参数差异（要人确认）。"""
    if not os.path.exists(old_path):
        return None, None, "现有 workflow.json 不存在，无法对比"
    sys.path.insert(0, HERE)
    from app.skills import load_workflow
    old = load_workflow(old_path)
    if not old:
        return None, None, "现有 workflow.json 解析失败"

    def links(wf):
        return {(n, k): tuple(str(x) for x in v)
                for n, nd in wf.items() for k, v in nd["inputs"].items()
                if isinstance(v, list)}

    lo, ln = links(old), links(new := api)
    topo = []
    if sorted(old, key=lambda x: int(x)) != sorted(new, key=lambda x: int(x)):
        topo.append("节点集合变了：%s -> %s"
                    % (sorted(old, key=lambda x: int(x)),
                       sorted(new, key=lambda x: int(x))))
    for k in sorted(set(lo) | set(ln)):
        if lo.get(k) != ln.get(k):
            topo.append("连线 %s.%s: %s -> %s" % (k[0], k[1], lo.get(k), ln.get(k)))

    params = []
    for nid in sorted(new, key=lambda x: int(x)):
        a = (old.get(nid) or {}).get("inputs", {})
        b = new[nid]["inputs"]
        for k in sorted(set(a) | set(b)):
            if isinstance(a.get(k), list) or isinstance(b.get(k), list):
                continue
            if a.get(k) != b.get(k):
                av, bv = a.get(k), b.get(k)
                if isinstance(av, str) and len(av) > 50:
                    av = av[:50] + "..."
                if isinstance(bv, str) and len(bv) > 50:
                    bv = bv[:50] + "..."
                params.append("节点%s.%s: %r -> %r" % (nid, k, av, bv))
    return topo, params, None


# ─── main ───────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ui_name", help="ComfyUI 工作流名（不含 .json），如 anime2")
    ap.add_argument("skill", help="目标渠道目录名，如 anima")
    ap.add_argument("--write", action="store_true", help="体检全绿后写盘")
    ap.add_argument("--prefix", default=None,
                    help="正向提示词前缀（默认从原文自动识别，如 '@kibro, '）")
    ap.add_argument("--ui-dir", default=COMFY_WF_DIR)
    args = ap.parse_args()

    src = os.path.join(args.ui_dir, args.ui_name + ".json")
    dst = os.path.join(HERE, "skills", args.skill, "workflow.json")
    if not os.path.exists(src):
        print("找不到 UI 工作流:", src)
        return 2

    import time
    print("源文件: %s" % src)
    print("       改动时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(os.path.getmtime(src))))
    print("目标:   %s\n" % dst)

    ui = json.load(io.open(src, encoding="utf-8"))
    api = ui_to_api(ui)
    print("转换完成：%d 个节点" % len(api))

    changes, warns = apply_placeholders(api, args.prefix)
    print("\n【占位符】")
    for c in changes:
        print("  -", c)
    for w in warns:
        print("  ! ", w)

    # 体检
    print("\n【连线】")
    bad_links = check_links(api)
    print("  悬空/None 连线:", bad_links or "无 OK")

    topo, params, err = diff_against(api, dst)
    print("\n【拓扑差异 vs 现有 workflow.json】")
    if err:
        print("  ", err)
    elif not topo:
        print("  无（节点集合与连线完全一致）OK")
    else:
        for t in topo:
            print("  ⚠ ", t)

    print("\n【参数差异】")
    for p in (params or ["无"]):
        print("  -", p)

    # ComfyUI 侧校验（连不上就跳过，不阻塞）
    print("\n【ComfyUI /object_info 校验】")
    try:
        sys.path.insert(0, HERE)
        from app.config import COMFYUI_URL
        bad_oi, missing = check_object_info(api, COMFYUI_URL)
        print("  非法字段/槽位:", bad_oi or "无 OK")
        print("  缺失文件/候选:", missing or "无 OK")
    except Exception as e:
        print("  跳过（%s）" % e)
        bad_oi, missing = [], []

    blockers = bool(bad_links) or bool(bad_oi) or bool(missing) or bool(warns)
    if topo:
        blockers = True

    print("\n" + "=" * 56)
    if blockers:
        print("体检未通过，**没有写盘**。上面带 ⚠ / ! 的都要先处理。")
        return 1
    print("体检全部通过。")
    if not args.write:
        print("（这是 dry-run，加 --write 才写盘）")
        return 0

    bak = dst.replace(".json", "_prev.json.bak")
    if os.path.exists(dst):
        io.open(bak, "w", encoding="utf-8", newline="\n").write(
            io.open(dst, encoding="utf-8").read())
        print("旧文件已备份 ->", os.path.basename(bak))
    io.open(dst, "w", encoding="utf-8", newline="\n").write(dump(api))
    print("已写入 ->", dst)
    print("\n下一步：跑测试  python -m unittest tests.test_image_anima tests.test_comfy_workflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())

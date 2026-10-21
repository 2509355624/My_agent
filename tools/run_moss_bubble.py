"""moss 2×2 四格 + 气泡：两条路线的性价比对照。

**要回答的问题**：做小漫画（一页 2×2 四格带对话气泡），

    路线 A（一步到位）：moss 直接出四格 + 气泡   —— 测 30 步 / 50 步两档
    路线 B（两段式）  ：moss 4 步先出干净四格 → qwen 25 步只加气泡

哪条更快、画质更值、气泡字更准。

**路线细节**：
- moss30：`skills/moss50/workflow.json` 的骨架（KSampler + SLG 22 + 负向），只把 steps 改 30。
  ⚠️ 不能改 `skills/moss` 的 4 步档 —— 那是 RapidMOSSAdapter 蒸馏 LoRA，改 steps 会崩。
- moss50：同一骨架原样 50 步。
- 两者都用 1024×1024（2×2 正好每格 512 见方），同 seed。
- moss4：`skills/moss` 4 步档，1024×1024，**提示词不含气泡**（干净底图）。
- qwen25：`skills/qwen_image_v1/workflow_i2i.json`，25 步，输入 = moss4 那张，
  **掐掉 2x 放大**（SaveImage 直接接 SplitImageWithAlpha），否则时间不可比。

**气泡文案（四格都用）**：
    左上 猎户拉弓    「谁在那儿？」
    右上 猎户特写    「是精怪？」
    左下 兔子精跪地  「别……别射」
    右下 兔子精走远  「谢谢……」

用法:
    python tools/run_moss_bubble.py                # 四条全跑
    python tools/run_moss_bubble.py --only moss30,qwen25
    python tools/run_moss_bubble.py --dry
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import comfy_src, image_jobs            # noqa: E402
from app.config import COMFYUI_OUTPUT_DIR        # noqa: E402
from app.skills import load_workflow            # noqa: E402
from app.tools.normal.generate_image import apply_prompt_placeholders  # noqa: E402

OUT_DIR = "web/实测_moss气泡"
WIDTH, HEIGHT = 1024, 1024

STYLE = ("anime style, clean line art, flat cel shading, cold pale blue morning light, "
         "muted color palette")

SCENE = ("a misty pine forest in the early morning, thin fog between the tall tree trunks, "
         "fallen brown leaves on the ground")

HUNTER = ("a young hunter with short dark brown hair, a brown linen tunic, a dark leather "
          "vest, a quiver of arrows on his back, holding a long wooden bow")

RABBIT = ("a rabbit spirit girl with long white hair, two long white rabbit ears standing up "
          "from her head, red eyes, a white kimono with a pale blue sash")

# 四格内容（moss 和 qwen 共用同一套叙事）
C1 = ("%s is kneeling on one knee with the bow drawn, aiming toward the right, seen from "
      "the side" % HUNTER)
C2 = ("a close-up of the hunter's face, eyes wide in surprise, lips slightly parted" )
C3 = ("%s is crouching on the ground with both hands pressed to her chest, looking up with "
      "wide red eyes" % RABBIT)
C4 = ("%s is walking away between the foggy tree trunks, her back to the viewer, her head "
      "turned back over one shoulder" % RABBIT)

BUBBLE_EN = ('All text is clean handwritten Chinese, black ink inside a white rounded '
             'speech bubble with a thin black outline, the Chinese characters are correct '
             'and legible.')

# ── 路线 A：一步到位（四格 + 气泡）──────────────────────────────
PROMPT_A = (
    "A comic page divided into exactly four panels in a two by two grid, separated by thin "
    "white gutters. %s. %s. "
    "Top left panel: %s. A small speech bubble near his mouth reads 「谁在那儿？」. "
    "Top right panel: %s. A small speech bubble near his mouth reads 「是精怪？」. "
    "Bottom left panel: %s. A small speech bubble near her mouth reads 「别……别射」. "
    "Bottom right panel: %s. A small speech bubble beside her head reads 「谢谢……」. "
    "%s"
) % (STYLE, SCENE, C1, C2, C3, C4, BUBBLE_EN)

# ── 路线 B 第一段：干净四格（无气泡、无文字）─────────────────────
PROMPT_B_BASE = (
    "A comic page divided into exactly four panels in a two by two grid, separated by thin "
    "white gutters. %s. %s. "
    "Top left panel: %s. "
    "Top right panel: %s. "
    "Bottom left panel: %s. "
    "Bottom right panel: %s. "
    "No text, no speech bubble."
) % (STYLE, SCENE, C1, C2, C3, C4)

# ── 路线 B 第二段：qwen 只加气泡 ──────────────────────────────
PROMPT_B_QWEN = (
    "把这张四格漫画加上对话气泡，其它一切都不要改变：四格的分割线位置、格内构图、"
    "人物、服装、背景、画风全部保持原样。只做加气泡这一件事。\n"
    "第 1 格（左上）：猎户嘴边的白色气泡，写着「谁在那儿？」\n"
    "第 2 格（右上）：猎户嘴边的白色气泡，写着「是精怪？」\n"
    "第 3 格（左下）：兔子精嘴边的白色气泡，写着「别……别射」\n"
    "第 4 格（右下）：兔子精头旁边的白色气泡，写着「谢谢……」\n"
    "气泡为白色圆角、细黑描边，文字为黑色手写体中文，清晰可读，不要遮挡人脸。"
)

SEED_A30, SEED_A50, SEED_B4, SEED_QWEN = 20261201, 20261201, 20261202, 20261203


def _moss_kernel():
    """moss30 / moss50 共用的骨架（moss50 原样拷贝，调用方改 steps）。"""
    return load_workflow(os.path.join(ROOT, "skills", "moss50", "workflow.json"))


def _run(workflow, skill, prompt, seed, label):
    job, why = image_jobs.enqueue(None, None, workflow, skill=skill,
                                  prompt=prompt[:200], seed=seed, out_dir=OUT_DIR)
    if job is None:
        print("[%s] 没进队列：%s" % (label, why))
        return None, 0.0
    print("[%s] 入队 %s（seed %d），等出图…" % (label, job.tag, seed), flush=True)
    t0 = time.time()
    try:
        job.wait()
    except Exception as exc:                    # noqa: BLE001
        print("[%s] 失败：%s（等了 %.0f 秒）" % (label, exc, time.time() - t0))
        return None, time.time() - t0
    cost = time.time() - t0
    files = image_jobs.output_images(job.entry, job=job)
    print("[%s] 完成 %.1f 秒 → %s" % (label, cost, files), flush=True)
    return (files[0] if files else None), cost


def run_moss_steps(steps, prompt, seed, label):
    wf = _moss_kernel()
    if not wf:
        return None, 0.0
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    wf["40"]["inputs"]["steps"] = steps
    wf["60"]["inputs"]["filename_prefix"] = "AgentGen_Bubble%d" % steps
    doc = apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)
    return _run(doc, "moss50", prompt, seed, label)


def run_moss4(prompt, seed, label):
    wf = load_workflow(os.path.join(ROOT, "skills", "moss", "workflow.json"))
    if not wf:
        return None, 0.0
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    wf["90"]["inputs"]["filename_prefix"] = "AgentGen_Bubble4"
    doc = apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)
    return _run(doc, "moss", prompt, seed, label)


def run_qwen(src_path, prompt, seed, label):
    wf = load_workflow(os.path.join(ROOT, "skills", "qwen_image_v1", "workflow_i2i.json"))
    if not wf:
        return None, 0.0
    # 掐掉 2x 放大：SaveImage 直接吃 SplitImageWithAlpha（节点 45）
    wf["50"]["inputs"]["images"] = ["45", 0]
    wf["30"]["inputs"]["steps"] = 25
    wf["50"]["inputs"]["filename_prefix"] = "AgentGen_BubbleQwen"
    # output_images 回的是**相对 ComfyUI output 目录**的路径，真实文件在 COMFYUI_OUTPUT_DIR 下
    if not os.path.exists(src_path):
        alt = os.path.join(COMFYUI_OUTPUT_DIR, src_path) if COMFYUI_OUTPUT_DIR else ""
        if alt and os.path.exists(alt):
            src_path = alt
    raw = open(src_path, "rb").read()
    try:
        uploaded = comfy_src.upload(raw)
    except Exception as exc:                    # noqa: BLE001
        print("[%s] 源图上传失败：%s" % (label, exc))
        return None, 0.0
    doc = apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)
    doc["13"]["inputs"]["image"] = uploaded
    print("[%s] 源图 %s → %s" % (label, os.path.basename(src_path), uploaded), flush=True)
    return _run(doc, "qwen_image_v1", prompt, seed, label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    keys = [x.strip() for x in args.only.split(",") if x.strip()] or ["moss30", "moss50", "moss4", "qwen25"]

    if args.dry:
        for k in keys:
            p = PROMPT_B_QWEN if k == "qwen25" else (PROMPT_B_BASE if k == "moss4" else PROMPT_A)
            print("── %s  提示词 %d 字符" % (k, len(p)))
        return

    cost = {}
    files = {}

    if "moss30" in keys:
        files["moss30"], cost["moss30"] = run_moss_steps(30, PROMPT_A, SEED_A30, "moss30")
    if "moss50" in keys:
        files["moss50"], cost["moss50"] = run_moss_steps(50, PROMPT_A, SEED_A50, "moss50")

    if "moss4" in keys:
        files["moss4"], cost["moss4"] = run_moss4(PROMPT_B_BASE, SEED_B4, "moss4")

    if "qwen25" in keys:
        # --only qwen25 时 files 里没有 moss4，回落到上一次跑出来的底图
        src = files.get("moss4") or "web/实测_moss气泡/moss/AgentGen_Bubble4_00001_.png"
        src_abs = src if os.path.isabs(src) else os.path.join(ROOT, src)
        if not os.path.exists(src_abs):
            alt = os.path.join(COMFYUI_OUTPUT_DIR, src) if COMFYUI_OUTPUT_DIR else ""
            src_abs = alt if alt and os.path.exists(alt) else src_abs
        if not os.path.exists(src_abs):
            print("[qwen25] 没有 moss4 的底图，跳过：%s" % src_abs)
        else:
            files["qwen25"], cost["qwen25"] = run_qwen(src_abs, PROMPT_B_QWEN, SEED_QWEN, "qwen25")

    print("\n=== 耗时 ===")
    for k in keys:
        if k in cost:
            print("  %-8s %6.1f 秒" % (k, cost[k]))
    if "moss4" in cost and "qwen25" in cost:
        print("  路线B合计 %6.1f 秒（moss4 + qwen25）" % (cost["moss4"] + cost["qwen25"]))
    print("\n=== 产出 ===")
    for k in keys:
        print("  %-8s → %s" % (k, files.get(k) or "没出来"))


if __name__ == "__main__":
    main()

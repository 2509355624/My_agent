"""moss 30 步 · 三页漫画排版测试（单格 / 2 格 / 3 格）。

**要回答的问题**：真做漫画要几页讲完一个故事，那每一页该排几格？
这一轮固定 30 步（已定的基调），三页统一 1024×1536 竖版，只变格数：

    P1  单格    整页一格，无格沟        —— 建立镜头，测"大格撑不撑得住"
    P2  上下两格 等分                    —— 测对话/推进格
    P3  上大下二小                      —— 真实漫画页最常见的版式

判据（按重要性）：
    ① 格数对不对、格沟干不干净（有没有多画格 / 格子糊成一片）
    ② 每格内容有没有画对（不该出现"一格画了两件事"）
    ③ 中文气泡有没有错字、位置会不会压脸
    ④ 三页放一起，同一角色/同一场景稳不稳（跨页连续性）
    ⑤ 页面留白够不够（后面要贴字幕的位置）

用法:
    python tools/run_moss_page3.py            # 三页全跑
    python tools/run_moss_page3.py --only p2  # 单跑某页
    python tools/run_moss_page3.py --dry
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import image_jobs                      # noqa: E402
from app.skills import load_workflow            # noqa: E402
from app.tools.normal.generate_image import apply_prompt_placeholders  # noqa: E402

SKILL = "moss50"          # 骨架用 moss50（KSampler + SLG 22 + 负向），steps 改 30
STEPS = 30
OUT_DIR = "web/实测_moss三页"
WIDTH, HEIGHT = 1024, 1536  # 竖版，16 的倍数
SEED_BASE = 20261301

# ── 固定串（三页逐字复制，moss 没有角色参考图，一致性全靠描述一字不差）──
STYLE = ("anime style, clean line art, flat cel shading, soft cinematic lighting, "
         "muted color palette, night scene")

SCENE_OUT = ("the entrance of a small convenience store at night, heavy rain falling, wet "
             "asphalt reflecting warm light from inside, a glowing white store sign above "
             "the door")

SCENE_IN = ("inside a small convenience store, bright warm fluorescent light, shelves of "
            "snacks and drinks, large front windows with rain streaks running down the "
            "glass, a small round table and two stools by the window")

GIRL = ("a girl with shoulder-length wavy brown hair, a mustard yellow knit scarf, an olive "
        "green duffel coat, holding a clear plastic umbrella")

BOY = ("a boy with short black hair, a navy blue hooded jacket soaked dark with rain, "
       "silver headphones hanging around his neck")

BUBBLE = ('All text is clean handwritten Chinese, black ink inside a white rounded speech '
          'bubble with a thin black outline, the Chinese characters are correct and legible.')

# ── 三页 ──────────────────────────────────────────────────
PAGES = [
    {
        "key": "p1",
        "label": "第1页 · 单格（建立镜头）",
        "seed": SEED_BASE,
        "prefix": "AgentGen_P3P1",
        "prompt": (
            "A single full-page comic panel with no borders and no gutters. %s. %s. "
            "%s is standing outside the store with the umbrella held low, seen from behind "
            "at a slight angle, her head turned toward the bright window, the rain falling "
            "in visible streaks across the frame. Wide shot, the store building fills the "
            "right half of the page, the dark wet street on the left. No text, no speech "
            "bubble."
        ) % (STYLE, SCENE_OUT, GIRL),
    },
    {
        "key": "p2",
        "label": "第2页 · 上下两格",
        "seed": SEED_BASE + 1,
        "prefix": "AgentGen_P3P2",
        "prompt": (
            "A comic page divided into exactly two horizontal panels of equal size, "
            "separated by a thin white gutter. %s. "
            "Top panel: %s. %s is pushing the glass door open and stepping in from the rain, "
            "water dripping from the umbrella tip, her scarf damp, seen from the front at "
            "medium shot, the door chime above her head. A small hand-lettered sound effect "
            "near the door reads 「叮咚」. "
            "Bottom panel: %s. %s walks between two shelves of snacks and looks toward the "
            "window, seen from behind over her shoulder, the round table by the window is "
            "visible in the distance. "
            "No speech bubble in the bottom panel."
        ) % (STYLE, SCENE_IN, GIRL, SCENE_IN, GIRL),
    },
    {
        "key": "p3",
        "label": "第3页 · 上大下二小",
        "seed": SEED_BASE + 2,
        "prefix": "AgentGen_P3P3",
        "prompt": (
            "A comic page divided into exactly three panels: one large wide panel filling "
            "the top half, and two smaller square panels side by side on the bottom half, "
            "all separated by thin white gutters. %s. "
            "Top panel: %s. %s is sitting on a stool at the round table, his jacket dripping, "
            "and %s is standing in front of him holding out a can of hot coffee with one "
            "hand. They look at each other. Medium shot, both figures in the same frame. "
            "A small speech bubble beside her mouth reads 「给。」. "
            "Bottom left panel: a close-up of her hand holding out the warm coffee can, "
            "steam rising from the open tab, his hand reaching to take it. "
            "Bottom right panel: the two of them sitting side by side on the stools facing "
            "the window, seen from behind, the rain on the glass getting lighter. "
            "A small speech bubble beside his head reads 「谢谢。」"
        ) % (STYLE, SCENE_IN, BOY, GIRL),
    },
]


def build(page, base_wf):
    wf = json.loads(json.dumps(base_wf))
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    wf["40"]["inputs"]["steps"] = STEPS
    wf["60"]["inputs"]["filename_prefix"] = page["prefix"]
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False),
                                    page["prompt"], page["seed"])


def run(page, base_wf):
    wf = build(page, base_wf)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL,
                                  prompt=page["prompt"][:200], seed=page["seed"],
                                  out_dir=OUT_DIR)
    if job is None:
        print("[%s] 没进队列：%s" % (page["key"], why))
        return None
    print("[%s] 入队 %s（seed %d），等出图…" % (page["key"], job.tag, page["seed"]),
          flush=True)
    t0 = time.time()
    try:
        job.wait()
    except Exception as exc:                    # noqa: BLE001
        print("[%s] 失败：%s（等了 %.0f 秒）" % (page["key"], exc, time.time() - t0))
        return None
    cost = time.time() - t0
    files = image_jobs.output_images(job.entry, job=job)
    print("[%s] 完成 %.1f 秒 → %s" % (page["key"], cost, files), flush=True)
    return files[0] if files else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    base_wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not base_wf:
        sys.exit("moss50 的 workflow.json 读不出来")

    groups = PAGES
    if args.only.strip():
        keys = [x.strip() for x in args.only.split(",") if x.strip()]
        groups = [p for p in PAGES if p["key"] in keys]

    print("画布 %dx%d / steps %d / 共 %d 页" % (WIDTH, HEIGHT, STEPS, len(groups)))

    if args.dry:
        for p in groups:
            print("\n── %s %s（seed %d）" % (p["key"], p["label"], p["seed"]))
            print("   提示词 %d 字符" % len(p["prompt"]))
            print("   " + p["prompt"][:150].replace("\n", " ") + "…")
        return

    done = {}
    for p in groups:
        done[p["key"]] = run(p, base_wf)

    print("\n=== 产出 ===")
    for p in groups:
        print("  %-4s → %s" % (p["key"], done.get(p["key"]) or "没出来"))


if __name__ == "__main__":
    main()

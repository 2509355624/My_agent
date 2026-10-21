"""moss 4 步档 · 「一页多分镜」效果测试（同一张图里排 2 / 3 / 4 格）。

**为什么单独测**：之前整页 5 格是在 `moss_page`（1024×1536 / 50 步）上跑通的，
那是慢档。4 步 DMD 在 704×928 上能不能画格沟、格内还能不能看清脸，是另一回事。

三张对照，同一段故事（猎户 → 兔子精），格数递增：
    panels2  上下两格（每格 704×464）
    panels3  上大下二小（每格更小，最接近真实漫画页版式）
    panels4  2×2 四格

判据（按重要性）：
    ① 格沟有没有真的画出来、格子数对不对
    ② 每一格的内容有没有画对（不是整张糊成一团）
    ③ 格内角色特征还认不认得出
    ④ 有没有串格（一格的内容溢到隔壁）

用法:
    python tools/run_moss_panels.py
    python tools/run_moss_panels.py --only panels3
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

SKILL = "moss"
OUT_DIR = "web/实测_moss多格"
WIDTH, HEIGHT = 704, 928
SEED_BASE = 20261101

STYLE = "anime style, clean line art, flat cel shading, cold pale blue morning light"

SCENE = ("a misty pine forest in the early morning, thin fog between the tall tree trunks, "
         "fallen brown leaves on the ground")

HUNTER = ("a young hunter with short dark brown hair, a brown linen tunic, a dark leather "
          "vest, a quiver of arrows on his back, holding a long wooden bow")

RABBIT = ("a rabbit spirit girl with long white hair, two long white rabbit ears standing up "
          "from her head, red eyes, a white kimono with a pale blue sash")

# 两个可复用的单格内容
CELL_DRAW = ("%s is kneeling and pulling the bowstring back to his cheek, aiming toward the "
             "right, seen from the side" % HUNTER)
CELL_RABBIT = ("%s is crouching on the ground with both hands pressed to her chest, looking up "
               "with wide red eyes" % RABBIT)
CELL_FACE = ("a close-up of %s, eyes wide in surprise, lips slightly parted" % HUNTER)
CELL_WALK = ("%s is walking away between the foggy trunks, her back to the viewer" % RABBIT)

VARIANTS = [
    {
        "key": "panels2",
        "label": "上下两格",
        "seed": SEED_BASE + 1,
        "prompt": (
            "A comic page divided into exactly two horizontal panels of equal size, "
            "separated by a thin white gutter. "
            "Top panel: %s. %s. %s. "
            "Bottom panel: %s. %s. "
            "Entire page in %s. No text, no speech bubble."
        ) % (SCENE, CELL_DRAW, "", SCENE, CELL_RABBIT, STYLE),
    },
    {
        "key": "panels3",
        "label": "上大下二小（最接近真实版式）",
        "seed": SEED_BASE + 2,
        "prompt": (
            "A comic page divided into exactly three panels: one large wide panel filling the "
            "top half, and two smaller square panels side by side on the bottom half, all "
            "separated by thin white gutters. "
            "Top panel: %s. %s. "
            "Bottom left panel: %s. %s. "
            "Bottom right panel: %s. %s. %s. "
            "Entire page in %s. No text, no speech bubble."
        ) % (SCENE, CELL_DRAW, SCENE, CELL_FACE, SCENE, CELL_RABBIT, "", STYLE),
    },
    {
        "key": "panels4",
        "label": "2×2 四格",
        "seed": SEED_BASE + 3,
        "prompt": (
            "A comic page divided into exactly four panels in a two by two grid, "
            "separated by thin white gutters. "
            "Top left panel: %s. %s. "
            "Top right panel: %s. %s. "
            "Bottom left panel: %s. "
            "Bottom right panel: %s. "
            "Entire page in %s. No text, no speech bubble."
        ) % (SCENE, CELL_DRAW, SCENE, CELL_FACE, CELL_RABBIT, CELL_WALK, STYLE),
    },
]


def build(v, base_wf):
    wf = json.loads(json.dumps(base_wf))
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False),
                                    v["prompt"], v["seed"])


def run(v, base_wf):
    wf = build(v, base_wf)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL,
                                  prompt=v["prompt"][:200], seed=v["seed"],
                                  out_dir=OUT_DIR)
    if job is None:
        print("[%s] 没进队列：%s" % (v["key"], why))
        return None
    print("[%s] 入队 %s（seed %d），等出图…" % (v["key"], job.tag, v["seed"]), flush=True)
    t0 = time.time()
    try:
        job.wait()
    except Exception as exc:                    # noqa: BLE001
        print("[%s] 失败：%s（等了 %.0f 秒）" % (v["key"], exc, time.time() - t0))
        return None
    files = image_jobs.output_images(job.entry, job=job)
    print("[%s] 完成 %.1f 秒 → %s" % (v["key"], time.time() - t0, files), flush=True)
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    base_wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not base_wf:
        sys.exit("moss 的 workflow.json 读不出来")

    groups = VARIANTS
    if args.only.strip():
        keys = [x.strip() for x in args.only.split(",") if x.strip()]
        groups = [v for v in VARIANTS if v["key"] in keys]

    print("画布 %dx%d / steps %s / 共 %d 组"
          % (WIDTH, HEIGHT, base_wf["40"]["inputs"]["steps"], len(groups)))

    if args.dry:
        for v in groups:
            print("\n── %s %s（seed %d）" % (v["key"], v["label"], v["seed"]))
            print("   %d 字符" % len(v["prompt"]))
        return

    for v in groups:
        run(v, base_wf)


if __name__ == "__main__":
    main()

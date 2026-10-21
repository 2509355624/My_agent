"""moss 4 步档 · 画风可调性对照（同 seed / 同主体 / 只换画风串）。

**为什么要做这个测试**：用户问「moss 的画风能不能调」。先把能调的项钉清楚：

`skills/moss/workflow.json` 只有 11 个节点，可调项一共四个 ——
    20.text（提示词）、30.width/height、40.steps、40.lora_strength、60.noise_seed
`RapidMOSSAdapter` 的 adapter 只有 `rapid-moss-4step` 一个选项（本地没装 lodestones 8 步），
steps 上限 8 但 adapter 只编了 4 步；**没有 cfg、没有 sampler/scheduler、没有负向入口**
（`BasicGuider` 是 flux 式无 CFG 采样）；`models/loras/` 是空的，**没有一个风格 LoRA**。

所以画风能动的杠杆按有效性排序：
    ① 提示词风格串（本轮主角） ② 分辨率（只改细节密度，不改画风）
    ③ lora_strength（蒸馏 LoRA 降强度，见 STRENGTH 组） ④ seed

**对照组设计**：主体动作逐字固定为「单膝跪地拉满弓」，
唯一变量是提示词最前面那段风格串。同 seed = 20261032，跟 `web/实测_moss猎户/P1_拉满弓.png` 同种子，
可以直接跟原来那张对比。

用法:
    python tools/run_moss_style.py              # 全部跑
    python tools/run_moss_style.py --only oil   # 只跑某几组
    python tools/run_moss_style.py --dry
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
OUT_DIR = "web/实测_moss画风"
WIDTH, HEIGHT = 704, 928
SEED = 20261032                                 # 与 P1_拉满弓 同种子

# ── 除风格串外，以下三段逐字固定 ──────────────────────────────────
SCENE = ("a misty pine forest in the early morning, thin fog between the tall tree trunks, "
         "fallen brown leaves on the ground")

HUNTER = ("a young hunter with short dark brown hair, a brown linen tunic, a dark leather "
          "vest, a quiver of arrows on his back, brown trousers and leather boots")

ACTION = ("He is kneeling on one knee on the fallen leaves, his left arm fully extended "
          "holding a long wooden bow upright in front of him, his right hand pulled back "
          "at his cheek, an arrow nocked and aiming toward the right edge of the frame. "
          "His body is turned sideways, one eye closed, the other staring along the arrow. "
          "Medium shot from the side. No text, no speech bubble.")

VARIANTS = [
    {
        "key": "base",
        "label": "基准（当前用的日式赛璐璐）",
        "note": "跟上一次 P1 完全相同，用来确认这轮没有别的变量跑偏",
        "style": ("anime style, clean line art, flat cel shading, cold pale blue morning light, "
                  "muted color palette, slightly desaturated"),
    },
    {
        "key": "retro90",
        "label": "90 年代复古赛璐璐",
        "note": "手绘感 + 胶片颗粒 + 厚黑线，考的是模型有没有这个画风的内存",
        "style": ("1990s retro anime style, hand-painted cel shading, thick black ink outlines, "
                  "visible film grain, slightly faded warm colors, mild color bleed between "
                  "shapes"),
    },
    {
        "key": "oil",
        "label": "厚涂油画",
        "note": "跟赛璐璐平涂完全相反的方向，看能不能真的把「平涂」掰成「笔触」",
        "style": ("oil painting, thick impasto brush strokes, visible canvas texture, "
                  "chiaroscuro lighting, muted earth tones, painterly edges instead of line art"),
    },
    {
        "key": "water",
        "label": "水彩插画",
        "note": "不画线、靠色块边缘，是线稿依赖度最低的一档",
        "style": ("soft watercolor illustration, wet washes, bleeding pigment edges, "
                  "visible cold-press paper texture, pale pastel palette, no hard outlines"),
    },
    {
        "key": "manga",
        "label": "黑白网点漫画",
        "note": "最强的判据：如果能出纯黑白网点，说明色彩完全由提示词控制",
        "style": ("black and white manga panel, screentone dot patterns for shadows, "
                  "ink hatching, pure monochrome, no color at all"),
    },
    {
        "key": "clay3d",
        "label": "3D 黏土 / 盲盒",
        "note": "跨媒介方向，考的是「渲染材质」这类词对 4 步还有没有效",
        "style": ("3D rendered character, soft clay material, subsurface scattering, "
                  "soft studio softbox lighting, shallow depth of field, octane render, "
                  "smooth rounded shapes"),
    },
]

# lora_strength 对照：同一个基准画风，只把蒸馏 LoRA 强度从 1.0 降到 0.7
STRENGTH = {"key": "lora07", "label": "基准画风 + lora_strength 0.7",
            "note": "唯一能动的模型侧旋钮，看降强度能不能松出更多画风自由度",
            "style": VARIANTS[0]["style"], "lora_strength": 0.7}


def build(v, base_wf):
    wf = json.loads(json.dumps(base_wf))
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    if "lora_strength" in v:
        wf["40"]["inputs"]["lora_strength"] = v["lora_strength"]
    prompt = "%s. %s. %s. %s" % (v["style"], SCENE, HUNTER, ACTION)
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, SEED), prompt


def run(v, base_wf):
    wf, prompt = build(v, base_wf)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL,
                                  prompt=prompt[:200], seed=SEED, out_dir=OUT_DIR)
    if job is None:
        print("[%s] 没进队列：%s" % (v["key"], why))
        return None
    print("[%s] 入队 %s，等出图…" % (v["key"], job.tag), flush=True)
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
    ap.add_argument("--only", default="", help="逗号分隔的 key")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    base_wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not base_wf:
        sys.exit("moss 的 workflow.json 读不出来")

    groups = VARIANTS + [STRENGTH]
    if args.only.strip():
        keys = [x.strip() for x in args.only.split(",") if x.strip()]
        groups = [v for v in groups if v["key"] in keys]

    print("画布 %dx%d / steps %s / seed %d / 共 %d 组"
          % (WIDTH, HEIGHT, base_wf["40"]["inputs"]["steps"], SEED, len(groups)))

    if args.dry:
        for v in groups:
            _, prompt = build(v, base_wf)
            print("\n── %s  %s" % (v["key"], v["label"]))
            print("   %s" % v["note"])
            print("   风格串 %d 字符 / 整条 %d 字符" % (len(v["style"]), len(prompt)))
        return

    for v in groups:
        run(v, base_wf)


if __name__ == "__main__":
    main()

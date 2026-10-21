"""moss 30 步 · 跨题材三轮（悬疑 / 喜剧 / 武侠），验证"不用 tag 库、让模型自己当编剧"。

**这一轮要回答的问题**（用户 10-21 定）：
    如果漫画台给 moss 单开一条渠道，那 anima-tags 那套 32.8 万标签体系用不上——
    我们打算让编剧模型直接写自然语言剧本。那这条路到底能不能撑住**不同题材**？

所以本体不是"再测一次 moss"，是**拿三份完全不同题材的剧本**，全部用纯散文
（**零 tag 语法**：不写 1girl / from above / score_7 / (tag:1.2)），看 moss 能不能：

    G1 悬疑 · 教室  上下两格    —— 单角色 + 冷色调氛围 + **场景内文字**（黑板粉笔字）
    G2 喜剧 · 猫    2×2 四格    —— **非人角色**（猫）+ 夸张表情 + 拟声字 + 连续动作
    G3 武侠 · 窄巷  上大下二小  —— 双人**动作对峙** + 服装质感 + 局部特写

判据（按重要性）：
    ① 格数与格沟对不对（exactly N 是否生效）
    ② 纯散文能不能定住构图（不靠 tag，镜头/景别/方位靠句子说出来）
    ③ 跨题材的"画风"是否真的由风格串控制（三组色调/气质必须明显不同）
    ④ 场景内文字 / 气泡 / 拟声字有没有错字
    ⑤ 同一角色跨格一致性（固定串逐字复制的效果）

用法:
    python tools/run_moss_genres.py              # 三张全跑
    python tools/run_moss_genres.py --only g2    # 单跑
    python tools/run_moss_genres.py --dry        # 只打印提示词，不入队
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

SKILL = "moss50"          # 骨架沿用 moss50（KSampler + SLG + 负向端口），steps 压到 30
STEPS = 30
OUT_DIR = "web/实测_moss题材"
WIDTH, HEIGHT = 1024, 1536
SEED_BASE = 20261401

# ══════════════════════════════════════════════════════════════
#  G1 悬疑 · 教室（上下两格）
#     测：冷色调氛围 / 单角色 / 场景内文字（粉笔字）
# ══════════════════════════════════════════════════════════════
G1_STYLE = ("anime style, clean line art, flat cel shading, cool blue moonlight coming "
            "through the windows, deep shadows, quiet suspenseful atmosphere, cool color "
            "palette, film grain")

G1_SCENE = ("an empty high school classroom at night, rows of wooden desks and chairs "
            "arranged neatly, a large dark green blackboard on the front wall, tall windows "
            "on the left with moonlight falling across the floor, long shadows stretching "
            "between the desks")

G1_GIRL = ("a girl with long straight black hair tied in a low ponytail, a white "
           "short-sleeved school shirt, a dark green plaid pleated skirt, black loafers")

# ══════════════════════════════════════════════════════════════
#  G2 喜剧 · 猫（2×2 四格）
#     测：非人角色 / 夸张表情 / 拟声字 / 四格连续动作节奏
# ══════════════════════════════════════════════════════════════
G2_STYLE = ("anime style, clean line art, flat cel shading, bright warm morning sunlight, "
            "vivid colors, exaggerated comedic expressions")

G2_SCENE = ("a small messy bedroom in the morning, sunlight coming through half-drawn "
            "curtains, a rumpled bed with a white duvet, clothes piled on a chair beside it")

G2_BOY = "a young man with messy black hair, a light grey t-shirt"
G2_CAT = "a fat orange tabby cat with a white chest"

# ══════════════════════════════════════════════════════════════
#  G3 武侠 · 窄巷（上大下二小）
#     测：双人动作对峙 / 古装质感 / 局部特写（拔剑、眼睛）
# ══════════════════════════════════════════════════════════════
G3_STYLE = ("anime style, clean line art, flat cel shading, dramatic dusk backlight, warm "
            "orange sky, dust and wind in the air, wuxia atmosphere")

G3_SCENE = ("a narrow stone alley in an old Chinese town at dusk, red paper lanterns "
            "hanging overhead, wooden doors and paper windows on both sides, dust drifting "
            "in the air, dry leaves on the stone ground")

G3_WOMAN = ("a young swordswoman with long black hair tied up high, a white long-sleeved "
            "robe, a dark blue sash around her waist")

G3_MAN = ("a tall man in a black wide-sleeved robe, a conical bamboo hat pulled low, a "
          "curved saber hanging at his side")


PAGES = [
    # ── G1 ──────────────────────────────────────────────────
    {
        "key": "g1",
        "label": "G1 悬疑 · 教室（上下两格）",
        "seed": SEED_BASE,
        "prefix": "AgentGen_Genre_G1",
        "prompt": (
            "A comic page divided into exactly two horizontal panels of equal size, "
            "separated by a thin white gutter. %s. "
            "Top panel: %s. %s is standing at the back of the classroom between two rows of "
            "desks, seen from a distance in a wide shot, her face lit from below by the glow "
            "of a smartphone in her hand, the rest of the room dark and empty. "
            "Bottom panel: a close-up of the large dark green blackboard, lit by a narrow "
            "beam of moonlight. One line of Chinese characters is written on the blackboard "
            "in white chalk, clean and readable, reading 「你终于来了。」. Chalk dust floats in "
            "the light. The edge of her shoulder and ponytail is visible in the lower right "
            "corner, out of focus. "
            "No speech bubble anywhere on the page."
        ) % (G1_STYLE, G1_SCENE, G1_GIRL),
    },
    # ── G2 ──────────────────────────────────────────────────
    {
        "key": "g2",
        "label": "G2 喜剧 · 猫（2×2 四格）",
        "seed": SEED_BASE + 1,
        "prefix": "AgentGen_Genre_G2",
        "prompt": (
            "A comic page divided into exactly four panels in a two by two grid, separated "
            "by thin white gutters. %s. All four panels share the same location: %s. "
            "Top left panel: %s is asleep under the white duvet, and %s is sitting on his "
            "chest, looking down at his face, seen from above at a high angle. "
            "Top right panel: the cat slowly raises one front paw in the air with a smug "
            "expression, its eyes narrowed, the sleeping young man still in frame below. "
            "Bottom left panel: a close-up of the cat's paw pressing down on the young man's "
            "cheek, his face squashed and his eyes wide open in shock, his mouth open in a "
            "small yelp. A small hand-lettered sound effect beside the paw reads 「啪！」. "
            "Bottom right panel: the young man sits up in bed with his hair sticking up, "
            "looking around in confusion, while the cat is already sitting at the foot of "
            "the bed licking its paw and pretending nothing happened. "
            "No speech bubble anywhere on the page."
        ) % (G2_STYLE, G2_SCENE, G2_BOY, G2_CAT),
    },
    # ── G3 ──────────────────────────────────────────────────
    {
        "key": "g3",
        "label": "G3 武侠 · 窄巷（上大下二小）",
        "seed": SEED_BASE + 2,
        "prefix": "AgentGen_Genre_G3",
        "prompt": (
            "A comic page divided into exactly three panels: one large wide panel filling "
            "the top half, and two smaller panels side by side on the bottom half, all "
            "separated by thin white gutters. %s. "
            "Top panel: %s. %s stands at the far end of the alley facing the viewer, and %s "
            "stands in the near foreground with his back half turned, facing her. A wide "
            "shot, the gap between them fills the middle of the frame, dry leaves blown "
            "across the stone floor. "
            "Bottom left panel: %s is pulling a long straight sword out of its scabbard, the "
            "blade catching a thin line of orange light, seen from the side in a medium "
            "close-up, only her arm and upper body in frame. "
            "Bottom right panel: a tight close-up of the man's face under the bamboo hat, "
            "the brim casting a hard shadow over the top half of his face, only his eyes "
            "visible and looking straight at the viewer. "
            "No speech bubble anywhere on the page."
        ) % (G3_STYLE, G3_SCENE, G3_WOMAN, G3_MAN, G3_WOMAN),
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


def _abs(path):
    base = os.environ.get("COMFYUI_OUTPUT_DIR") or ""
    if not base or not path:
        return path
    return path if os.path.isabs(path) else os.path.join(base, path)


def run(page, base_wf):
    wf = build(page, base_wf)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL,
                                  prompt=page["prompt"][:200], seed=page["seed"],
                                  out_dir=OUT_DIR)
    if job is None:
        print("[%s] 没进队列：%s" % (page["key"], why), flush=True)
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
    files = [_abs(f) for f in image_jobs.output_images(job.entry, job=job)]
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

    print("画布 %dx%d / steps %d / 共 %d 页 / 风格：纯自然语言（零 tag）"
          % (WIDTH, HEIGHT, STEPS, len(groups)))

    if args.dry:
        for p in groups:
            print("\n── %s %s（seed %d）" % (p["key"], p["label"], p["seed"]))
            print("   提示词 %d 字符" % len(p["prompt"]))
            print("   " + p["prompt"][:200].replace("\n", " ") + "…")
        return

    done = {}
    for p in groups:
        done[p["key"]] = run(p, base_wf)

    print("\n=== 产出 ===")
    for p in groups:
        print("  %-4s → %s" % (p["key"], done.get(p["key"]) or "没出来"))


if __name__ == "__main__":
    main()

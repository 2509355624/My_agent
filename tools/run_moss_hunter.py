"""moss 4 步档 · 复杂动作 + 5 页连续性测试：《猎户与兔子精》。

剧本——清晨山林，猎户拉弓瞄准；发现猎物是兔子精，愣住；兔子精跪地求饶；
猎户放下弓、摊开手掌；兔子精转身跑进雾里，回头看他一眼。

**本轮测的是三件事**（不是测角色区分，那个上一轮已过）：
1. **复杂动作能不能表达清楚**：拉满弓（一手持弓一手三指扣弦）、松开弓弦、摊掌、转身回望。
   难点在于 4 步 DMD 只认粗动作，细指法会退化（上轮实测：`pointing with index finger` → 握拳）。
2. **跨页连续性**：同一角色、同一片林子、同一身衣服，5 张摆在一起读不读得出同一个故事。
3. **不带字幕的纯画面叙事**：用户明确说字幕之后交给 qwen 加，所以这里全部 `no text, no speech bubble`，
   能不能只看画面就懂，是这个测试的核心判据。

设计依据（前两轮实测教训）：
- 0.65M（704×928）+ 4 步：单张 ~20 秒；1088×1920 下 4 步会结构漂移。
- **道具比人物容易丢**（上轮海报被吞）→ 弓是这个故事的骨架，每格都显式写它现在在哪、什么状态。
- **景别拉不出去** → 不写 wide shot，第 5 格用「她在右边跑 / 他在左下站着」的左右分区代替。
- **角色串逐字复制**，动作句单起一句、以角色串开头。
- **正向强调**：`the bow is clearly visible` / `her two long ears are clearly visible` 这类句子比禁止句有用，
  因为 4 步没有负向入口。

用法:
    python tools/run_moss_hunter.py            # 5 页全跑
    python tools/run_moss_hunter.py --pages 1,3
    python tools/run_moss_hunter.py --dry
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

SKILL = "moss"                                  # 4 步档
OUT_DIR = "web/实测_moss猎户"
WIDTH, HEIGHT = 704, 928
BASE_SEED = 20261031

# ── 常量串：5 页逐字复制 ──────────────────────────────────────────
STYLE = ("anime style, clean line art, flat cel shading, cold pale blue morning light, "
         "muted color palette, slightly desaturated")

SCENE = ("a misty pine forest in the early morning, thin fog between the tall tree trunks, "
         "fallen brown leaves on the ground")

HUNTER = ("a young hunter with short dark brown hair, a brown linen tunic, a dark leather "
          "vest, a quiver of arrows on his back, brown trousers and leather boots")

RABBIT = ("a rabbit spirit girl with long white hair, two long white rabbit ears standing up "
          "from her head, red eyes, a white kimono with a pale blue sash")

PAGES = [
    {
        "n": 1,
        "title": "拉满弓",
        "act": "复杂手部动作：一手持弓、一手三指扣弦拉到脸颊，瞄准画面右侧",
        "prompt": (
            "%s. %s. %s. "
            "He is kneeling on one knee on the fallen leaves, his left arm fully extended "
            "holding a long wooden bow upright in front of him, his right hand pulled back "
            "with three fingers hooked on the bowstring at his cheek, an arrow nocked and "
            "aiming toward the right edge of the frame. His body is turned sideways, "
            "one eye closed, the other staring along the arrow. "
            "The long wooden bow and the drawn string are clearly visible in silhouette. "
            "Medium shot from the side. No text, no speech bubble."
        ) % (STYLE, SCENE, HUNTER),
    },
    {
        "n": 2,
        "title": "看清了",
        "act": "动作转为静止：弓松下来，箭还在弦上，眼睛睁大、嘴微张",
        "prompt": (
            "%s. %s. %s. "
            "He is still kneeling but has let the bow drop forward, the string no longer "
            "drawn, the arrow resting loosely across the bow. His hands are open and slack. "
            "His eyes are wide open, his lips slightly parted, his whole face frozen in "
            "surprise as he stares toward the right edge of the frame. "
            "The long wooden bow is clearly visible across the lower part of the image. "
            "Medium close shot on his face and shoulders from the side. "
            "No text, no speech bubble."
        ) % (STYLE, SCENE, HUNTER),
    },
    {
        "n": 3,
        "title": "是只兔子精",
        "act": "低角度仰拍（猎户视角）：跪地、双手按胸口、耳朵后压、眼里有泪",
        "prompt": (
            "%s. %s. %s. "
            "She is crouching low on the ground between the roots of a pine tree, both hands "
            "pressed flat against her chest, her two long white ears folded back along her head, "
            "looking up toward the viewer with wide red eyes. Small tears at the corners of "
            "her eyes. Her white kimono is dusty at the hem. "
            "Her two long white rabbit ears are clearly visible against the dark forest. "
            "Medium close shot from a low angle, as if seen by someone standing above her. "
            "No text, no speech bubble."
        ) % (STYLE, SCENE, RABBIT),
    },
    {
        "n": 4,
        "title": "放下弓",
        "act": "两个手部动作同时：一手把弓垂到身侧，另一只手摊开手掌朝向她",
        "prompt": (
            "%s. %s. %s. "
            "He is standing up straight and lowering the long wooden bow down to his side, "
            "holding it loosely by the grip in his left hand, the string hanging slack, "
            "no arrow on it. His right arm is extended toward the lower right of the frame, "
            "the hand open with the palm facing outward, fingers spread, in a calm "
            "stay-there gesture. His expression is soft and serious. "
            "Medium shot, three-quarter view, the lowered bow clearly visible at his side. "
            "No text, no speech bubble."
        ) % (STYLE, SCENE, HUNTER),
    },
    {
        "n": 5,
        "title": "放她走",
        "act": "最难：她在跑、背对镜头但头转回来；他在前景站定目送",
        "prompt": (
            "%s. %s. "
            "On the right side of the frame, %s, she is running away between the foggy tree "
            "trunks with her back to the viewer, but her head is turned back over her shoulder "
            "toward him, her two long white ears streaming behind her, one hand lifted. "
            "On the lower left of the frame, %s, he is standing still with his back partly "
            "to the viewer, the long wooden bow hanging down from his left hand, watching her go. "
            "Medium shot, the two of them separated left and right, fog between them. "
            "No text, no speech bubble."
        ) % (STYLE, SCENE, RABBIT, HUNTER),
    },
]


def build(page, base_wf):
    wf = json.loads(json.dumps(base_wf))
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    seed = BASE_SEED + page["n"]
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False),
                                    page["prompt"], seed), seed


def run(page, base_wf):
    wf, seed = build(page, base_wf)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL,
                                  prompt=page["prompt"][:200],
                                  seed=seed, out_dir=OUT_DIR)
    if job is None:
        print("[第 %d 页] 没进队列：%s" % (page["n"], why))
        return None
    print("[第 %d 页] 入队 %s（seed %d），等出图…" % (page["n"], job.tag, seed), flush=True)
    t0 = time.time()
    try:
        job.wait()
    except Exception as exc:                    # noqa: BLE001
        print("[第 %d 页] 失败：%s（等了 %.0f 秒）" % (page["n"], exc, time.time() - t0))
        return None
    files = image_jobs.output_images(job.entry, job=job)
    print("[第 %d 页] 完成 %.1f 秒 → %s" % (page["n"], time.time() - t0, files), flush=True)
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="", help="逗号分隔的页号，如 1,3,5")
    ap.add_argument("--dry", action="store_true", help="只打印，不入队")
    args = ap.parse_args()

    base_wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not base_wf:
        sys.exit("moss 的 workflow.json 读不出来")

    picked = ([int(x) for x in args.pages.split(",") if x.strip()]
              if args.pages.strip() else [p["n"] for p in PAGES])

    print("画布 %dx%d / steps %s / adapter %s / 共 %d 页"
          % (WIDTH, HEIGHT, base_wf["40"]["inputs"]["steps"],
             base_wf["40"]["inputs"]["adapter"], len(picked)))
    print("种子基准 %d（每页 +1）\n" % BASE_SEED)

    if args.dry:
        for p in PAGES:
            if p["n"] not in picked:
                continue
            wf, seed = build(p, base_wf)
            doc = wf
            text = doc["20"]["inputs"]["text"]
            print("── 第 %d 页 %s（seed %d）" % (p["n"], p["title"], seed))
            print("   %s" % p["act"])
            print("   提示词 %d 字符 / 残留占位符 %s"
                  % (len(text), "__MULTI_PROMPTS__" in text or "__SEED__" in text))
            print()
        return

    done = {}
    for p in PAGES:
        if p["n"] in picked:
            done[p["n"]] = run(p, base_wf)
    print("\n汇总：")
    for p in PAGES:
        if p["n"] in picked:
            print("  第 %d 页 %-8s → %s" % (p["n"], p["title"], done.get(p["n"]) or "没出来"))


if __name__ == "__main__":
    main()

"""moss 4 步档 · 多人（男女同框）角色区分测试：3 格连续分镜。

剧本《掉下来的书》——女生抱书走过书架，书滑落；男生俯身捡起；递给女生。

设计依据（都是实测教训，别随手改）：
1. **0.65M 画布（704×928）+ 4 步 DMD**：2026-10-20 实测这个组合中文气泡零错字、单张 ~15~22 秒；
   1088×1920 下 4 步会结构漂移（第二格场景跑到室内沙发）。
2. **角色串逐字复制**：moss 没有角色参考图，一致性全靠每条提示词里那段描述一字不差。
   所以 GIRL / BOY 抽成常量，三个镜头共用，只改「镜头 + 动作 + 台词」。
3. **特征要粗、要高对比**：4 步出不了雀斑/眼痣这种细节，还会让两角色互相污染。
   这里女=黑长直+藏青发带+白衬衫+百褶裙，男=黑色短发+深灰外套+背包带，发型/服装/有无裙子三重区分。
4. **景别不要硬拉 wide shot**：实测 4 步拉不出去，全出中近景。所以三格都按中/近景设计。
5. **刻字**：moss 4 步无负向入口（BasicGuider 只有 positive），气泡错字只能靠重抽或降分辨率解决。
   这里只放 2 处文字（1 个拟声 + 1 句台词），尽量不赌。

用法:
    python tools/run_moss_shots.py            # 三格全跑
    python tools/run_moss_shots.py --shots 1,2
    python tools/run_moss_shots.py --dry      # 只打印工作流参数，不入队

同 run_moss_pages.py：走 image_jobs 队列，不裸 POST。
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
OUT_DIR = "web/实测_moss多人"
WIDTH, HEIGHT = 704, 928                        # 0.65M，16 的倍数
BASE_SEED = 20261021

# ── 三个常量串：逐字复制，只改镜头/动作/台词 ────────────────────────
STYLE = ("anime style, clean line art, flat cel shading, soft warm afternoon light, "
         "muted color palette")

SCENE = ("two students in a school library aisle between tall wooden bookshelves, "
         "warm sunlight coming from the window on the right, dust motes in the light")

GIRL = ("a girl with long straight black hair reaching her waist, a navy blue ribbon tied "
        "on the left side of her hair, white short-sleeve shirt with a light blue neckerchief, "
        "navy pleated skirt")

BOY = ("a boy with short black hair, dark gray school jacket worn over a white shirt, "
       "a black backpack strap over one shoulder, dark trousers")

SHOTS = [
    {
        "n": 1,
        "title": "一本掉下来",
        "desc": {
            "zh": "女生抱着三本书走过书架，最上面一本滑出手臂，往木地板上落；她低头，眼睛睁大。",
            "camera": "medium shot, three-quarter view",
            "text": "拟声「啪嗒」贴在下落的书旁",
        },
        "prompt": (
            "%s. %s. %s. "
            "She is walking and holding three hardcover books against her chest with both arms. "
            "The topmost book has slipped out of her arms and is falling toward the wooden floor, "
            "a few pages fluttering open in mid-air. "
            "She looks down at the falling book, her eyes wide in surprise, lips slightly parted. "
            "Medium shot, three-quarter view. "
            "A small hand-lettered sound effect next to the falling book reads \"啪嗒\"."
        ) % (STYLE, SCENE, GIRL),
    },
    {
        "n": 2,
        "title": "他弯腰去捡",
        "desc": {
            "zh": "男生的特写：蹲下身，手指已经碰到书脊；书摊开在地板上，封面朝上。",
            "camera": "low angle close shot",
            "text": "无台词（纯动作格）",
        },
        "prompt": (
            "%s. %s. %s. "
            "He is crouching down on one knee and reaching out one hand toward a fallen hardcover "
            "book lying open on the wooden floor, his fingertips just touching its spine. "
            "Low angle close shot on the boy, the book in the foreground. "
            "No speech bubble, no text."
        ) % (STYLE, SCENE, BOY),
    },
    {
        "n": 3,
        "title": "递给她",
        "desc": {
            "zh": "男生站起来，把书递到女生面前；女生伸手去接，两人隔着书架过道相对。",
            "camera": "medium shot, both in profile",
            "text": "男生嘴边小气泡「给你。」",
        },
        "prompt": (
            "%s. %s. %s, and %s. "
            "He is standing and holding the closed book out toward her with one hand. "
            "She is reaching out with both hands to take it. "
            "They face each other across the narrow aisle, both seen in profile, bookshelves on both sides. "
            "Medium shot, both figures in the same frame. "
            "A small speech bubble beside his mouth reads \"给你。\"."
        ) % (STYLE, SCENE, BOY, GIRL),
    },
]


def build(shot, base_wf):
    wf = json.loads(json.dumps(base_wf))
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    seed = BASE_SEED + shot["n"]
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False),
                                    shot["prompt"], seed), seed


def run(shot, base_wf):
    wf, seed = build(shot, base_wf)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL,
                                  prompt=shot["prompt"][:200],
                                  seed=seed, out_dir=OUT_DIR)
    if job is None:
        print("[第 %d 格] 没进队列：%s" % (shot["n"], why))
        return None
    print("[第 %d 格] 入队 %s（seed %d），等出图…" % (shot["n"], job.tag, seed), flush=True)
    t0 = time.time()
    try:
        job.wait()
    except Exception as exc:                    # noqa: BLE001
        print("[第 %d 格] 失败：%s（等了 %.0f 秒）" % (shot["n"], exc, time.time() - t0))
        return None
    files = image_jobs.output_images(job.entry, job=job)
    print("[第 %d 格] 完成 %.1f 秒 → %s" % (shot["n"], time.time() - t0, files), flush=True)
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="", help="逗号分隔的格号，如 1,2,3")
    ap.add_argument("--dry", action="store_true", help="只打印，不入队")
    args = ap.parse_args()

    base_wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not base_wf:
        sys.exit("moss 的 workflow.json 读不出来")

    picked = ([int(x) for x in args.shots.split(",") if x.strip()]
              if args.shots.strip() else [s["n"] for s in SHOTS])

    print("画布 %dx%d / steps %s / adapter %s / 共 %d 格"
          % (WIDTH, HEIGHT, base_wf["40"]["inputs"]["steps"],
             base_wf["40"]["inputs"]["adapter"], len(picked)))
    print("种子基准 %d（每格 +1）\n" % BASE_SEED)

    if args.dry:
        for s in SHOTS:
            if s["n"] not in picked:
                continue
            wf, seed = build(s, base_wf)
            doc = wf                            # apply_prompt_placeholders 已返回 dict
            text = doc["20"]["inputs"]["text"]
            print("── 第 %d 格 %s（seed %d）" % (s["n"], s["title"], seed))
            print("   画布 %sx%s  steps=%s"
                  % (doc["30"]["inputs"]["width"], doc["30"]["inputs"]["height"],
                     doc["40"]["inputs"]["steps"]))
            print("   提示词 %d 字符 / 残留占位符 %s"
                  % (len(text), "__MULTI_PROMPTS__" in text or "__SEED__" in text))
            print("   " + text.replace("\n", " ")[:180] + "…\n")
        return

    done = {}
    for s in SHOTS:
        if s["n"] in picked:
            done[s["n"]] = run(s, base_wf)
    print("\n汇总：")
    for s in SHOTS:
        if s["n"] in picked:
            print("  第 %d 格 %-8s → %s" % (s["n"], s["title"], done.get(s["n"]) or "没出来"))


if __name__ == "__main__":
    main()

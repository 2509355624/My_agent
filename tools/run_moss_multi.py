"""moss 4 步档 · 复杂多人（3~4 人同框）角色区分测试：3 格连续分镜。

剧本《文化祭海报》——放学后的社团教室，四个人做海报。
格1 = 3 人（讲台/黑板协作），格2 = 4 人（围桌看稿），格3 = 3 人（并排看成品）。
**人数故意递增 3→4→3**，目的是找出 4 步 DMD 的角色区分崩溃阈值，而不是只求好看。

设计依据（含上一轮《掉下来的书》的实测教训，别随手改）：
1. **0.65M 画布（704×928）+ 4 步 DMD**：1088×1920 下 4 步会结构漂移。这个组合单张 ~15~40 秒。
2. **角色串逐字复制**：moss 没有角色参考图，一致性全靠每条提示词那段描述一字不差。
   四个角色抽成常量，每格只挑该出场的那几个拼进去，一个字都不改。
3. **特征要粗、要高对比**：4 步出不了雀斑/眼痣，还会互相污染。四个角色靠
   「发色 + 发长 + 外套颜色 + 有无眼镜/裙子」四重区分，尽量两两之间至少差两项：
     GIRL_A 黑长直 + 藏青发带 + 白衬衫 + 百褶裙
     BOY_B  黑短发 + 深灰立领外套 + 背包带 + 深裤
     GIRL_C 橙色波波头 + 圆框眼镜 + 红开衫 + 白裙
     BOY_D  浅金短发 + 蓝色运动外套 + 个子最高
4. **景别不要硬拉 wide shot**：4 步拉不出去，全出中近景。三格都按中景设计。
5. **接触类动词会退化成「指」**（上轮实测：reaching / fingertips touching → 食指戳）。
   要抓握就写 closing his hand around，要说「指」就直接写 pointing at。
6. **刻字尽量少**：moss 4 步无负向入口，气泡错字只能重抽。这里只放 2 处文字。

用法:
    python tools/run_moss_multi.py             # 三格全跑
    python tools/run_moss_multi.py --shots 2   # 只重抽第 2 格
    python tools/run_moss_multi.py --dry       # 只打印工作流参数，不入队

同 run_moss_shots.py：走 image_jobs 队列，不裸 POST。
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
OUT_DIR = "web/实测_moss多人_复杂"
WIDTH, HEIGHT = 704, 928                        # 0.65M，16 的倍数
BASE_SEED = 20261022

# ── 四个角色串：逐字复制，只改「谁出场 + 动作 + 台词」 ──────────────
STYLE = ("anime style, clean line art, flat cel shading, soft late afternoon light, "
         "muted color palette")

SCENE = ("a school club room after class, a long wooden table in the middle, "
         "a blackboard on the back wall, rolled paper and art supplies on the table, "
         "warm sunset light coming through the window on the left, dust motes in the air")

GIRL_A = ("a girl with long straight black hair reaching her waist, a navy blue ribbon tied "
          "on the left side of her hair, white short-sleeve shirt with a light blue neckerchief, "
          "navy pleated skirt")

BOY_B = ("a boy with short black hair, dark gray school jacket worn over a white shirt, "
         "a black backpack strap over one shoulder, dark trousers")

GIRL_C = ("a girl with chin-length curly orange hair, round silver glasses, "
          "a red knitted cardigan over a white dress")

BOY_D = ("a tall boy with messy light blond hair, a blue track jacket with white stripes "
         "on the sleeves, gray trousers, the tallest of the group")

SHOTS = [
    {
        "n": 1,
        "title": "黑板前分工",
        "people": 3,
        "desc": {
            "zh": "三人同框。女生A 站在黑板前举着粉笔写字；男生B 站在她旁边，把一盒彩色粉笔递过去；"
                  "女生C 抱着纸卷从门口走进来。",
            "camera": "medium shot, all three in the same frame",
            "text": "男生B 嘴边小气泡「粉笔给我。」",
        },
        "prompt": (
            "Three people in the same frame. %s. %s. "
            "%s, she is standing in front of the blackboard with one arm raised, "
            "holding a piece of white chalk, writing on the board. "
            "%s, he is standing beside her and holding out a small box of colored chalk to her. "
            "%s, she is walking in from the door on the right, carrying a rolled sheet of paper "
            "under one arm. "
            "Medium shot, all three figures clearly in the same frame, "
            "each of them has a different hair color and a different outfit. "
            "A small speech bubble beside the boy's mouth reads \"粉笔给我。\"."
        ) % (STYLE, SCENE, GIRL_A, BOY_B, GIRL_C),
    },
    {
        "n": 2,
        "title": "围桌看稿",
        "people": 4,
        "desc": {
            "zh": "四人同框，围着长桌看摊开的巨幅海报底稿。女生A、男生B 站在左侧低头看，"
                  "女生C 坐在桌沿，男生D 站在右侧最高，伸手指着纸上某处。",
            "camera": "medium shot from a slight high angle, all four in the same frame",
            "text": "无台词（纯动作格）",
        },
        "prompt": (
            "Four people in the same frame. %s. %s. "
            "On the long wooden table there is a large unfolded poster draft covered with "
            "pencil sketches. "
            "%s, she is standing at the left side of the table, both hands resting on the edge, "
            "looking down at the paper. "
            "%s, he is standing just behind her shoulder, one hand on his chin, also looking down. "
            "%s, she is sitting on the edge of the table on the right, one leg hanging down. "
            "%s, he is standing at the far right side, the tallest one, leaning over the table "
            "and pointing down at one spot on the paper with his index finger. "
            "Medium shot from a slight high angle, all four figures clearly in the same frame, "
            "each of them has a different hair color and a different outfit. "
            "No speech bubble, no text."
        ) % (STYLE, SCENE, GIRL_A, BOY_B, GIRL_C, BOY_D),
    },
    {
        "n": 3,
        "title": "成品贴在墙上",
        "people": 3,
        "desc": {
            "zh": "三人同框近景。海报已经贴在墙上，三人并排站在它前面转头看镜头，"
                  "女生A 微微笑，男生B 双手插兜，女生C 举起手比了个 OK。",
            "camera": "medium close shot, all three side by side",
            "text": "女生C 气泡「搞定了！」",
        },
        "prompt": (
            "Three people in the same frame. %s. %s. "
            "A finished colorful poster is taped on the wall behind them. "
            "%s, she is standing on the left in front of the poster, turning her head toward "
            "the viewer with a small satisfied smile, arms relaxed at her sides. "
            "%s, he is standing in the middle, both hands in his pockets, looking at the viewer "
            "with a calm expression. "
            "%s, she is standing on the right, raising one hand to make an OK sign near her cheek, "
            "grinning at the viewer. "
            "Medium close shot, all three standing side by side, all in the same frame, "
            "each of them has a different hair color and a different outfit. "
            "A small speech bubble above the orange-haired girl's head reads \"搞定了！\"."
        ) % (STYLE, SCENE, GIRL_A, BOY_B, GIRL_C),
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
    print("[第 %d 格] 入队 %s（seed %d，%d 人），等出图…"
          % (shot["n"], job.tag, seed, shot["people"]), flush=True)
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
            print("── 第 %d 格 %s（seed %d，%d 人）" % (s["n"], s["title"], seed, s["people"]))
            print("   画布 %sx%s  steps=%s"
                  % (doc["30"]["inputs"]["width"], doc["30"]["inputs"]["height"],
                     doc["40"]["inputs"]["steps"]))
            print("   提示词 %d 字符 / 残留占位符 %s"
                  % (len(text), "__MULTI_PROMPTS__" in text or "__SEED__" in text))
            print("   " + text.replace("\n", " ")[:200] + "…\n")
        return

    done = {}
    for s in SHOTS:
        if s["n"] in picked:
            done[s["n"]] = run(s, base_wf)
    print("\n汇总：")
    for s in SHOTS:
        if s["n"] in picked:
            print("  第 %d 格 %-10s（%d 人）→ %s"
                  % (s["n"], s["title"], s["people"], done.get(s["n"]) or "没出来"))


if __name__ == "__main__":
    main()

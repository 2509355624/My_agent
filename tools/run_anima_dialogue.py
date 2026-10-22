"""双人对话格：anima 线条出场景 + qwen 在图上写字，一次跑完。

这是给「双人场景台词怎么和标签 / 场景对上」这件事扫障碍用的**离线测试台**，
不进产品链路：不写 state/、不动漫画任务、只往 `dist/anima_dialogue/` 落图。

为什么单独测：产品的阶段一（anima-line 出场景）和阶段二（qwen_image_v1 加
气泡）一直是给 **POV 单人**调的——气泡只有「我方」和「画面里的她」两种归属。
第三人称双人不一样：画面里两个真人各有台词，气泡的**指向**必须一人一根，
指错了就是"男生的嘴长在女生身上"。这类错在分镜稿里看不出来，只能出图看。

提示词**不另写一份**，直接调 `comic._bubble_sec` / `_layout` / `BUBBLE_TEMPLATE_ALL`
——测的必须是产品真实文案，否则测完还要再改一遍产品。

台词写了两句**强依赖画面**的：男生撑伞把人往伞下带，女生嘴上嫌近。画面里
要是没伞、没雨、两人没贴在一起，这一格就是错的——这样"台词和场景符不符"
一眼能看出来，不用拿分镜稿对。

用法:
    # 只看提示词，不出图
    python tools/run_anima_dialogue.py --dry

    # 出场景图 → 加两句台词（默认两步都跑）
    python tools/run_anima_dialogue.py

    # 只重跑第二步，拿上一次的场景图（省掉 40~90 秒）
    python tools/run_anima_dialogue.py --scene dist/anima_dialogue/<时间戳>/01_scene.png

    # 换一句台词试试（两个 -- 参数各给一句）
    python tools/run_anima_dialogue.py --boy "别淋湿了" --girl "知道了啦"

出图: dist/anima_dialogue/<时间戳>/01_scene.png + 02_bubble.png + manifest.json
"""

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import comic, comic_render_opts, image_jobs  # noqa: E402
from app.tools.normal import generate_image  # noqa: E402

SCENE_SKILL = comic.SCENE_SKILL        # anima-line，和漫画场景同一渠道
BUBBLE_SKILL = comic.BUBBLE_SKILL      # qwen_image_v1，和漫画气泡同一渠道
OUT_ROOT = os.path.join(ROOT, "dist", "anima_dialogue")

# ── 这一格的场景 ──────────────────────────────────────────
# 第三人称双人：镜头是**旁观者**，画面里男女生都在，没有 pov / pov_hands。
# 人物锚只写不随场景变的属性（发色 / 瞳色 / 衣服），动作和道具写在这一格
# 自己的尾巴里——固定串里塞动作，下一格换个场景就会跟着跑。
BOY = "black_hair, short_hair, brown_eyes, grey_jacket, black_pants"
GIRL = "black_hair, long_hair, brown_eyes, white_shirt, pleated_skirt"
SCENE = (
    "masterpiece, best quality, absurdres, safe, "
    "1boy, 1girl, "
    + BOY + ", " + GIRL + ", "
    "two people facing each other, standing close, "
    "boy holding umbrella, sharing umbrella, rain, night, "
    "convenience store, awning, wet street, "
    "from side, upper body, looking at each other, "
    "cinematic lighting, depth of field"
)

# 台词：两句都**指着画面里的东西说**（伞 / 距离），场景不对立刻露馅。
# 每条控制在 10 字以内——qwen 的中文在 11 字以内才零错字。
BOY_LINE = "站过来点，别淋到"
GIRL_LINE = "已经很近了呀"


def load_opts():
    """anima-line 的当前默认渲染参数（漫画台「渲染参数」卡存的那份）。"""
    try:
        return comic_render_opts.load_defaults(SCENE_SKILL)
    except Exception as exc:                       # 没有默认就吃模板原值
        print("⚠️ 读不到默认渲染参数（%s），用模板原值" % exc)
        return None


def bubble_prompt(boy_line, girl_line, positional=False):
    """两句话的提示词。`positional=False`（默认）走**按人绑定**的写法。

    为什么默认不用 `comic._bubble_sec`：它用「左上方 / 右上角」描述落位，而
    双人图里谁在左是不确定的（模型自己排的），位置跟人对不上就是"男生的嘴长
    在女生身上"——2026-10-22 实测，`_bubble_sec` 那种「左上方…指向画面中的
    男生」写出来，两个尾巴**全部连给了位置最近的人**，两句台词全归错人。

    qwen 认人、不认"我们嘴里的左上"：直接把**归属和台词绑在一起**说
    「尾巴指向男生的那个气泡里写…」，它自己会把人找出来再决定气泡放哪。
    位置根本不提，也就没有左右可指错。
    """
    if positional:                       # 老写法，留着做 A/B 对照
        pos1, pos2 = comic._layout(["男", "女"], False)  # noqa: SLF001
        return comic.BUBBLE_TEMPLATE_ALL.format(
            sec1=comic._bubble_sec(pos1, "男", boy_line, intro=False),
            sec2=comic._bubble_sec(pos2, "女", girl_line, intro=False),
            lab_sec=comic._count_note(2, pos2, 0, pos1),
        )
    boy = comic._clean_dialogue(boy_line)  # noqa: SLF001
    girl = comic._clean_dialogue(girl_line)  # noqa: SLF001
    return (
        "在这张漫画格里添加两个对话框（气泡）。"
        '尾巴指向画面中男生的那个气泡里写 "%s"。'
        '尾巴指向画面中女生的那个气泡里写 "%s"。'
        "两个气泡都是白色圆角框、黑色细边框，字体为黑体，"
        "文字排成 2 到 3 行居中显示，每行不超过 8 个字，字迹清晰。"
        "画面里的文字一共这两处：指向男生的那个气泡、指向女生的那个气泡。"
        "两个气泡都放在人物脸部以外的空白处，"
        "人物、服装、姿势、背景和画风保持不变。"
        % (boy, girl)
    )


def render_scene(out_dir, opts, seed=None):
    seed = seed if seed is not None else comic._new_seed()  # noqa: SLF001
    wf = generate_image.build_t2i_workflow(SCENE_SKILL, SCENE, seed)
    if wf is None:
        raise RuntimeError("渠道 %s 没有文生图工作流" % SCENE_SKILL)
    if opts:
        wf = comic_render_opts.apply(wf, opts)
    t0 = time.time()
    name = comic._submit_retry(                    # noqa: SLF001
        lambda: comic._submit(wf, SCENE_SKILL, SCENE, seed, out_dir=out_dir),  # noqa: SLF001
        "[场景]")
    dst = os.path.join(out_dir, "01_scene.png")
    comic._download(name, dst)                     # noqa: SLF001
    return {"prompt": SCENE, "seed": seed,
            "seconds": round(time.time() - t0, 1), "file": dst}


def render_bubble(src, out_dir, boy_line, girl_line, seed=None, positional=False,
                  out_name="02_bubble.png"):
    prompt = bubble_prompt(boy_line, girl_line, positional=positional)
    seed = seed if seed is not None else comic._new_seed()  # noqa: SLF001
    uploaded = comic._upload_source(src)                    # noqa: SLF001
    wf = comic._build_i2i(BUBBLE_SKILL, prompt, seed, uploaded)  # noqa: SLF001
    t0 = time.time()
    name = comic._submit_retry(                    # noqa: SLF001
        lambda: comic._submit(wf, BUBBLE_SKILL, prompt, seed, out_dir=out_dir),  # noqa: SLF001
        "[气泡]")
    # ⚠️ 必须用 out_name 拼路径：上面那个 `name` 是 ComfyUI 返回的整串（自带
    # subfolder），拿它 join 会被 os.path.join 当成绝对路径整段替换掉，图就
    # 落到 `dist/<项目绝对路径>/` 那种怪目录里（10-22 踩过一次）。
    dst = os.path.join(out_dir, out_name)
    comic._download(name, dst)                     # noqa: SLF001
    return {"prompt": prompt, "seed": seed, "boy_line": boy_line,
            "girl_line": girl_line,
            "seconds": round(time.time() - t0, 1), "file": dst}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打提示词，不出图")
    ap.add_argument("--scene", default="",
                    help="复用已有的场景图（跳过第一步）")
    ap.add_argument("--boy", default=BOY_LINE, help="男生台词")
    ap.add_argument("--girl", default=GIRL_LINE, help="女生台词")
    ap.add_argument("--seed", type=int, default=None, help="场景图固定种子")
    ap.add_argument("--bubble-seed", type=int, default=None, help="加字固定种子")
    ap.add_argument("--positional", action="store_true",
                    help="用老写法（左上方/右上角 + 指向某人）做 A/B 对照")
    args = ap.parse_args()

    prompt = bubble_prompt(args.boy, args.girl, positional=args.positional)
    if args.dry:
        print("=" * 70)
        print("【步骤一 · 场景】%s" % SCENE_SKILL)
        print(SCENE)
        print("=" * 70)
        print("【步骤二 · 加字】%s" % BUBBLE_SKILL)
        print(prompt)
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(OUT_ROOT, stamp)
    os.makedirs(out_dir, exist_ok=True)

    man = {"created": stamp, "scene_skill": SCENE_SKILL,
           "bubble_skill": BUBBLE_SKILL}

    if args.scene:
        scene_file = os.path.abspath(args.scene)
        if not os.path.isfile(scene_file):
            raise SystemExit("找不到场景图：%s" % scene_file)
        man["scene"] = {"file": scene_file, "reused": True}
        print("[1/2] 复用场景图 %s" % scene_file)
    else:
        opts = load_opts()
        if opts:
            print("[参数] %s" % comic_render_opts.describe(opts))
        print("[1/2] anima-line 出场景…（808×1024 两段采样，约 40~90 秒）")
        man["scene"] = render_scene(out_dir, opts, seed=args.seed)
        print("      → %s（%.1f 秒）" % (man["scene"]["file"],
                                       man["scene"]["seconds"]))
        scene_file = man["scene"]["file"]

    print("[2/2] qwen 图上写两句台词…（约 30~90 秒）")
    out_name = "02_bubble_pos.png" if args.positional else "02_bubble.png"
    man["bubble"] = render_bubble(scene_file, out_dir, args.boy, args.girl,
                                  seed=args.bubble_seed,
                                  positional=args.positional, out_name=out_name)
    print("      → %s（%.1f 秒）" % (man["bubble"]["file"],
                                   man["bubble"]["seconds"]))

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    print("\n落盘目录：%s" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""POV 手势探针：观众的手 vs 角色自己的手，能不能分得清。

这是给「第一人称 POV 漫画」扫障碍用的**离线测试台**，不进产品链路：
不写 state/、不动漫画任务、只往 `dist/pov_hands/` 落图。

为什么单独测手：POV 漫画里「我」只有手在场，所有亲密动作都靠两组手完成
（牵手 / 系鞋带 / 捧脸 / 递物）。模型最容易犯的错不是画得丑，而是**手的归属
错了**——我们的手画成她的手、她该空的另一只手又冒出来一只、四只手变五只。
这类错在分镜稿里看不出来，只能靠同参数批量出图盯着数。

参数（2026-10-22 用户指定，来自漫画台「渲染参数」卡）：
    6 槽 LoRA / 底图 10 步 er_sde / 高清 10 步 euler denoise .25 /
    808×1024 / latent 放大 1.1 / 2x_Ani4Kv2 最终放大
    → 每 5 张重启一次 ComfyUI（用户：这套参数 5 张一重启）

用法:
    # 只核对标签是否存在，不出图
    python tools/run_pov_hands.py --check-tags

    # 第一批 5 个核心场景（先跑这个）
    python tools/run_pov_hands.py --batch 1

    # 第二批 8 个进阶场景
    python tools/run_pov_hands.py --batch 2

    # 单跑某几个（编号见 --list）
    python tools/run_pov_hands.py --only 3,7

    # 固定种子复现同一张
    python tools/run_pov_hands.py --only 1 --seed 12345

出图: dist/pov_hands/<时间戳>_b<批次>/NN_<key>.png + manifest.json
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

SKILL = comic.SCENE_SKILL          # anima-line，和漫画场景同一渠道
DEFAULTS = os.path.join(ROOT, "state", "comic_render_defaults.json")
TAGS_TSV = os.path.join(ROOT, "skills", "anima-tags", "data", "tags.tsv")
OUT_ROOT = os.path.join(ROOT, "dist", "pov_hands")

# 统一的人物锚：手部测试不需要换装，同一套外观才能横向比
GIRL = "black_hair, long_hair, brown_eyes, white_shirt, pleated_skirt"

# ── 场景表 ────────────────────────────────────────────────
# point = 这张要验什么；期望 = 数手时的参考答案（观众 2 只 + 她 2 只）
SCENARIOS = [
    # 第一批：最常用 + 最容易翻车的五个
    dict(id=1, key="hold_hand_lookback", batch=1, name="回头牵手",
         point="两组手交握时，谁的手连在谁的手腕上",
         expect="观众双手在画面下缘握住她的右手；她的左手不出现",
         tags="from_behind, turning_head, looking_back, holding_hands, "
              "interlocked_fingers, walking, street, sunset, depth_of_field"),
    dict(id=2, key="tie_shoelace", batch=1, name="给她系鞋带",
         point="观众的手落在她的鞋上，两者是否真的相对（手在鞋面、鞋在脚下）",
         expect="观众双手在她鞋面上；她的双手放在自己膝上",
         tags="sitting, leaning_forward, looking_down, shoes, shoelaces, "
              "hands_on_lap, indoors, wooden_floor"),
    dict(id=3, key="cup_face", batch=1, name="捧她的脸",
         point="双组手同框的极限：观众两只手占满她脸颊，她的手该收起来",
         expect="观众双手贴在她两颊；她不出现第三只手",
         tags="hand_on_another's_cheek, upper_body, looking_at_viewer, blush, "
              "close-up, light_rays"),
    dict(id=4, key="offer_cup", batch=1, name="她递水、我们接",
         point="传物时两只手同时抓同一件东西，杯子会不会长出第三只手",
         expect="她一只手拿杯伸向镜头，观众一只手接；杯上不超过两只手",
         tags="holding_cup, cup, reaching_towards_viewer, looking_at_viewer, "
              "upper_body, indoors, backlighting"),
    dict(id=5, key="pat_head", batch=1, name="摸她的头（对照项）",
         point="最基础的「我们的手在她身上」，应该最容易过；过不了说明底层就废",
         expect="观众一只手在她头顶，她的双手垂在身侧",
         tags="hand_on_another's_head, looking_up, smile, upper_body, indoors"),

    # 第二批：进阶 / 反向 / 精细操作
    dict(id=6, key="hug_from_behind", batch=2, name="背后环抱",
         point="观众双手出现在她腰前，与她自己的手叠在同一区域",
         expect="观众双手在她腹前，她的手压在我们手背上或抬着",
         tags="hug_from_behind, arm_around_another's_waist, from_behind, "
              "upper_body, indoors, backlighting"),
    dict(id=7, key="she_grab_wrist", batch=2, name="她抓我们的手腕",
         point="主动性在她：她的手是施动方，我们的手腕是被抓对象",
         expect="她一只手抓住画面下缘我们的手腕；我们的另一只手不出现",
         tags="holding_another's_wrist, looking_at_viewer, walking, "
              "upper_body, indoors"),
    dict(id=8, key="wipe_tears", batch=2, name="替她擦眼泪",
         point="拇指贴脸这种小接触点，手容易糊成一团",
         expect="观众一只手在她眼角；她的双手不出现或抓着我们的手",
         tags="wiping_tears, hand_on_another's_cheek, crying, close-up, "
              "light_rays"),
    dict(id=9, key="tuck_hair", batch=2, name="替她别头发",
         point="手在耳侧、头发在手指间，细节最密的一类",
         expect="观众一只手在她耳后；她的双手垂着",
         tags="tucking_hair, hand_on_another's_ear, from_side, upper_body, "
              "indoors, light_rays"),
    dict(id=10, key="tie_scarf", batch=2, name="替她整理衣领/围巾",
         point="手在她锁骨一带，容易把她的手也吸过来一起动",
         expect="观众双手在她领口；她的双手垂在身侧",
         tags="adjusting_clothes, scarf, upper_body, looking_at_viewer, "
              "indoors, light_rays"),
    dict(id=11, key="pull_up", batch=2, name="拉她起身",
         point="两手对握 + 受力方向：谁拉谁、手臂朝哪边发力",
         expect="观众一只手握住她的手往上带；她另一只手撑地",
         tags="holding_hands, sitting, looking_up, reaching_towards_viewer, "
              "full_body, wooden_floor, indoors"),
    dict(id=12, key="ring", batch=2, name="给她戴戒指",
         point="最精细的一类：手指套进戒指，手指数目一错就完",
         expect="观众两只手指捏着戒指，套在她伸出的手指上；戒指只有一枚",
         tags="ring, hand_on_another's_hand, jewelry, close-up, light_rays"),
    dict(id=13, key="hand_on_hand_table", batch=2, name="桌上覆手",
         point="两双手上下叠放，最容易长出一只多余的手",
         expect="观众一只手在桌上，她的手覆在我们手背上；共两只手",
         tags="pov_across_table, hand_on_another's_hand, sitting, table, cafe, "
              "looking_at_viewer, upper_body"),
]


# ── 参数 ─────────────────────────────────────────────────

def build_opts(scale_by=1.1, size=None):
    """用户截图那套参数。1.1 来自截图（全局默认里是 1.2）。"""
    opts = {}
    if os.path.isfile(DEFAULTS):
        with open(DEFAULTS, "r", encoding="utf-8") as f:
            opts = json.load(f)
    opts.setdefault("base", {})
    opts.setdefault("hires", {})
    opts.setdefault("upscale", {})
    opts["upscale"]["scale_by"] = float(scale_by)
    opts.setdefault("width", 808)
    opts.setdefault("height", 1024)
    if size:
        w, h = size.lower().split("x")
        opts["width"], opts["height"] = int(w), int(h)
    return opts


def _tag_set():
    s = set()
    if not os.path.isfile(TAGS_TSV):
        return None
    with open(TAGS_TSV, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            name = line.split("\t", 1)[0].strip()
            if name:
                s.add(name)
    return s


def check_tags(verbose=True):
    """只检测、不拦截：缺哪个标签报出来，照样能跑（模型可能认得近义词）。"""
    known = _tag_set()
    if known is None:
        print("⚠️ 找不到标签库：%s" % TAGS_TSV)
        return []
    bad = []
    for sc in SCENARIOS:
        tags = [t.strip() for t in full_prompt(sc).split(",") if t.strip()]
        miss = [t for t in tags if t not in known and t != "safe"]
        if miss:
            bad.append((sc["id"], sc["key"], miss))
            if verbose:
                print("[%02d %-18s] 库中没有：%s" % (sc["id"], sc["key"],
                                                  "、".join(miss)))
    if verbose and not bad:
        print("✓ 13 个场景的标签全部在库里")
    return bad


def full_prompt(sc):
    return "safe, 1girl, solo_focus, 1other, pov, pov_hands, %s, %s" % (
        GIRL, sc["tags"])


# ── 出图 ─────────────────────────────────────────────────

def render_one(sc, out_dir, opts, seed=None, timeout_hint=None):
    prompt = full_prompt(sc)
    seed = seed if seed is not None else comic._new_seed()
    wf = generate_image.build_t2i_workflow(SKILL, prompt, seed)
    if wf is None:
        raise RuntimeError("渠道 %s 没有文生图工作流" % SKILL)
    wf = comic_render_opts.apply(wf, opts)
    t0 = time.time()
    name = comic._submit_retry(
        lambda: comic._submit(wf, SKILL, prompt, seed, out_dir=out_dir),
        "[%02d %s]" % (sc["id"], sc["key"]))
    dst = os.path.join(OUT_ROOT, os.path.basename(out_dir),
                       "%02d_%s.png" % (sc["id"], sc["key"]))
    comic._download(name, dst)
    secs = time.time() - t0
    return {"no": sc["id"], "key": sc["key"], "name": sc["name"],
            "point": sc["point"], "expect": sc["expect"],
            "prompt": prompt, "seed": seed,
            "seconds": round(secs, 1), "file": dst}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="1", choices=["1", "2", "all"])
    ap.add_argument("--only", default="", help="编号，逗号分隔，如 3,7")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--size", default="", help="如 808x1024")
    ap.add_argument("--scale", type=float, default=1.1, help="latent 放大倍率")
    ap.add_argument("--restart-after", type=int, default=5,
                    help="每出几张重启一次 ComfyUI（0 = 不主动重启）")
    ap.add_argument("--list", action="store_true", help="列出场景就退出")
    ap.add_argument("--check-tags", action="store_true", help="只核对标签")
    ap.add_argument("--dry", action="store_true", help="只打印提示词，不出图")
    args = ap.parse_args()

    if args.list or args.check_tags:
        for sc in SCENARIOS:
            print("[%02d] batch%d %-18s %s" % (sc["id"], sc["batch"],
                                              sc["key"], sc["name"]))
            if args.check_tags:
                print("     %s" % full_prompt(sc))
        check_tags()
        return

    todo = SCENARIOS
    if args.only:
        want = {int(x) for x in args.only.replace("，", ",").split(",") if x.strip()}
        todo = [s for s in SCENARIOS if s["id"] in want]
    elif args.batch != "all":
        todo = [s for s in SCENARIOS if s["batch"] == int(args.batch)]
    if not todo:
        print("没有要跑的场景")
        return

    if args.dry:
        for sc in todo:
            print("[%02d] %s\n%s\n" % (sc["id"], sc["name"], full_prompt(sc)))
        return

    check_tags(verbose=True)
    opts = build_opts(scale_by=args.scale, size=args.size or None)
    print("参数：%dx%d / latent ×%.2f / %s" % (
        opts["width"], opts["height"], opts["upscale"]["scale_by"],
        comic_render_opts.describe(opts)))

    stamp = time.strftime("%Y%m%d_%H%M")
    tag = "b%s" % (args.batch if not args.only else "sel")
    out_dir = "web/pov手势_%s_%s" % (stamp, tag)
    os.makedirs(os.path.join(OUT_ROOT, os.path.basename(out_dir)), exist_ok=True)
    print("出图目录：dist/pov_hands/%s/" % os.path.basename(out_dir))

    comic._require_comfy()
    manifest = []
    ok = 0
    for i, sc in enumerate(todo, 1):
        print("\n[%d/%d] %02d %s —— %s" % (i, len(todo), sc["id"],
                                          sc["name"], sc["point"]))
        try:
            rec = render_one(sc, out_dir, opts, seed=args.seed)
            ok += 1
            print("    ✓ %.1f 秒  seed %s  → %s" % (rec["seconds"], rec["seed"],
                                                   os.path.basename(rec["file"])))
        except Exception as exc:                       # 单张失败不拖垮整批
            rec = {"no": sc["id"], "key": sc["key"], "name": sc["name"],
                   "prompt": full_prompt(sc), "error": str(exc)}
            print("    ✗ 失败：%s" % exc)
        manifest.append(rec)
        if args.restart_after and i % args.restart_after == 0 and i < len(todo):
            print("    —— 已出 %d 张，重启 ComfyUI ——" % args.restart_after)
            image_jobs._restart_comfy()

    path = os.path.join(OUT_ROOT, os.path.basename(out_dir), "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"opts": opts, "time": stamp, "items": manifest},
                  f, ensure_ascii=False, indent=1)
    print("\n完成 %d/%d，账本：%s" % (ok, len(todo), path))


if __name__ == "__main__":
    main()

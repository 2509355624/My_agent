"""silver 同一提示词 × 不同种子：查「边缘锯齿 / 割裂感」。

用户 2026-10-22 报：silver 出来的图在**轮廓边缘**（裙摆、衣摆）有很硬的锯齿，
像被量化过一道，不是正常抗锯齿的柔和过渡。同一提示词换种子还出现不同的
毛刺形态，所以先跑一批同词不同种子的图，排除「单个种子的偶然」。

这是离线测试台，不进产品链路：
- 走**同一个** `generate_image.build_t2i_workflow` + `image_jobs.enqueue`，
  和 QQ / 网页点「silver」完全同一条路（裸 POST /prompt 会绕开队列与重启策略，
  测出来的东西和线上不是一回事）。
- 图落 `dist/silver_edge/`，不动漫画任务、不写 state/。

用法:
    python tools/run_silver_edge.py                # 跑 5 张
    python tools/run_silver_edge.py --seeds 1,2,3  # 指定种子
    python tools/run_silver_edge.py --skill silver-hd

出图: dist/silver_edge/<渠道>_<seed>.png + manifest.json
"""

import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import image_jobs  # noqa: E402
from app.config import COMFYUI_OUTPUT_DIR  # noqa: E402
from app.tools.normal import generate_image  # noqa: E402

OUT_ROOT = os.path.join(ROOT, "dist", "silver_edge")

# 用户给的那条提示词，一字不改 —— 测的必须是他实际遇到问题的那条。
PROMPT = (
    "1girl, solo, full body, standing, front view, brown hat, boater hat, "
    "black hair, long hair, hair over shoulder, braid, bangs, green eyes, smile, "
    "closed mouth, white blouse, long sleeves, puffy sleeves, ribbon, "
    "brown overalls, suspenders, buttons, plaid skirt, long skirt, brown skirt, "
    "white socks, black shoes, mary janes, hands together, "
    "white background, simple background"
)

DEFAULT_SEEDS = [111111, 222222, 333333, 444444, 555555]


def apply_variant(wf, variant):
    """改 `LatentUpscaleBy` 的插值与倍率，用来做 A/B（只改内存里的 dict）。

    silver 骨架是：1024×1536 → 12 步 → **LatentUpscaleBy × 1.2** → 8 步
    denoise 0.25 refine → VAE → **2× 放大模型**。

    这里有两个独立的嫌疑：
    - `nearest-exact`：最近邻，直接复制像素块，边缘天生带台阶；
    - `scale_by=1.2`：**latent 是 1/8 分辨率，×1.2 会让 128 → 153.6**，取整成
      154 之后每个源像素映射到的目标宽度在 1~2 之间抖动 → 台阶高度不均匀，
      这就是「有的地方一格、有的地方两格」的割裂感来源。×1.25 是整数
      （128→160、192→240），网格完全对齐。

    variant: base（原样）| bilinear | bicubic | bislerp | area | scale125 | nolatent

    ComfyUI 的 `LatentUpscaleBy` 只认这五种插值（`/object_info` 实测）：
    `nearest-exact` / `bilinear` / `area` / `bicubic` / `bislerp`。
    `bislerp` 是 ComfyUI 专门给 latent 写的球面插值，社区做 latent 放大时的
    首选，所以 A/B 必须把它一起测，不能只比 bilinear/bicubic。
    """
    wf = json.loads(json.dumps(wf))          # 深拷贝，绝不碰模板
    ids = [k for k, v in wf.items()
           if str((v or {}).get("class_type")) == "LatentUpscaleBy"]
    if variant == "nolatent":
        # 直接删掉这一步：把 refine 的 latent 接回第一段的产物
        for nid in ids:
            src = (wf[nid].get("inputs") or {}).get("samples")
            del wf[nid]
            for nd in wf.values():
                ins = (nd or {}).get("inputs") or {}
                for k, v in list(ins.items()):
                    if isinstance(v, list) and v and v[0] == nid:
                        ins[k] = src
        return wf
    for nid in ids:
        ins = wf[nid].setdefault("inputs", {})
        if variant in ("bilinear", "bicubic", "bislerp", "area",
                       "nearest-exact"):
            ins["upscale_method"] = variant
        elif variant == "scale125":
            ins["scale_by"] = 1.25
        elif variant in ("base", "", None):
            pass
        else:
            raise ValueError("未知变体：%s" % variant)
    return wf


def _no_forced_restart():
    """关掉「连画 N 张就整机重启 ComfyUI」这两条。

    `COMFY_RELEASE_AFTER_TOTAL` / `COMFY_RELEASE_AFTER_SAME` 在生产里是防显存
    残留叠爆的，但它们会**重启用户正在用的 ComfyUI**（60~90 秒，还会掐断 QQ
    那边排队中的任务）。这个测试台只跑五张，不值得为它掐断线上服务。
    低显存 / 低内存那两条保护（会打 /free 或重启）**不关**——真到那一步说明
    再画就要崩了。
    """
    image_jobs.COMFY_RELEASE_AFTER_TOTAL = 0
    image_jobs.COMFY_RELEASE_AFTER_SAME = 0


def _local_path(rel):
    """`output_images()` 给的是相对路径，拼回本地盘上的绝对路径。"""
    root = (COMFYUI_OUTPUT_DIR or "").strip()
    if not root:
        return ""
    return os.path.join(root, rel.replace("/", os.sep))


def run_one(skill, seed, tag, variant="base"):
    wf = generate_image.build_t2i_workflow(skill, PROMPT, seed)
    if not wf:
        raise RuntimeError("渠道 %s 没有可用的 workflow.json" % skill)
    wf = apply_variant(wf, variant)
    job, reason = image_jobs.enqueue(
        None, "silver-edge-%s" % tag, wf, skill=skill,
        prompt=PROMPT, seed=seed, quiet=True,
        out_dir="dist_silver_edge")
    if job is None:
        raise RuntimeError("入队被拒：%s" % (reason or "未知原因"))
    t0 = time.time()
    entry = job.wait()
    rels = image_jobs.output_images(entry, job)
    return {"seed": seed, "variant": variant, "elapsed": round(time.time() - t0, 1),
            "files": [_local_path(r) for r in rels], "rels": rels}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default="silver")
    ap.add_argument("--seeds", default=",".join(str(s) for s in DEFAULT_SEEDS))
    ap.add_argument("--variant", default="base",
                    choices=["base", "nearest-exact", "bilinear", "bicubic",
                             "bislerp", "area", "scale125", "nolatent"],
                    help="LatentUpscaleBy 的 A/B：插值算法 / 整数倍率 / 去掉这一步")
    ap.add_argument("--dry", action="store_true", help="只打印工作流摘要，不出图")
    args = ap.parse_args()

    seeds = [int(x) for x in args.seeds.replace("，", ",").split(",") if x.strip()]
    os.makedirs(OUT_ROOT, exist_ok=True)

    if args.dry:
        wf = apply_variant(
            generate_image.build_t2i_workflow(args.skill, PROMPT, seeds[0]),
            args.variant)
        print("渠道:", args.skill, "变体:", args.variant, "节点数:", len(wf or {}))
        for nid, nd in (wf or {}).items():
            if str(nd.get("class_type", "")).startswith(
                    ("KSampler", "EmptyLatent", "LatentUpscale", "ImageUpscale")):
                print(" ", nid, nd.get("class_type"), nd.get("inputs"))
        return

    _no_forced_restart()
    print("渠道 %s，变体 %s，%d 张，输出 %s"
          % (args.skill, args.variant, len(seeds), OUT_ROOT))
    manifest = {"skill": args.skill, "variant": args.variant,
                "prompt": PROMPT, "runs": []}
    for i, seed in enumerate(seeds, 1):
        print("[%d/%d] seed=%s %s ..." % (i, len(seeds), seed, args.variant),
              flush=True)
        try:
            r = run_one(args.skill, seed, "%s_%s_%d" % (args.skill, args.variant, i),
                        variant=args.variant)
        except Exception as exc:
            print("   失败：%s: %s" % (type(exc).__name__, exc), flush=True)
            manifest["runs"].append({"seed": seed, "error": str(exc)})
            continue
        saved = []
        for j, fp in enumerate(r["files"], 1):
            if fp and os.path.isfile(fp):
                dst = os.path.join(
                    OUT_ROOT, "%s_%s_%s%s.png" % (
                        args.skill, args.variant, seed,
                        "" if j == 1 else "_%d" % j))
                shutil.copyfile(fp, dst)
                saved.append(dst)
        r["saved"] = saved
        r.pop("files", None)
        manifest["runs"].append(r)
        print("   完成 %.1fs → %s" % (r["elapsed"],
                                    ", ".join(os.path.basename(x) for x in saved)),
              flush=True)

    with open(os.path.join(OUT_ROOT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    ok = [r for r in manifest["runs"] if r.get("saved")]
    print("完成 %d/%d 张，清单：%s" % (len(ok), len(seeds),
                                  os.path.join(OUT_ROOT, "manifest.json")))


if __name__ == "__main__":
    main()

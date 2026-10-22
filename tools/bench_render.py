"""渲染耗时基准：低分辨率 / 无二次采样 / 少步数，一张到底要多久。

给「POV 漫画要不要为关系镜头单开一条渠道」算账用的**离线测试台**，
不进产品链路：不写 state/、不动漫画任务，只往 `dist/bench/` 落图。

为什么单独测：krea2-general 关系镜头能做对，但原生参数 832×1216 → latent ×2
→ 第二段 2 步 → 2x 放大，出来 3328×4864 / 13.9 MB / 75~110 秒。整本漫画用不起。
要回答的是——**把二次采样和放大全砍掉、只留一次采样，成本能降到多少**。

⚠️ 计时口径：以 ComfyUI 日志的 `Prompt executed in X seconds` 为准。
脚本自己的墙钟会多算进程启动、僵尸锁等待、冷启动，不能拿来当"这张图多久"。

用法:
    # 默认矩阵（krea2-general，7 组）
    python tools/bench_render.py

    # 只看参数不开跑
    python tools/bench_render.py --dry

    # 换渠道 / 换尺寸步数组合
    python tools/bench_render.py --skill anima-line --cases 768x1024:3,768x1024:6

出图: dist/bench/<时间戳>_<渠道>/<尺寸>_<步数>s[_full].png + manifest.json
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import comic, comic_render_opts, image_jobs  # noqa: E402
from app.tools.normal import generate_image  # noqa: E402

COMFY_LOG = r"D:/AI/ComfyUI_v037/user/comfyui_8188.log"
OUT_ROOT = os.path.join(ROOT, "dist", "bench")
SEED = 20261022

# 固定同一句：场景 01「回头牵手」（前几轮 13 场景里最有代表性的关系镜头）。
# krea2 系要求质量词开头，这是它 SKILL.md 的硬性写法，不算对照变量。
PROMPT = ("masterpiece, best quality, ultra-detailed, absurdres, "
          "safe, 1girl, solo_focus, 1other, pov, pov_hands, "
          "black_hair, long_hair, brown_eyes, white_shirt, pleated_skirt, "
          "turning_head, looking_back, holding_hands, interlocked_fingers, "
          "from_side, upper_body, light_rays, warm_lighting")

# (标签, 宽, 高, base 步数, 是否保留二次采样)
CASES = [
    ("512x768_3s",        512,  768, 3, False),
    ("512x768_6s",        512,  768, 6, False),
    ("640x960_3s",        640,  960, 3, False),
    ("640x960_6s",        640,  960, 6, False),
    ("832x1216_3s",       832, 1216, 3, False),
    ("832x1216_6s",       832, 1216, 6, False),
    ("832x1216_8s_full",  832, 1216, 8, True),   # 基准：模板原样（含二次采样 + 放大）
]


def strip_second_pass(wf):
    """砍掉第二段采样：把消费 hires 采样器输出的节点改吃 base 采样器输出。

    节点**留着不删**——ComfyUI 只执行输出可达的节点，没人引用的那段不会跑
    （`comic_render_opts._apply_upscale` 的 final_skip 用的也是这招）。
    删节点反而要扫全部引用，漏一处整条链就断。
    """
    passes = comic_render_opts._passes(wf)  # noqa: SLF001
    base = passes["base"][0]
    hires = passes["hires"][0] if passes["hires"] else None
    if not hires:
        return 0
    n = 0
    for nd in wf.values():
        inp = comic_render_opts._inputs(nd)  # noqa: SLF001
        for k, v in list(inp.items()):
            if isinstance(v, list) and v and str(v[0]) == hires:
                inp[k] = [base, v[1] if len(v) > 1 else 0]
                n += 1
    return n


def _log_size():
    return 0          # 占位：偏移量取数已废弃，见 _exec_seconds
def _exec_seconds(since):
    """这张图**真正执行**了多久。先看日志，日志拿不到就退回 history。

    两条路都试一遍，是因为各有各的坑：

    - 日志：ComfyUI 重启会**重写**日志文件（实测一轮 7 张里丢了 3 个点）。
      所以不能按文件偏移量取增量，只能取最后一条并用时间戳确认是本次的。
    - history：`execution_start` / `execution_success` 两条消息自带毫秒时间戳，
      差值就是纯执行时长；但 ComfyUI 一重启 history 就清空，重启前的拿不到。

    → 先日志（ComfyUI 自己算的 `Prompt executed`，口径最正），拿不到再退 history。
    """
    try:
        with open(COMFY_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 40000))
            raw = f.read().decode("utf-8", "replace")
        hits = re.findall(
            r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.\d]*\] Prompt executed in ([\d.]+) seconds",
            raw)
        if hits:
            ts, sec = hits[-1]
            t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            if t >= since - 5:      # 早于本张提交 → 是上一张的记录
                return float(sec), "log"
    except (OSError, ValueError, OverflowError):
        pass
    return _history_seconds(since), "history"


def _history_seconds(since):
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open("http://127.0.0.1:8188/history?max_items=4", timeout=10) as r:
            hist = json.load(r)
    except Exception:  # noqa: BLE001
        return None
    best = None
    for entry in hist.values():
        t0 = t1 = None
        for m in ((entry.get("status") or {}).get("messages") or []):
            d = m[1] if len(m) > 1 else {}
            if m[0] == "execution_start":
                t0 = d.get("timestamp")
            elif m[0] == "execution_success":
                t1 = d.get("timestamp")
        if not (t0 and t1):
            continue
        if t1 / 1000.0 < since - 5:
            continue
        best = round((t1 - t0) / 1000.0, 2)
    return best


def build(skill, case, seed=SEED):
    tag, w, h, steps, second = case
    wf = generate_image.build_t2i_workflow(skill, PROMPT, seed)
    if wf is None:
        raise RuntimeError("渠道 %s 没有文生图工作流" % skill)
    opts = {"width": w, "height": h, "base": {"steps": steps}}
    if not second:
        # final_skip：SaveImage 直接接 VAEDecode，跳过 2x 放大模型
        opts["upscale"] = {"final_skip": True}
    wf = comic_render_opts.apply(wf, opts)
    cut = 0 if second else strip_second_pass(wf)
    return wf, cut


def _reachable(wf):
    """从保存节点回溯，只有链上的节点会被 ComfyUI 执行。

    「砍掉二次采样」是断引用不是删节点，所以光看模板上还挂着几个 KSampler
    会以为第二段还在跑。真正要证明的是它**不可达**——这个函数就是那个证据。
    """
    seen = set()
    stack = [str(nid) for nid, nd in wf.items()
             if (nd or {}).get("class_type", "").startswith(("SaveImage", "PreviewImage"))]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in wf:
            continue
        seen.add(nid)
        for v in (comic_render_opts._inputs(wf[nid]) or {}).values():  # noqa: SLF001
            if isinstance(v, list) and v:
                stack.append(str(v[0]))
    return seen


def describe_wf(wf):
    """把**真正会执行**的东西念一遍：几段采样、各几步、底图多大、放大跳没跳。"""
    live = _reachable(wf)
    passes = comic_render_opts._passes(wf)  # noqa: SLF001
    bits = []
    for name in ("base", "hires"):
        for nid in passes[name]:
            if str(nid) not in live:
                bits.append("%s 已断链" % name)
                continue
            inp = comic_render_opts._inputs(wf[nid])  # noqa: SLF001
            bits.append("%s %s步 d%s" % (name, inp.get("steps"), inp.get("denoise")))
    size = ""
    for nid in comic_render_opts._latent_ids(wf):  # noqa: SLF001
        inp = comic_render_opts._inputs(wf[nid])  # noqa: SLF001
        if inp.get("width"):
            size = "%sx%s" % (inp.get("width"), inp.get("height"))
            break
    up_final = (comic_render_opts._upscale_nodes(wf) or {}).get("final")  # noqa: SLF001
    up = "跳过" if not up_final or str(up_final) not in live else "2x"
    return "%s ｜ 底图 %s ｜ 最终放大 %s" % (" + ".join(bits), size, up)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default="krea2-general")
    ap.add_argument("--cases", default="",
                    help="覆盖默认矩阵，如 768x1024:3,768x1024:6（宽x高:步数，默认无二次采样）")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="每张之间空等几秒（默认 0；填 60 可看冷启动对计时的影响）")
    ap.add_argument("--repeat", type=int, default=3,
                    help="每个组合跑几次（默认 3：第 1 张当热身，后两张取最快）")
    ap.add_argument("--dry", action="store_true", help="只打印组合与工作流形态")
    args = ap.parse_args()

    cases = list(CASES)
    if args.cases:
        cases = []
        for item in args.cases.split(","):
            item = item.strip()
            if not item:
                continue
            rng, _, st = item.partition(":")
            w, h = rng.lower().split("x")
            steps = int(st or 8)
            cases.append(("%sx%s_%ds" % (w, h, steps), int(w), int(h), steps, False))

    if args.dry:
        for case in cases:
            wf, cut = build(args.skill, case)
            print("[%-20s] %s" % (case[0], describe_wf(wf)))
            if cut:
                print("    └ 砍掉二次采样：改了 %d 处引用" % cut)
        return

    comic._require_comfy()  # noqa: SLF001
    stamp = time.strftime("%Y%m%d_%H%M")
    out_dir = os.path.join(OUT_ROOT, "%s_%s" % (stamp, args.skill))
    os.makedirs(out_dir, exist_ok=True)
    remote = "web/bench_%s_%s" % (stamp, args.skill)
    print("出图目录：dist/bench/%s/" % os.path.basename(out_dir))

    items = []
    runs = [(case, r + 1) for case in cases for r in range(args.repeat)]
    n_seed = 0

    for i, (case, rnd) in enumerate(runs, 1):
        tag = case[0] if args.repeat == 1 else "%s_r%d" % (case[0], rnd)
        # ⚠️ 每张换一个 seed：同 seed 同参数第二次提交会**整个工作流命中缓存**，
        # 实测跑出 0.0 秒 / 墙钟 0.7 秒，把「重复测量」变成「重复复用」。
        seed = SEED + n_seed
        n_seed += 1
        print("\n[%d/%d] %-18s" % (i, len(runs), tag))
        try:
            wf, cut = build(args.skill, case, seed)
            if rnd == 1:
                print("    %s" % describe_wf(wf))
            t_submit = time.time()
            name = comic._submit_retry(  # noqa: SLF001
                lambda: comic._submit(wf, args.skill, PROMPT, seed, out_dir=remote),
                "[%s]" % tag)
            dst = os.path.join(out_dir, "%s.png" % tag)
            comic._download(name, dst)  # noqa: SLF001
            wall = time.time() - t_submit
            ex, src = _exec_seconds(t_submit)
            if ex is None:                  # 日志可能还没 flush，等一下再读一次
                time.sleep(1.0)
                ex, src = _exec_seconds(t_submit)
            from PIL import Image
            with Image.open(dst) as im:
                size = "%dx%d" % im.size
            mb = round(os.path.getsize(dst) / 1024 / 1024, 2)
            print("    → %s ｜ 执行 %s ｜ 墙钟 %.1f 秒 ｜ %.2f MB" % (
                size, ("%.1f 秒(%s)" % (ex, src)) if ex is not None else "没抓到",
                wall, mb))
            items.append({"tag": tag, "base_tag": case[0], "round": rnd,
                          "seed": seed,
                          "width": case[1], "height": case[2],
                          "steps": case[3], "second_pass": case[4],
                          "cut": cut, "out": size, "mb": mb,
                          "exec_seconds": ex, "exec_source": src,
                          "wall_seconds": round(wall, 1),
                          "file": dst})
        except Exception as exc:  # noqa: BLE001
            print("    ✗ %s" % exc)
            items.append({"tag": tag, "base_tag": case[0], "round": rnd,
                          "seed": seed,
                          "width": case[1], "height": case[2],
                          "steps": case[3], "second_pass": case[4],
                          "error": str(exc)})
        if args.seconds:
            time.sleep(args.seconds)

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"skill": args.skill, "prompt": PROMPT, "seed": SEED,
                   "items": items}, f, ensure_ascii=False, indent=1)

    # 汇总：同组合多次取**最快那次**（热态纯执行），墙钟取中位数意义不大——
    # 它混着冷启动和僵尸锁等待，只用来判断"这次是不是被别的东西拖了"。
    print("\n=== 汇总（执行秒数 = ComfyUI 日志口径，同组合取最快）===")
    print("%-18s %-11s %-4s %-10s %8s %8s  %s" % (
        "组合", "出图", "步数", "采样", "执行", "墙钟", "体积"))
    order = []
    for it in items:
        if it["base_tag"] not in order:
            order.append(it["base_tag"])
    for bt in order:
        grp = [x for x in items if x["base_tag"] == bt]
        # 0.x 秒 = 缓存命中，不是真执行；第 1 轮当热身（含模型加载/编译），都不计入
        good = [x for x in grp if (x.get("exec_seconds") or 0) >= 1.0
                and x.get("round") != 1]
        if not good:
            good = [x for x in grp if (x.get("exec_seconds") or 0) >= 1.0]
        bad = [x for x in grp if x.get("error")]
        if not good:
            why = bad[0]["error"] if bad else "执行秒数没抓到"
            print("%-18s 失败 / 无数据：%s" % (bt, why))
            continue
        best = min(good, key=lambda x: x["exec_seconds"])
        print("%-18s %-11s %-4s %-10s %6.1f 秒 %6.1f 秒  %.2f MB" % (
            bt, best["out"], best["steps"],
            "有二次采样" if best["second_pass"] else "单段采样",
            best["exec_seconds"], min(x["wall_seconds"] for x in good), best["mb"]))


if __name__ == "__main__":
    main()

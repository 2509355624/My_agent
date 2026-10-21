"""moss 漫画 · 脚本闭环（一句话 → 编剧 → 逐页出图）。

这是「moss 漫画」模式的**命令行版**，等契约跑稳了再包页面。
它跟 `comic_agent` / `comic_api` 没有调用关系，只共用 `image_jobs` 的出图队列。

用法:
    # 完整一遍：编剧 + 出 4 页
    python tools/run_moss_comic.py --brief "..." --pages 4 --style wuxia

    # 只编剧不出图（先看脚本）
    python tools/run_moss_comic.py --brief "..." --pages 4 --dry

    # 复用已经写好的脚本，只重画第 2 页（换 seed）
    python tools/run_moss_comic.py --reuse state/moss_comic/xxx --only 2 --seed 999

任务目录: state/moss_comic/<时间戳>_<标题>/
    spec.json      结构化脚本（可手改后 --reuse 重画）
    spec_raw.txt   模型原始回复（排查用）
    prompts.txt    拼装后的每页提示词
出图目录: web/moss漫画/<同一个目录名>/
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import image_jobs, moss_comic             # noqa: E402
from app.skills import load_workflow              # noqa: E402
from app.tools.normal.generate_image import apply_prompt_placeholders  # noqa: E402

SKILL = "moss50"          # 骨架用 moss50（KSampler + SLG + 负向端口），steps 压到 30
STEPS = 30
# 默认 9:16 竖条（抖音原生画幅）。720 宽是能做到精确 9:16 又都整除 16 的档里
# 最贴近 80 秒的一个：实测 720×1280/30 步 = 81.2 秒，和 816×1024 的 82.1 秒几乎
# 一样，但比例对了。更省的 576×1024 只要 52 秒，代价是放大到 1080 要 1.875 倍。
WIDTH, HEIGHT = 720, 1280
SEED_BASE = 20261501
STATE_ROOT = os.path.join(ROOT, "state", "moss_comic")
OUT_ROOT = "web/moss漫画"


def _slug(text, limit=14):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "", text or "")
    return (s or "untitled")[:limit]


def _new_task_dir(spec):
    stamp = time.strftime("%Y%m%d_%H%M")
    d = os.path.join(STATE_ROOT, "%s_%s" % (stamp, _slug(spec.get("title"))))
    os.makedirs(d, exist_ok=True)
    return d


def _save(path, spec, raw, prompts):
    with open(os.path.join(path, "spec.json"), "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(path, "spec_raw.txt"), "w", encoding="utf-8") as fh:
        fh.write(raw or "")
    with open(os.path.join(path, "prompts.txt"), "w", encoding="utf-8") as fh:
        for p in spec.get("pages", []):
            fh.write("── 第 %s 页 [%s] %d 格\n%s\n\n"
                     % (p.get("n"), p.get("layout"),
                        len(p.get("panels") or []), prompts.get(p.get("n"), "")))


def build_workflow(base_wf, prompt, seed, prefix, steps=STEPS, size=(WIDTH, HEIGHT)):
    wf = json.loads(json.dumps(base_wf))
    wf["30"]["inputs"]["width"] = size[0]
    wf["30"]["inputs"]["height"] = size[1]
    wf["40"]["inputs"]["steps"] = steps
    wf["60"]["inputs"]["filename_prefix"] = prefix
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)


def _abs(path):
    base = os.environ.get("COMFYUI_OUTPUT_DIR") or ""
    if not base or not path:
        return path
    return path if os.path.isabs(path) else os.path.join(base, path)


def parse_size(text):
    """`812x1024` → (816, 1024)。moss 的 latent 要求 16 的倍数，不是就向上对齐。"""
    raw = (text or "").lower().replace("*", "x").replace("×", "x").strip()
    try:
        w, h = [int(x) for x in raw.split("x")]
    except Exception:                                   # noqa: BLE001
        sys.exit("尺寸写成 1024x1536 这样，收到的是 %r" % text)
    if w <= 0 or h <= 0:
        sys.exit("尺寸得是正数")
    aw, ah = (w + 15) // 16 * 16, (h + 15) // 16 * 16
    if (aw, ah) != (w, h):
        print("提示：%dx%d 不是 16 的倍数，对齐成 %dx%d（moss 的 latent 要求）"
              % (w, h, aw, ah))
    return aw, ah


def draw(page, prompt, base_wf, seed, prefix, out_dir, steps=STEPS, size=(WIDTH, HEIGHT)):
    wf = build_workflow(base_wf, prompt, seed, prefix, steps, size)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL, prompt=prompt[:200],
                                  seed=seed, out_dir=out_dir)
    if job is None:
        print("   ✗ 没进队列：%s" % why, flush=True)
        return None
    print("   入队 %s（seed %d），等出图…" % (job.tag, seed), flush=True)
    t0 = time.time()
    try:
        job.wait()
    except Exception as exc:                    # noqa: BLE001
        print("   ✗ 失败：%s（等了 %.0f 秒）" % (exc, time.time() - t0))
        return None
    files = [_abs(f) for f in image_jobs.output_images(job.entry, job=job)]
    print("   ✓ %.1f 秒 → %s" % (time.time() - t0, files[0] if files else "没找到图"))
    return files[0] if files else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", default="", help="一句话剧情")
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--layout", default="",
                    help="强制版式：single / 2h / 3h / 3t / 2x2（单页故事用 2x2）")
    ap.add_argument("--style", default="wuxia",
                    help="wuxia / daily / suspense / soft，或直接给风格串，空=让模型自己定")
    ap.add_argument("--only", default="", help="只画这几页，如 2,3")
    ap.add_argument("--reuse", default="", help="复用已有任务目录里的 spec.json")
    ap.add_argument("--seed", type=int, default=0, help="覆盖基础种子（重抽用）")
    ap.add_argument("--steps", type=int, default=STEPS,
                    help="采样步数（默认 %d；非 30 步时文件名带 _sN 后缀便于对比）" % STEPS)
    ap.add_argument("--size", default="%dx%d" % (WIDTH, HEIGHT),
                    help="出图尺寸，如 1024x1536 / 816x1024（自动对齐到 16 的倍数）")
    ap.add_argument("--no-text", action="store_true", help="出纯底图，气泡后期贴")
    ap.add_argument("--dry", action="store_true", help="只编剧 + 打印提示词，不出图")
    args = ap.parse_args()
    size = parse_size(args.size)

    # ── 1. 拿脚本 ────────────────────────────────────────
    if args.reuse:
        task_dir = os.path.abspath(args.reuse)
        spec_path = os.path.join(task_dir, "spec.json")
        if not os.path.isfile(spec_path):
            sys.exit("找不到 %s" % spec_path)
        with open(spec_path, encoding="utf-8") as fh:
            spec = json.load(fh)
        print("复用脚本：%s" % task_dir)
    else:
        if not args.brief.strip():
            sys.exit("要么 --brief 给剧情，要么 --reuse 复用已有脚本")
        print("编剧中（%d 页 / 风格 %s）…" % (args.pages, args.style or "模型自定"), flush=True)
        t0 = time.time()
        spec, raw, err = moss_comic.write(args.brief, pages=args.pages, style=args.style,
                                          ratio="%dx%d" % size,
                                          layout=args.layout)
        if spec is None:
            sys.exit("编剧失败：%s\n\n原始回复前 500 字：\n%s" % (err, (raw or "")[:500]))
        print("编剧完成 %.1f 秒：%s" % (time.time() - t0, spec.get("title") or "（无标题）"))
        task_dir = _new_task_dir(spec)

    # ── 2. 体检 ──────────────────────────────────────────
    issues = moss_comic.check(spec)
    print("\n体检：%s" % ("没问题" if not issues else "%d 条" % len(issues)))
    for it in issues:
        print("   · " + it)
    by_n = {p.get("n"): p for p in spec.get("pages", [])}
    print("页数 %d / 角色 %d 个 / 画风 %s"
          % (len(by_n), len(spec.get("cast") or {}), (spec.get("style") or "")[:60]))

    # ── 3. 拼提示词 ──────────────────────────────────────
    prompts = {}
    for n, page in sorted(by_n.items()):
        prompts[n] = moss_comic.build_page_prompt(spec, page, with_text=not args.no_text)
    if not args.reuse:
        _save(task_dir, spec, raw, prompts)
        print("已存：%s" % task_dir)

    print("\n=== 每页提示词 ===")
    for n in sorted(prompts):
        p = by_n[n]
        txt = prompts[n]
        print("\n── 第 %s 页 [%s] %d 格 · %d 词 · %d 字符"
              % (n, p.get("layout"), len(p.get("panels") or []),
                 len(txt.split()), len(txt)))
        print("   " + txt)
        for k, panel in enumerate(p.get("panels") or [], 1):
            bits = [x for x in (panel.get("caption"), panel.get("bubble"),
                                panel.get("sfx")) if x]
            if bits:
                print("   格%d 文字：%s" % (k, " / ".join(bits)))

    if args.dry:
        print("\n（--dry：没出图）")
        return

    # ── 4. 出图 ──────────────────────────────────────────
    base_wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not base_wf:
        sys.exit("moss50 的 workflow.json 读不出来")

    want = sorted(prompts)
    if args.only.strip():
        keys = []
        for x in args.only.split(","):
            x = x.strip().lstrip("p")
            if x.isdigit():
                keys.append(int(x))
        want = [n for n in want if n in keys]

    seed_base = args.seed or SEED_BASE
    out_dir = "%s/%s" % (OUT_ROOT, os.path.basename(task_dir))
    # 换步数 / 换尺寸做对比时给文件名加后缀，免得和默认的图混在一起
    tag = "" if args.steps == STEPS else "_s%d" % args.steps
    if size != (WIDTH, HEIGHT):
        tag += "_%dx%d" % size
    print("\n=== 出图 %d 页（%d 步 / %dx%d）→ %s ==="
          % (len(want), args.steps, size[0], size[1], out_dir))
    done = {}
    for n in want:
        print("\n[第 %s 页]" % n)
        seed = seed_base + n - 1
        done[n] = draw(by_n[n], prompts[n], base_wf, seed,
                       "MossComic_%s_P%02d%s" % (_slug(spec.get("title")), n, tag),
                       out_dir, args.steps, size)

    print("\n=== 产出 ===")
    for n in want:
        print("  第 %-2s 页 → %s" % (n, done.get(n) or "没出来"))


if __name__ == "__main__":
    main()

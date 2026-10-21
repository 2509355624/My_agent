"""把《同款便签》六页整页提示词按 `moss_page` 档跑出来（走 image_jobs 队列）。

用法:
    python tools/run_moss_pages.py --pages 1          # 只跑第 1 页
    python tools/run_moss_pages.py --pages 1,2,3
    python tools/run_moss_pages.py --all

为什么不裸 POST /prompt：2026-10-20 实测记录里，裸 POST 三次里崩两次
（ComfyUI 一重启整条路全废）。走队列能白拿四样东西——探活、内存守卫、
渠道切换 /free、出图落到 `output/web/百合整页/moss_page/`。

⚠️ 这里**不改任何生产代码路径**：workflow 在本脚本里就建好，`process()` 拿
`job.workflow` 直接提交（见 image_jobs.process 开头那段）。所以 `moss_page`
不需要进 QQ 的渠道白名单也能跑。
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

SKILL = "moss_page"
OUT_DIR = "web/百合整页"                        # output/web/百合整页/moss_page/
PAGES_PATH = os.path.join(ROOT, "skills", "moss_page", "pages.json")
BASE_SEED = 20261020


def load_pages():
    with open(PAGES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build(page_no, pages, base_wf):
    """第 page_no 页（从 1 数）的工作流 + 种子。"""
    page = pages[page_no - 1]
    # 第 4 页原文：便签被纸边与手指故意裁切，所以文档要求把 `cut-off text`
    # 从负面里删掉（否则模型会为了不裁切而把便签缩成完整一张，构图就散了）。
    wf = base_wf
    if page_no == 4:
        wf = json.loads(json.dumps(base_wf))
        neg = wf["21"]["inputs"]["text"].replace("cut-off text, ", "")
        wf["21"]["inputs"]["text"] = neg
    seed = BASE_SEED + page_no
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False),
                                    page["pos"], seed), seed


def run(page_no, pages, base_wf):
    wf, seed = build(page_no, pages, base_wf)
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL,
                                  prompt=pages[page_no - 1]["pos"][:200],
                                  seed=seed, out_dir=OUT_DIR)
    if job is None:
        print("[第 %d 页] 没进队列：%s" % (page_no, why))
        return None
    print("[第 %d 页] 入队 %s（seed %d），等出图…" % (page_no, job.tag, seed))
    t0 = time.time()
    try:
        job.wait()
    except Exception as exc:                    # noqa: BLE001
        print("[第 %d 页] 失败：%s（等了 %.0f 秒）" % (page_no, exc, time.time() - t0))
        return None
    files = image_jobs.output_images(job.entry, job=job)
    print("[第 %d 页] 完成 %.1f 秒 → %s" % (page_no, time.time() - t0, files))
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="", help="逗号分隔的页码，如 1,2,3")
    ap.add_argument("--all", action="store_true", help="六页全跑")
    args = ap.parse_args()

    pages = load_pages()
    base_wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not base_wf:
        sys.exit("moss_page 的 workflow.json 读不出来")
    print("画布 %s×%s / steps %s / cfg %s / SLG %s (0~%s)"
          % (base_wf["30"]["inputs"]["width"], base_wf["30"]["inputs"]["height"],
             base_wf["40"]["inputs"]["steps"], base_wf["40"]["inputs"]["cfg"],
             base_wf["14"]["inputs"]["single_layers"],
             base_wf["14"]["inputs"]["end_percent"]))

    if args.all:
        picked = list(range(1, len(pages) + 1))
    elif args.pages.strip():
        picked = [int(x) for x in args.pages.split(",") if x.strip()]
    else:
        sys.exit("要跑哪几页？--pages 1  或  --all")

    done = {}
    for n in picked:
        if not 1 <= n <= len(pages):
            print("跳过：没有第 %d 页" % n)
            continue
        done[n] = run(n, pages, base_wf)
    print("\n汇总：")
    for n in picked:
        print("  第 %d 页 %s → %s" % (n, pages[n - 1]["title"],
                                     done.get(n) or "没出来"))


if __name__ == "__main__":
    main()

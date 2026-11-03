"""跑一页 moss 漫画，看当前版式的出图质量（临时探针）。

    python tools/_probe_moss_layout.py --layout 1t2m2b
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import moss_api, moss_comic            # noqa: E402
from app.skills import load_workflow           # noqa: E402

BRIEF = ("这是一部成人向漫画的一页。女主和泉纱雾，银发双马尾，20 岁，"
         "害羞的插画师。男主是她哥哥。此刻她坐在书桌前面画，他从背后抱住她。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", default=None)
    ap.add_argument("--seed", type=int, default=20261103)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    layout = a.layout or moss_api.__dict__.get("_LAYOUT") or "1t2m2b"
    spec, raw, err = moss_comic.write(BRIEF, pages=1, layout=layout)
    if spec is None:
        print("编剧失败:", err)
        print((raw or "")[:600])
        return 1
    print("=== 编剧 ===")
    print("layout:", spec["pages"][0].get("layout"),
          "| 格数:", len(spec["pages"][0].get("panels") or []))
    print("check:", moss_comic.check(spec) or "(无问题)")
    page = spec["pages"][0]
    for i, p in enumerate(page.get("panels") or [], 1):
        print("  %d. 气泡=%r 旁白=%r" % (i, p.get("bubble"), p.get("caption")))

    prompt = moss_comic.build_page_prompt(spec, page)
    base_wf = load_workflow(os.path.join(
        moss_api.BASE_DIR, "skills", moss_api.SKILL, "workflow.json"))
    out = "web/moss漫画/_probe_layout" + (("_" + a.tag) if a.tag else "")
    print("\n=== 出图中（%dx%d / %d 步 / %s）===" %
          (moss_api.WIDTH, moss_api.HEIGHT, moss_api.STEPS, layout))
    t0 = time.time()
    f, why = moss_api._draw(page, prompt, base_wf, a.seed, out,
                            spec.get("title") or "probe")
    print("耗时 %.1fs\nfile: %s\nerr: %s" % (time.time() - t0, f, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())

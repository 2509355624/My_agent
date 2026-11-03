"""用真实历史跑一次「出图」的完整链路（编剧 → 出图）。

    python tools/_probe_real_brief.py --uin 2509355624
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import moss_api, moss_comic, persona      # noqa: E402
from app.skills import load_workflow               # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uin", default="2509355624")
    ap.add_argument("--card", default="")
    ap.add_argument("--draw", action="store_true")
    a = ap.parse_args()

    key = "private_" + a.uin
    ent = persona._entry(key) or {}
    card = persona.active_card(key)
    if card is None and a.card:
        card = persona.find_card(a.card)
    if card is None:
        print("这条线不在扮演态，且没指定 --card")
        return 1

    brief = persona.comic_brief(card, key)
    print("=== brief ===")
    print(brief)
    print()

    spec, raw, err = moss_comic.write(brief, pages=1, layout="1t2m2b")
    if spec is None:
        print("编剧失败:", err)
        print((raw or "")[:800])
        return 1
    page = spec["pages"][0]
    print("=== 编剧结果 ===")
    print("标题:", spec.get("title"))
    print("格数:", len(page.get("panels") or []))
    print("check:", moss_comic.check(spec) or "(无问题)")
    for i, p in enumerate(page.get("panels") or [], 1):
        print("[%d] %s" % (i, p.get("desc", "")[:130]))
        if p.get("bubble") or p.get("caption"):
            print("     气泡=%r 旁白=%r" % (p.get("bubble"), p.get("caption")))

    if not a.draw:
        return 0

    prompt = moss_comic.build_page_prompt(spec, page)
    base_wf = load_workflow(os.path.join(
        moss_api.BASE_DIR, "skills", moss_api.SKILL, "workflow.json"))
    out = "web/moss漫画/_probe_real"
    print("\n=== 出图 ===")
    t0 = time.time()
    f, why = moss_api._draw(page, prompt, base_wf, 20261103, out,
                            spec.get("title") or "real")
    print("耗时 %.1fs\nfile: %s\nerr: %s" % (time.time() - t0, f, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())

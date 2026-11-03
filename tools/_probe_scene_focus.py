"""验证「一页一个瞬间 + 点名要画的东西」在提示词层是否生效，并出图。

    python tools/_probe_scene_focus.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import moss_api, moss_comic            # noqa: E402
from app.skills import load_workflow           # noqa: E402

BRIEF = """这是一部成人向漫画的一页。

## 画什么
**这是一页，不是一个故事。** 只画下面「这一刻」的那一个瞬间，用五格从不同角度拍它，不要推进时间、不要讲起承转合。

## 人物
- 女主角：和泉纱雾。外观固定：masterpiece, best quality, 1girl, solo, silver hair, long hair, waist-length hair, blue eyes, pale skin, slender adult figure, pink hair ribbons, twin tails, blush, natural adult proportions
- 男主角：an adult man in his late twenties, short black hair, plain dark shirt, average build, natural proportions

## 这一刻的状态（画面里必须和它一致）
仰躺在床上 · 白色睡裙被推到腰上，下身完全裸露 · 腿分开着

## 用户点名要画的（最高优先，别绕开）
看到小穴。"""


def main():
    spec, raw, err = moss_comic.write(BRIEF, pages=1, layout="1t2m2b")
    if spec is None:
        print("编剧失败:", err)
        print((raw or "")[:600])
        return 1
    page = spec["pages"][0]
    print("=== 编剧 ===")
    print("格数:", len(page.get("panels") or []))
    for i, p in enumerate(page.get("panels") or [], 1):
        print("[%d] %s" % (i, p.get("desc", "")[:110]))
    print("\ncheck:", moss_comic.check(spec) or "(无问题)")

    prompt = moss_comic.build_page_prompt(spec, page)
    base_wf = load_workflow(os.path.join(
        moss_api.BASE_DIR, "skills", moss_api.SKILL, "workflow.json"))
    out = "web/moss漫画/_probe_scene_focus"
    print("\n=== 出图 ===")
    t0 = time.time()
    f, why = moss_api._draw(page, prompt, base_wf, 20261103, out,
                            spec.get("title") or "scene")
    print("耗时 %.1fs\nfile: %s\nerr: %s" % (time.time() - t0, f, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())

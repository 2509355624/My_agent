"""验证扮演态连续「出图」会接到同一条线上（不新开）。

    python tools/_probe_persona_comic_series.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import moss_series, persona            # noqa: E402

TARGET, TID = "private", "999001"


def main():
    card = persona.find_card("和泉纱雾")
    if card is None:
        print("找不到卡")
        return 1
    key = "private_999001"
    persona.switch(key, card)

    print("== 第一次出图 ==")
    sid1 = moss_series.current(TARGET, TID)
    print("当前线（应为空）:", repr(sid1))

    # 模拟 moss 新开一条线
    s = moss_series.open_new(TARGET, TID, title="测试", style="", cast={})
    sid = s["id"]
    page = moss_series.mutate(sid, lambda x: moss_series.add_page(
        x, parent_id="", task="t1", brief="第一页剧情", seed=1,
        title="测试", owner=""))
    print("新开:", sid, "第", page["n"], "页")
    print("current() =", moss_series.current(TARGET, TID))

    print("\n== 第二次「出图」：应该接到同一条线 ==")
    cur = moss_series.current(TARGET, TID)
    series = moss_series.load(cur)
    newest = moss_series.latest(series, "")
    print("这条线最新一页:", newest["n"], "| 标题:", newest.get("title"))
    page2 = moss_series.mutate(cur, lambda x: moss_series.add_page(
        x, parent_id=newest["id"], task="t2", brief="第二页剧情", seed=2,
        title="测试", owner=""))
    print("加了一页 → 第", page2["n"], "页（应为 2，不是新线的第 1 页）")

    series = moss_series.load(cur)
    print("这条线共", len(moss_series.alive(series, "")) or
          len(series.get("pages") or []), "页")

    persona.leave(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""验收：用「模糊回忆式」提问测原话检索，不提序号、不提展区。

这是用户 2026-11-15 明确的口径：
  「我记得有一个动物好像会发光，前面有个小灯泡……是什么来着？」
用户自己都记不清名字，模型应该能把它召回来，再反问「你说的是不是灯笼鱼」。

所以这里**不许出现「第一站」「第几轮」这类指路词**——那种问法等于把答案
写在题目里，测出来的命中率是假的。

走的是现成接口 `persona_rag.retrieve_raw()`：阈值 0.70、最多 3 条、MMR 去重，
和线上扮演时注入原话的那条链路完全一样。

跑：`python tools/_probe_guide_recall.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LINE = "showcase_guide_v1"

# (模糊问法, 期望命中的关键词)
CASES = [
    ("我记得有个动物好像会发光，前面有个小灯泡那样的，是什么来着？", ("鮟鱇", "钓竿", "发光")),
    ("有一种水母特别毒，被蜇了会出人命的，是哪种？", ("箱水母", "剧毒", "致命")),
    ("有种动物会拿石头砸贝壳吃，好像还会仰着漂在水上", ("海獭", "石头", "贝壳")),
    ("有一种鲨鱼脑袋像个锤子，眼睛长在两边", ("双髻鲨", "锤", "视野")),
    ("以前古代当钱用的那种小贝壳，滑溜溜的", ("宝螺", "货币", "壳")),
    ("有种鱼睡觉会吐一层黏液把自己包起来，怕被闻到", ("鹦嘴鱼", "黏液", "包")),
]


def main():
    from dotenv import load_dotenv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass

    from app import persona_rag

    if not persona_rag.enabled_for(LINE):
        print("记忆没开，先检查 PERSONA_RAG")
        return

    # `retrieve_raw` 在 daemon 没 ready 时直接返回空（线上靠聊天自然触发启动，
    # 脚本里得自己拉一次，否则六题全是「一条都没召回」的假结果）。
    client = persona_rag._client()
    if not client.is_ready():
        client.start()
        import time
        for _ in range(40):
            if client.is_ready():
                break
            time.sleep(0.5)
    print("daemon ready:", client.is_ready())

    hit_n = 0
    for q, keys in CASES:
        docs = persona_rag.retrieve_raw(LINE, q)
        print("\n问：%s" % q)
        if not docs:
            print("  （一条都没召回）")
            continue
        ok = False
        for d in docs[:3]:
            txt = " ".join(str(d.get("content") or "").split())
            try:
                dist = float(d.get("distance", 1.0))
            except (TypeError, ValueError):
                dist = 1.0
            mark = ""
            if any(k in txt for k in keys):
                ok = True
                mark = "  <= 命中"
            print("  %.3f  %s%s" % (dist, txt[:70], mark))
        if ok:
            hit_n += 1
            print("  ✅ 召回里出现了期望内容")
        else:
            print("  ❌ 召回里没出现 %s" % ("/".join(keys)))
    print("\n合计 %d/%d 题召回正确内容" % (hit_n, len(CASES)))


if __name__ == "__main__":
    main()

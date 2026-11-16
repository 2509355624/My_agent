# -*- coding: utf-8 -*-
"""把海洋馆母本灌进记忆库——**只调用现成接口**，不另造一套。

用的就是角色扮演那条链：
  - `persona_rag.remember(line, 用户说, 她答)`：每 `PERSONA_RAG_INGEST_EVERY`
    轮（默认 5）攒一批，交给现成的提炼 AI `distill()` 处理，和线上一模一样；
  - `persona_rag.raw_entries(turns)`：现成的原文层条目构造器（id `raw-<轮>-u/a`、
    前缀「用户说：/她：」），平时给 `_backfill_raw` 用，这里直接喂 `client.ingest`。

脚本本身不发明任何字段：配对规则、批大小、提炼口径全部沿用线上。

跑：
  `python tools/_seed_showcase_guide.py --dry`     # 只看会跑几批、不写库
  `python tools/_seed_showcase_guide.py`           # 真跑
  `python tools/_seed_showcase_guide.py --drop`    # 清掉这条演示线
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LINE = "showcase_guide_v1"          # 展示用的固定会话线，`guest_` 开头会被跳过
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:                                        # noqa: BLE001
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass
    # 提炼是后台线程干的，不打日志就只剩「一条都没进库」这种哑结果
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")


def pairs_from(turns):
    """母本 250 条 → (游客说, 讲解员答) 配对。

    只收**真正的问答**：游客那句之后紧跟的那条讲解员发言。每个展项开头那段
    讲解员介绍前面没有提问，不硬塞占位——它留在原文层（`raw_entries` 收全部
    250 条），提炼层只收问答，免得提炼 AI 把独白当成"回答"编出奇怪的事实。
    """
    out = []
    pending = None
    for t in turns:
        if t["role"] == "visitor":
            pending = t["text"]
        elif pending:
            out.append((pending, t["text"]))
            pending = None
    return out


def raw_turns(turns):
    """套成 `raw_entries()` 认识的形状。

    它要的是「一轮」：`{"turn": n, "user": 用户那句, "assistant": 她答的那句}`，
    两侧有一侧为空时只写另一侧。母本是一条条交替消息，这里按轮合并回去——
    开场那段讲解员独白前面没有提问，就只填 assistant 一侧，原话照样留得下。
    """
    rows = []
    pending = None
    turn = 0
    for t in turns:
        if t["role"] == "visitor":
            pending = t["text"]
            continue
        turn += 1
        rows.append({
            "turn": turn,
            "user": pending or "",
            "assistant": t["text"],
            "day": 0,
            "phase": "",
        })
        pending = None
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只统计，不写库")
    ap.add_argument("--limit", type=int, default=0, help="只灌前 N 对，调参用")
    ap.add_argument("--drop", action="store_true", help="删掉这条演示线")
    ap.add_argument("--reset", action="store_true",
                    help="清掉这条线的待提炼缓冲（重跑前用，否则 seq 会接着涨、前几批重复提炼）")
    ap.add_argument("--raw-only", action="store_true",
                    help="只刷原文层，不动已提炼的批次（改母本措辞后重灌用）")
    args = ap.parse_args()

    _ensure_env()
    from app import persona_rag
    from app.config import PERSONA_RAG_INGEST_EVERY

    coll = persona_rag.collection_name(LINE)
    client = persona_rag._client()

    if args.reset:
        with persona_rag._PENDING_LOCK:
            data = persona_rag._pending_load() or {}
            gone = data.pop(LINE, None)
            persona_rag._pending_save(data)
        print("已清掉 %s 的待提炼缓冲：%s" % (LINE, bool(gone)))
        if not (args.drop or args.dry):
            pass
    if args.drop:
        print(client.delete_kb(coll))
        return

    data = json.load(open(os.path.join(ROOT, "web", "showcase_guide.json"),
                          encoding="utf-8"))
    turns = data["turns"]
    ps = pairs_from(turns)
    if args.limit:
        ps = ps[:args.limit]

    print("会话线 %s → collection %s" % (LINE, coll))
    print("母本 %d 条 / 配对 %d 轮 / 每批 %d 轮 → 约 %d 批提炼调用"
          % (len(turns), len(ps), PERSONA_RAG_INGEST_EVERY,
             (len(ps) + PERSONA_RAG_INGEST_EVERY - 1) // PERSONA_RAG_INGEST_EVERY))
    print("RAG 开关 enabled_for(line) =", persona_rag.enabled_for(LINE))
    if args.dry:
        for q, a in ps[:3]:
            print("\n  游客：%s\n  阿海：%s" % (q[:40], a[:40]))
        return

    if not persona_rag.enabled_for(LINE):
        print("记忆没开（PERSONA_RAG 不为真），先开再跑")
        return

    # ① 原文层：现成的 raw_entries，一条消息一个 id，重复跑靠 id 幂等
    entries = persona_rag.raw_entries(raw_turns(turns))
    print("\n写入原文层 %d 条…" % len(entries))
    done = 0
    for s in range(0, len(entries), 64):
        r = client.ingest(coll, entries[s:s + 64])
        if not r.get("ok"):
            print("原文层写入失败：", r.get("error"))
            return
        done += int(r.get("count") or 0)
    print("原文层完成 %d 条" % done)
    if args.raw_only:
        print("\n--raw-only：不跑提炼，结束。")
        return

    # ② 提炼层：只调 remember()，它自己攒批、自己排队、自己叫 distill()
    print("\n交给现成提炼 AI（每 %d 轮一批）…" % PERSONA_RAG_INGEST_EVERY)
    for i, (q, a) in enumerate(ps, 1):
        persona_rag.remember(LINE, q, a, card_name="讲解员阿海")
        if i % 20 == 0:
            print("  已投喂 %d/%d 轮" % (i, len(ps)))

    # 等队列真正排空。注意：`qsize()==0` 只代表任务被取走了，提炼线程
    # 还在跑——第一版就是在这里提前退出、把 worker 线程一起杀掉的。
    # 所以要求「连续 30 秒都空」才算跑完。
    print("\n等提炼队列跑完（每批一次模型调用，别关窗口）…")
    from app import persona_rag as pr
    last = -1
    idle = 0
    for _ in range(900):
        time.sleep(2)
        with pr._PENDING_LOCK:
            rec = (pr._pending_load() or {}).get(LINE) or {}
        left = len(rec.get("items") or [])
        batches = int(rec.get("seq") or 0)
        if left != last:
            print("  进度：已投 %d 轮，缓冲剩 %d 条" % (batches, left))
            last = left
        if left == 0 and pr._QUEUE.qsize() == 0:
            idle += 1
            if idle >= 15:
                break
        else:
            idle = 0
    else:
        print("  等待超时，提炼可能还在后台跑（看 logging 输出）")
    listed = client.list_entries(coll)
    n = len(listed.get("entries") or []) if listed.get("ok") else -1
    print("\n完成。库内共 %d 条（原文 + 提炼）" % n)


if __name__ == "__main__":
    main()

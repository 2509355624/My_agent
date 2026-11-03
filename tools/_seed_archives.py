"""把现有会话的窗口历史补写成归档的起点（2026-11-03）。

背景：归档是这轮才加的，老会话的完整历史在加之前就被 12 条滑动窗口挤掉
了，**救不回来**。能做的只有把「现在还留在窗口里的那 12 条」铺进归档，
从这一刻开始不再丢。

跑法：
    python tools/_seed_archives.py            # 只看会做什么
    python tools/_seed_archives.py --apply    # 真写
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import agents, persona                # noqa: E402
from app.config import QQ_AGENT_ID             # noqa: E402


def main(argv):
    apply_ = "--apply" in argv
    d = agents.agent_dir(QQ_AGENT_ID)
    if not d:
        print("找不到 agent 目录")
        return 1
    sess = os.path.join(d, "sessions")
    if not os.path.isdir(sess):
        print("没有 sessions 目录：%s" % sess)
        return 1

    n_made = n_skip = 0
    for p in sorted(glob.glob(os.path.join(sess, "persona_*.jsonl"))):
        name = os.path.basename(p)
        if name.startswith("persona_archive_"):
            continue
        key = name[len("persona_"):-len(".jsonl")]
        arc = persona._archive_path(key)                # noqa: SLF001
        if not arc:
            continue
        if os.path.exists(arc):
            n_skip += 1
            continue
        # 读窗口里那几条，原样铺进归档
        msgs = []
        try:
            import json
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        msgs.append(json.loads(line))
        except Exception as e:                          # noqa: BLE001
            print("  跳过 %s：%s" % (key, e))
            continue
        if not msgs:
            continue
        turns = sum(1 for m in msgs if m.get("role") == "assistant")
        print("  %-30s 窗口 %2d 条 / %d 轮 -> 归档" % (key, len(msgs), turns))
        if apply_:
            persona._append_archive(key, [], seed=msgs)  # noqa: SLF001
        n_made += 1

    print()
    print("需要补写 %d 个，已有归档 %d 个" % (n_made, n_skip))
    if not apply_:
        print("（只是看看。要真写就加 --apply）")
    else:
        print("已写入。注意：更早的历史在加归档之前就丢了，救不回来。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

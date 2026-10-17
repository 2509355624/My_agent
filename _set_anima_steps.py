# -*- coding: utf-8 -*-
"""把 hd_3_* 四个画风的**第一段** KSampler（node 2）步数 10 -> 20。

背景：2026-10-11 A/B 实测（`_anima_steps_ab.py`，同 seed 对照，对照跑逐像素一致）
—— 第一段翻到 20 步只 +12.3 s 采样，且 4 对里至少 1 例消除了「多画一只猫」这种
结构错误。用户拍板转正。

⚠️ 只动 node 2。node 19（第二段，denoise 0.25）**一个字节都不碰**。
⚠️ 字节级替换，保留 CRLF；`"seed": __SEED__` 是裸 token，绝不走 json.loads/dumps。

用法：python _set_anima_steps.py          # 干跑，只打印
      python _set_anima_steps.py --apply  # 真改
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
# node 2 的块里，从 `"2": {` 到第一个 `"steps":` 之间不会有 `}`（inputs 里全是数组/标量）
PAT = re.compile(rb'("2":\s*\{[^}]*?"steps":\s*)10(?=\s*[,\}])')


def main(apply):
    paths = sorted(glob.glob(os.path.join(ROOT, "skills", "hd_3_*", "workflow.json")))
    paths += sorted(glob.glob(os.path.join(ROOT, "skills", "hd_3_*", "workflow_i2i.json")))
    if not paths:
        raise SystemExit("!! 没找到 hd_3_* 工作流")

    for p in paths:
        rel = os.path.relpath(p, ROOT)
        with open(p, "rb") as f:
            raw = f.read()
        crlf = raw.count(b"\r\n")
        new, n = PAT.subn(rb"\g<1>20", raw, count=1)
        if n != 1:
            raise SystemExit("!! %s 匹配到 %d 处 node2 steps，预期 1 处 —— 停手" % (rel, n))
        # 复查：node19 的 steps 必须仍是 10
        after = new
        m19 = re.search(rb'"19":\s*\{[^}]*?"steps":\s*(\d+)', after)
        if not m19 or m19.group(1) != b"10":
            raise SystemExit("!! %s 第二段步数被动到了（%r）—— 停手" % (rel, m19.group(1) if m19 else None))
        # 复查：整个文件里 `"steps": 20` 只能出现 1 次
        if after.count(b'"steps": 20') != 1:
            raise SystemExit("!! %s 出现 %d 处 20 步" % (rel, after.count(b'"steps": 20')))
        print("%-42s CRLF=%d  %s" % (rel, crlf, "已改" if apply else "待改（干跑）"))
        if apply:
            with open(p, "wb") as f:
                f.write(after)


if __name__ == "__main__":
    main("--apply" in sys.argv)

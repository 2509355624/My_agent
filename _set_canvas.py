# -*- coding: utf-8 -*-
"""改某个 qwen 渠道的「画布」（workflow.json / workflow_i2i.json 里的 EmptyLatentImage）。

⚠️ 纯字节级替换，**不** json.loads / json.dump：
   上一次改工作流时用 `json.loads(raw.replace('__SEED__','1'))` 再 dump 回去，
   把 `__SEED__` 永久烘成了字面量 1（skills/ 被 gitignore，git diff 看不见，
   测试也全绿）——2026-10-10 §30 的坑。这里只动 width/height 两个数字，
   行尾（CRLF/LF）和 `__SEED__` 原样保留。

用法：  python _set_canvas.py charsheet 2304 1536
"""
import io
import re
import sys

TARGETS = {
    "charsheet": ["skills/qwen-charsheet/workflow.json",
                  "skills/qwen-charsheet/workflow_i2i.json"],
    # ⚠️ restore 的 workflow_i2i.json **没有** EmptyLatentImage（i2i 骨架把它换成
    #    LoadImage + TextEncodeQwenImage21 的 latent 输出），而闸门只读
    #    `skill_data["workflow"]`（t2i 那份，见 generate_image.py:964），
    #    所以只需要改 workflow.json。
    "restore":   ["skills/qwen-restore/workflow.json"],
}

# 只认「EmptyLatentImage 节点的 inputs 里紧挨着的那对 width/height」
PAT = re.compile(
    rb'("class_type"\s*:\s*"EmptyLatentImage"\s*,\s*"inputs"\s*:\s*\{\s*'
    rb'"width"\s*:\s*)(\d+)(\s*,\s*"height"\s*:\s*)(\d+)'
)


def patch(path, w, h):
    with open(path, "rb") as f:
        raw = f.read()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    hits = PAT.findall(raw)
    if len(hits) != 1:
        raise SystemExit("!! %s 匹配到 %d 处 EmptyLatentImage（应为 1）" % (path, len(hits)))
    old_w, old_h = hits[0][1], hits[0][3]
    new = PAT.sub(
        lambda m: m.group(1) + str(w).encode() + m.group(3) + str(h).encode(),
        raw,
    )
    with open(path, "wb") as f:
        f.write(new)
    # 自检：seed 占位符还在，且只有一个 width/height 被改
    assert b"__SEED__" in new, "%s 里 __SEED__ 丢了！" % path
    print("  %-46s %sx%s -> %sx%s   CRLF=%d LF=%d  __SEED__=OK"
          % (path, old_w.decode(), old_h.decode(), w, h, crlf, lf))


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    skill, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    if skill not in TARGETS:
        raise SystemExit("skill 只能是 %s" % list(TARGETS))
    print("[%s] 画布 -> %dx%d" % (skill, w, h))
    for p in TARGETS[skill]:
        patch(p, w, h)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

# -*- coding: utf-8 -*-
"""提交信息脱敏过滤（2026-11-06 用户要求）。

仓库是公开的，提交信息里出现「射精 / 高潮 / 堕落 / 屈服 / 开发度」这类词
不太合适。这个脚本从 stdin 读一条提交信息、往 stdout 写替换后的版本，
供 `git filter-branch --msg-filter` 调用；也可以 `--preview` 直接统计效果。

替换只做**词级**、按长词优先，避免把「属性 / 个性 / 线性」里的「性」误伤。
技术含义保留：峰值=高潮值，敏感度=开发度，沉浸度=堕落，顺从度=屈服，
激发=冲动，收尾=射精。
"""
import io
import sys

MAP = [
    ("射精弹窗", "收尾弹窗"),
    ("射精选择", "收尾选择"),
    ("射精", "收尾"),
    ("高潮值", "峰值"),
    ("高潮次数", "峰值次数"),
    ("高潮", "峰值"),
    ("堕落值", "沉浸度"),
    ("堕落", "沉浸度"),
    ("屈服度", "顺从度"),
    ("屈服", "顺从度"),
    ("开发度", "敏感度"),
    ("冲动值", "激发值"),
    ("冲动", "激发"),
    ("小穴", "部位"),
    ("肛门", "部位"),
    ("乳头", "部位"),
    ("内射", "体内结算"),
    ("做爱", "亲密场景"),
    ("压迫模式", "强制场景"),
    ("调教", "养成"),
    ("发情", "情绪"),
    ("情欲", "情绪"),
    ("肉体", "身体"),
    ("淫水", "体液"),
    ("精液", "体液"),
    ("处女", "无经验"),
    ("花核", "部位"),
    ("花蕊", "部位"),
    ("蜜穴", "部位"),
    ("玉茎", "部位"),
]

# 自检用：替换后这些词不该再出现在提交信息里。
BAD = ["射精", "高潮", "堕落", "屈服", "开发度", "冲动", "小穴", "肛门",
       "乳头", "内射", "做爱", "调教", "发情", "情欲", "淫水", "精液",
       "处女", "花核", "花蕊", "蜜穴", "玉茎"]


def scrub(text):
    for a, b in MAP:
        text = text.replace(a, b)
    return text


def main():
    if "--preview" in sys.argv:
        import subprocess
        out = subprocess.run(["git", "log", "--pretty=%B%x00"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        changed = 0
        for msg in out.split("\x00"):
            if not msg.strip():
                continue
            new = scrub(msg)
            if new != msg:
                changed += 1
                print("--- 改：" + msg.strip().splitlines()[0][:70])
                print("+++ 成：" + new.strip().splitlines()[0][:70])
        left = sum(new.count(k) for k in BAD)
        print("\n受影响提交 %d 条；替换后残留敏感词 %d 个" % (changed, left))
        return
    data = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    sys.stdout.buffer.write(scrub(data).encode("utf-8"))


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                 errors="replace")
    main()

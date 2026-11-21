# -*- coding: utf-8 -*-
"""不出图，先看提示词。

单 Agent 链路落盘的 `spec.json` 里，每页的 `prompt_override` 就是最终交给
ComfyUI 的 Layout JSON 原文——出图端一个字节都不会再加。所以画面层的问题
（谁穿了什么、手里拿了什么、字落在哪儿、这一格有没有两个状态）在提交队列
之前就全都在文本里躺着，不必等 20 秒出图再看。

这个脚本只读文件、不调 API、不入队，做两件事：

1. 把每页摊开成人能读的清单（格 / bbox / 词数 / 全文，文字条原文与归属格）
2. 跑一遍文本层能确定的事实检查，分「错」和「疑」两档

用法：
    python tools/moss_prompt_review.py state/_selfcheck/agent_20261121_235620
    python tools/moss_prompt_review.py state/moss_comic/20261121_2355_xxx
    python tools/moss_prompt_review.py path/to/spec.json

看不出来的只有纯视觉那几样：画风有没有变成网点、人物比例、手指、脸崩。
那些必须看图。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from app.features.comic import moss_agent as A          # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")


def load_spec(path):
    if os.path.isdir(path):
        for name in ("spec.json",):
            p = os.path.join(path, name)
            if os.path.exists(p):
                path = p
                break
        else:
            raise SystemExit("目录里没有 spec.json: %s" % path)
    with open(path, encoding="utf-8") as f:
        return path, json.load(f)


def cells_of(page):
    """这一页的格子 bbox 列表（character item 的，阅读顺序）。"""
    try:
        d = json.loads(page.get("prompt_override") or "{}")
    except Exception:                              # noqa: BLE001
        return []
    return [it.get("bbox") for it in (d.get("items") or [])
            if it.get("label") == "character" and it.get("bbox")]


def inside(inner, outer, pad=0):
    x1, y1, x2, y2 = [min(inner[0], inner[2]), min(inner[1], inner[3]),
                      max(inner[0], inner[2]), max(inner[1], inner[3])]
    X1, Y1, X2, Y2 = [min(outer[0], outer[2]), min(outer[1], outer[3]),
                      max(outer[0], outer[2]), max(outer[1], outer[3])]
    return x1 >= X1 - pad and x2 <= X2 + pad and y1 >= Y1 - pad and y2 <= Y2 + pad


def words(s):
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", s or ""))


def for_audit(spec):
    """把 prompt_override 里的 items 塞回页对象。

    `spec.json` 落盘时页里只有 `panels`（desc 纯文本），bbox 全在
    `prompt_override` 里；而 `moss_agent.audit` 是照模型原始输出写的，读的
    是 `page["items"]`。不补这一步，audit 会把每一页都报成「格子跟骨架对不上」。
    """
    pages = []
    for p in spec.get("pages") or []:
        q = dict(p)
        try:
            q["items"] = (json.loads(p.get("prompt_override") or "{}")
                          .get("items") or [])
        except Exception:                          # noqa: BLE001
            q["items"] = []
        pages.append(q)
    return {"cast": spec.get("cast") or {}, "pages": pages}



def _strip_noise(text, segs):
    """去掉外形串和衣物短语，剩下的才是「这一格独有的画面内容」。

    每一格都必须重写外形和衣着，这是规则要求的，不能当重复报。
    """
    low = (text or "").lower()
    for s in segs:
        low = low.replace(s.lower(), " ")
    for p in sorted(set(A._cloth_phrases(low)), key=len, reverse=True):
        if p:
            low = low.replace(p, " ")
    # 每格末尾那串画风锁是 autofix 统一加的，格格相同，不算内容重复。
    idx = low.find("monochrome greyscale screentone")
    if idx > 0:
        low = low[:idx]
    return low


def _phrases(text, n=12):
    w = re.findall(r"[a-z][a-z'-]*", (text or "").lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def review(spec):
    """返回 (rows, issues)。rows 是每页的可读结构，issues 是 [(档, 消息)]。"""
    rows, issues, warns = [], [], []
    cast = spec.get("cast") or {}
    segs = A._cast_segs(cast.get("girl") or "")

    blocking, soft = A.audit(for_audit(spec))
    for m in blocking:
        issues.append(("错", "体检：" + m))
    for m in soft:
        warns.append("体检：" + m)

    for page in spec.get("pages") or []:
        n, lay = page.get("n"), page.get("layout")
        raw = page.get("prompt_override") or ""
        if not raw.strip():
            issues.append(("错", "P%s 没有 prompt_override，出图端会走另一条拼装路径" % n))
            continue
        try:
            d = json.loads(raw)
        except Exception as e:                     # noqa: BLE001
            issues.append(("错", "P%s 的 prompt_override 不是合法 JSON：%s" % (n, e)))
            continue

        cells = [it.get("bbox") for it in (d.get("items") or [])
                 if it.get("label") == "character"]
        chars = [it for it in (d.get("items") or []) if it.get("label") == "character"]
        texts = [it for it in (d.get("items") or []) if it.get("label") == "text"]
        cap = ((d.get("summary") or {}).get("caption") or "")
        low_cap = cap.lower()

        row = {"n": n, "layout": lay, "chars": [], "texts": [],
               "beat": (page.get("beat") or "")[:70],
               "size": len(raw), "words": words(cap)}

        # ── 每格 ───────────────────────────────────────────────
        per_cell = []
        for i, it in enumerate(chars):
            c = it.get("caption") or ""
            per_cell.append(c)
            row["chars"].append({"i": i, "bbox": it.get("bbox"), "w": words(c), "cap": c})
            if CJK.search(c):
                issues.append(("错", "P%s 格%d 的英文提示词里混进中文「%s」"
                               % (n, i + 1, CJK.search(c).group(0))))
            mid = re.search(A._MIDSTATE, c.lower())
            if mid:
                issues.append(("错", "P%s 格%d 写了中间态「%s」——一格只有一个定格"
                               % (n, i + 1, mid.group(0))))
            # 同一格里同一串衣物出现两次 = 这件衣服既被写成穿在身上、又被写成
            # 拿在手里。和 `moss_agent.audit` 用的是同一条判据（`_cloth_phrases`），
            # 这里多给出「哪一格、哪一串」，方便直接改。
            ph = A._cloth_phrases(c.lower())
            dup = sorted({p for p in ph if p and ph.count(p) > 1})
            if dup:
                issues.append(("错", "P%s 格%d 同一串衣物「%s」在一格里出现 %d 次——"
                               "同一件既在身上又在手里"
                               % (n, i + 1, dup[0], ph.count(dup[0]))))
            if len(re.findall(A._PHASE, c.lower())) >= 2:
                issues.append(("错", "P%s 格%d 有两个「already/just」，是两个时间点挤进一格"
                               % (n, i + 1)))
            if segs and c:
                miss = [s for s in segs if s.lower() not in c.lower()]
                if miss:
                    issues.append(("错", "P%s 格%d 外形串漏项 %s"
                                   % (n, i + 1, "、".join("「%s」" % m[:28] for m in miss[:2]))))
            if words(c) > 110:
                warns.append("P%s 格%d 单格 %d 词，后半段容易被稀释"
                             % (n, i + 1, words(c)))

        # 同一页两格出现同一段独有画面（外形串/衣物已剔除，要求连续 12 词）
        for a in range(len(per_cell)):
            for b in range(a + 1, len(per_cell)):
                dup = _phrases(_strip_noise(per_cell[a], segs)) & \
                    _phrases(_strip_noise(per_cell[b], segs))
                if dup:
                    warns.append("P%s 格%d / 格%d 有连续 12 词相同「%s」——两格可能画成同一个状态"
                                 % (n, a + 1, b + 1, sorted(dup)[0][:70]))

        # ── 文字条 ─────────────────────────────────────────────
        for it in texts:
            tx = (it.get("text") or "").strip()
            bb = it.get("bbox")
            kind = "裸字" if "bare chinese" in (it.get("caption") or "").lower() else "气泡"
            if "caption box" in (it.get("caption") or "").lower():
                kind = "方框"
            owner = [i + 1 for i, cc in enumerate(cells) if bb and cc and inside(bb, cc)]
            row["texts"].append({"text": tx, "kind": kind, "bbox": bb, "owner": owner})
            if not tx:
                issues.append(("错", "P%s 有一条文字没写内容（只有样式描述）" % n))
            if len(tx) > 22:
                warns.append("P%s 文字「%s」%d 字，超过 22 字会被挤成多列"
                             % (n, tx[:12], len(tx)))
            if bb and cells:
                if not owner:
                    issues.append(("错", "P%s 文字「%s」bbox %s 不落在任何一格内，"
                                   "会压在格线上或飘到隔壁" % (n, tx[:10], bb)))
                elif len(owner) > 1:
                    warns.append("P%s 文字「%s」横跨格 %s" % (n, tx[:10], owner))
            elif not bb:
                issues.append(("错", "P%s 文字「%s」没有 bbox" % (n, tx[:10])))

        # ── 尾句与实际文字对不对得上 ───────────────────────────
        has_bare = any("bare chinese" in (it.get("caption") or "").lower() for it in texts)
        says_no_bare = "no bare text" in low_cap
        # 整页无字时 summary 尾句是 `_NO_TEXT_TAIL`（no text, no lettering…）；
        # 页里明明有字却写这句，出图会尽量不画字、把气泡画成空白框。
        says_no_text = bool(re.search(r"no text, no lettering|no lettering anywhere", low_cap))
        if texts and says_no_text:
            issues.append(("错", "P%s summary 说不许有字，实际有 %d 条文字" % (n, len(texts))))
        if texts and says_no_bare and has_bare:
            issues.append(("错", "P%s summary 写 no bare text，实际有裸字「%s」"
                           % (n, next(i["text"] for i in row["texts"] if i["kind"] == "裸字"))))

        # 台词在同页重复
        seen = {}
        for t in row["texts"]:
            if t["text"] and t["text"] in seen:
                issues.append(("错", "P%s 台词「%s」出现两次" % (n, t["text"])))
            seen[t["text"]] = 1

        rows.append(row)
    return rows, issues, warns


def dump(rows, issues, warns, path, spec):
    out = []
    pgs = spec.get("pages") or []
    out.append("# 提示词审查 · %s" % os.path.basename(os.path.dirname(path) or path))
    out.append("")
    out.append("页数 %d   骨架 %s   合计 %d 字符"
               % (len(pgs), " / ".join(str(p.get("layout")) for p in pgs),
                  sum(r["size"] for r in rows)))
    out.append("")
    for r in rows:
        out.append("═" * 62)
        out.append("P%s  %s  %d 字符  %d 词%s"
                   % (r["n"], r["layout"], r["size"], r["words"],
                      ("  · " + r["beat"]) if r["beat"] else ""))
        for c in r["chars"]:
            out.append("")
            out.append("  ┌ 格%d  bbox %s  %d 词" % (c["i"] + 1, c["bbox"], c["w"]))
            for line in re.findall(r".{1,100}(?:\s|$)", c["cap"]):
                out.append("  │ " + line.strip())
        if r["texts"]:
            out.append("")
            for t in r["texts"]:
                out.append("  ✎ %s「%s」落在格 %s"
                           % (t["kind"], t["text"], t["owner"] or "?"))
        out.append("")
    out.append("═" * 62)
    out.append("文本层检查：%d 处错 / %d 处待看图确认"
               % (len([i for i in issues]), len(warns)))
    for level, m in issues:
        out.append("  [%s] %s" % (level, m))
    for m in warns:
        out.append("  [疑] %s" % m)
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path, spec = load_spec(sys.argv[1])
    rows, issues, warns = review(spec)
    text = dump(rows, issues, warns, path, spec)
    out = os.path.join(os.path.dirname(path), "prompt_review.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    print("\n→ 已写入 %s" % out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

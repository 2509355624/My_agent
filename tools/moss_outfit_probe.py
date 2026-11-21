# -*- coding: utf-8 -*-
"""换装压力测试：拿几个不同话题真跑链，看编剧 AI 的衣服是不是跟着剧情换。

重点是「多段换装」——常服 → 浴衣 → 浴巾 这种，以及档案里写死的衣服会不会
被抄进每一格。每次跑 5 页，落盘后自动做四件事：

1. 打印每页每格的衣着分句（人和衣服的对应关系，肉眼一栏看完）
2. 报「档案衣服泄漏」：输入档案里那件衣服的词（dress / lace collar）出现在画面里
3. 报「旧衣回潮」：一套衣服消失过、后面又出现
4. 报「没换装」：全本只出现过一套衣服

用法：
    python tools/moss_outfit_probe.py                # 跑全部话题
    python tools/moss_outfit_probe.py 温泉            # 只跑一个
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.features.comic import moss_agent as A   # noqa: E402
from app.features.comic import moss_twopass as M  # noqa: E402
import importlib.util                              # noqa: E402

_s = importlib.util.spec_from_file_location(
    "moss_prompt_review", os.path.join(ROOT, "tools", "moss_prompt_review.py"))
R = importlib.util.module_from_spec(_s)
_s.loader.exec_module(R)

# 用户真实用法：档案在前、剧情在后。档案里那件连衣裙是压力源——它不许出现在
# 任何一格里，衣服只能由剧情决定。
GIRL_ARCHIVE = (
    "a girl about twelve, slender small frame, long straight dark hair with soft "
    "blunt bangs, a wide fluffy pale cloth headband, large round dark eyes, round soft "
    "face, faint blush, wearing a plain short-sleeve knee-length cotton dress with an "
    "embroidered lace collar and buttons down the front, a dark fabric scrunchie on "
    "her left wrist")
LOOK = "manga style, monochrome, greyscale screentones, inked lineart, bold black ink outlines, pen crosshatching, flat shading, no color cast."

TOPICS = [
    ("更衣", "女生穿白色短袖，运动短裤的，在操场做拉伸运动，浑身是汗，衣服湿透了，"
             "然后去卫生间洗脸，看到了我，然后自己脱下短袖扔给我，我把新的短袖扔给她"),
    ("温泉", "女生周末和同学去山里的温泉旅馆。路上她穿米色针织开衫、白T恤和深蓝牛仔裤，"
             "背着帆布包。到了旅馆前台，她换上馆内的深蓝细条纹浴衣，把换下来的衣服放进衣篮。"
             "然后进汤池区，在淋浴位冲洗身体，最后裹着一条白色浴巾走进露天汤池。"),
    ("淋雨", "放学时下起大雨，她没带伞，校服白衬衫和藏青百褶裙全淋透了，抱着书包跑回家。"
             "进门在玄关脱下湿校服擦头发，然后换上灰色宽松家居T恤和粉色棉短裤，"
             "坐在窗边捧着热牛奶。"),
]

# 只用于「展示和统计」，不做准入判断。比 moss_agent._CLOTH 多了和服/浴衣/
# 牛仔/泳装这些剧情里常见、但检测判据里故意没加的词。
CLOTH = (r"(?:t-shirt|shirt|tee|blouse|dress|gown|skirt|shorts|pants|jeans|trousers"
         r"|jacket|cardigan|coat|hoodie|sweater|jumper|uniform|jersey|yukata|kimono"
         r"|robe|towel|underlayer|tank top|socks|shoes|sandals|slippers|swimsuit"
         r"|bikini|bra|briefs|underwear|apron)")


def outfit_bits(caption):
    """抽出提到衣服的分句（按逗号/分号/句号切，保留含衣物词的那几段）。"""
    out = []
    for seg in re.split(r"[.;]", caption or ""):
        seg = seg.strip()
        if not seg:
            continue
        for part in re.split(r",\s*", seg):
            if re.search(CLOTH, part, re.I) and len(part) < 220:
                out.append(part.strip())
    return out


def page_sets(spec):
    """每页的衣物短语集合（用 moss_agent 的判据，跨页比较才可比）。"""
    out = []
    for p in spec.get("pages") or []:
        d = json.loads(p.get("prompt_override") or "{}")
        got = []
        for it in d.get("items") or []:
            if it.get("label") == "character":
                got += A._cloth_phrases((it.get("caption") or "").lower())
        out.append(set(got))
    return out


def check_outfit(spec, name):
    """返回 (行, 问题)。行是给人看的衣着时间线。"""
    issues, lines = [], []
    pages = spec.get("pages") or []
    sets = page_sets(spec)

    for p, ps in zip(pages, sets):
        d = json.loads(p.get("prompt_override") or "{}")
        cells = [it for it in (d.get("items") or []) if it.get("label") == "character"]
        lines.append("P%s %s" % (p.get("n"), p.get("layout")))
        for i, it in enumerate(cells, 1):
            cap = it.get("caption") or ""
            bits = outfit_bits(cap)
            tight = bool(A._TIGHT.search(cap.lower()))
            lines.append("   格%d %s" % (i, (" ｜ ".join(bits))[:300] or "（没写衣着）"))
            if not bits:
                # 脸 / 手 / 脚的特写里看不到衣服，不写是对的——硬塞一件反而画错。
                if tight:
                    lines[-1] += "   ← 局部特写，不需要衣着"
                else:
                    issues.append("P%s 格%d 没写任何衣着——人物会裸着或沿用上一格"
                                  % (p.get("n"), i))


    # 1) 档案里那件衣服不许出现在画面里
    leak = []
    for p in pages:
        d = json.loads(p.get("prompt_override") or "{}")
        for it in d.get("items") or []:
            c = (it.get("caption") or "").lower()
            for kw in ("lace collar", "knee-length cotton dress", "buttons down the front"):
                if kw in c:
                    leak.append((p.get("n"), kw))
    if leak:
        issues.append("档案衣服泄漏 %d 处：%s" % (len(leak), leak[:3]))

    # 2) 旧衣回潮：某套衣服出现 → 消失 → 又出现
    seen_order = {}
    for i, ps in enumerate(sets, 1):
        for ph in ps:
            seen_order.setdefault(ph, []).append(i)
    # 旧衣回潮：只有「某页明确写了它被脱下来」，后面又出现，才算回潮。
    # 一直穿着的短裤在某页没提、下一页又提，是省略，不是回潮（初版这样误报过）。
    _REMOVED = (r"stepped out of|already (?:fully )?off|took (?:it )?off|pulled (?:it )?off"
                r"|stripped (?:it )?off|slipped out of|kicked (?:them )?off|taken off")
    ptxt = []
    for p in spec.get("pages") or []:
        d = json.loads(p.get("prompt_override") or "{}")
        ptxt.append(" ".join((it.get("caption") or "").lower()
                             for it in d.get("items") or []
                             if it.get("label") == "character"))

    def removed_near(text, ph):
        """这个短语**后面**跟着脱掉动作，且中间没夹别的衣物。"""
        for m in re.finditer(re.escape(ph), text):
            tail = text[m.end():m.end() + 60]
            head = re.split(A._CLOTH, tail, 1)[0]      # 到下一件衣服为止
            if re.search(_REMOVED, head):
                return True
        return False

    for ph, ns in seen_order.items():
        if len(ns) < 2:
            continue
        span = list(range(ns[0], ns[-1] + 1))
        if not any(n not in ns for n in span):
            continue
        off_at = [n for n in span if removed_near(ptxt[n - 1], ph)]
        later = [n for n in ns if off_at and n > off_at[0]]
        if later:
            issues.append("旧衣回潮「%s」：第%d页已脱掉，第%d页又画回来了"
                          % (ph, off_at[0], later[0]))

    # 3) 全本只有一套衣服 = 没换装
    allph = set().union(*sets) if sets else set()
    core = {x for x in allph if len(x) > 5}
    if len(core) <= 1:
        issues.append("全本只出现一套衣服（%s），没有换装" % (list(core)[:3] or "无"))

    return lines, issues


def resolve_model(base, key, model):
    import urllib.request
    if os.environ.get("MOSS_ONESHOT_MODEL"):
        return os.environ["MOSS_ONESHOT_MODEL"]
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=60) as r:
            ids = [m.get("id") for m in
                   (json.loads(r.read().decode()).get("data") or [])]
        if model in ids:
            return model
        hit = [i for i in ids if i and i.startswith(model)]
        if hit:
            return hit[0]
    except Exception as e:                        # noqa: BLE001
        print("（列模型失败，按原名发：%s）" % e)
    return model


def main():
    env = M.load_env(os.path.join(ROOT, ".env"))
    prov = {}
    try:
        with open(os.path.join(ROOT, "data", "custom_providers.json"),
                  encoding="utf-8") as f:
            prov = (json.load(f).get("providers") or {}).get(env.get("LLM_PROVIDER")) or {}
    except Exception:                             # noqa: BLE001
        pass
    base = prov.get("base_url") or env["DEEPSEEK_BASE_URL"]
    key = prov.get("api_key") or env["DEEPSEEK_API_KEY"]
    model = resolve_model(base, key, prov.get("model") or env["DEEPSEEK_MODEL"])
    print("model=%s system=%d字" % (model, len(A.SYSTEM)))

    want = sys.argv[1] if len(sys.argv) > 1 else ""
    todo = [t for t in TOPICS if not want or want in t[0]]
    summary = []

    for name, idea in todo:
        brief = GIRL_ARCHIVE + "; " + LOOK + ".\n" + idea
        print("\n" + "=" * 66)
        print("话题：%s" % name)
        # 5 页大 JSON 偶尔会超过默认 300s（实测卡死一次），放宽到 600s；
        # generate 内部不传 timeout，只能在这一层改默认值。
        class SlowLLM(M.LLM):
            def __call__(self, system, user, max_tokens=8000, temperature=0.7,
                         timeout=600, tag=""):
                return super().__call__(system, user, max_tokens=max_tokens,
                                        temperature=temperature,
                                        timeout=timeout, tag=tag)

        class SlowLLM(M.LLM):
            last = ""

            def __call__(self, system, user, max_tokens=8000, temperature=0.7,
                         timeout=600, tag=""):
                r = super().__call__(system, user, max_tokens=max_tokens,
                                     temperature=temperature, timeout=timeout, tag=tag)
                SlowLLM.last = r or ""
                return r

        llm = SlowLLM(base, key, model)
        t0 = time.time()
        try:
            spec, report = A.generate(brief, 5, llm)
        except Exception as exc:                  # noqa: BLE001 - 要看到原始返回
            d = os.path.join(ROOT, "state", "_selfcheck",
                             "outfit_%s_%s_fail" % (time.strftime("%Y%m%d_%H%M%S"), name))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "raw_fail.txt"), "w", encoding="utf-8") as f:
                f.write(llm.last)
            print("失败：%s  原始返回 %d 字 → %s" % (exc, len(llm.last), d))
            print("尾部：" + repr(llm.last[-300:]))
            summary.append((name, time.time() - t0, False, None, ["生成失败"], [], d))
            continue
        secs = time.time() - t0

        d = os.path.join(ROOT, "state", "_selfcheck",
                         "outfit_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), name))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
        with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        with open(os.path.join(d, "brief.txt"), "w", encoding="utf-8") as f:
            f.write(brief)

        rows, pissues, pwarns = R.review(spec)
        with open(os.path.join(d, "prompt_review.txt"), "w", encoding="utf-8") as f:
            f.write(R.dump(rows, pissues, pwarns, os.path.join(d, "spec.json"), spec))

        lines, oissues = check_outfit(spec, name)
        print("耗时 %.1fs  体检%s  轮次 %s" % (
            secs, "通过" if report.get("pass") else "有问题",
            [len(r["issues"]) for r in report.get("rounds") or []]))
        print("骨架 " + " / ".join("%s(%d格)" % (p["layout"], p["grid"])
                                   for p in spec["pages"]))
        print("\n".join(lines))
        for m in report.get("script_issues") or []:
            print("  [体检] " + m)
        for m in oissues:
            print("  [衣] " + m)
        for lv, m in pissues:
            print("  [%s] %s" % (lv, m))
        summary.append((name, secs, report.get("pass"), spec, oissues, pissues, d))

    print("\n" + "=" * 66)
    print("汇总")
    for name, secs, ok, spec, oi, pi, d in summary:
        print(" %-4s %5.1fs 体检%-4s 衣着问题%d 提示词错%d  %s"
              % (name, secs, "过" if ok else "否", len(oi), len(pi), d))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

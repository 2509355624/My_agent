# -*- coding: utf-8 -*-
"""跑一遍真链路：moss_agent.generate（成稿 → 体检 → 重修 → 体检 → autofix）。

    python tools/moss_agent_e2e.py "女孩放学回家，写完作业下楼吃蛋糕" 5

产物落在 state/_selfcheck/agent_<时间>/：spec.json / report.json / preview.html
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.features.comic import moss_agent as A   # noqa: E402
from app.features.comic import moss_twopass as M  # noqa: E402


def resolve_model(base, key, model):
    import urllib.request
    if os.environ.get("MOSS_ONESHOT_MODEL"):
        return os.environ["MOSS_ONESHOT_MODEL"]
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=60) as r:
            ids = [m.get("id") for m in (json.loads(r.read().decode()).get("data") or [])]
        if model in ids:
            return model
        hit = [i for i in ids if i and i.startswith(model)]
        if hit:
            print("模型名对齐：%s → %s" % (model, hit[0]))
            return hit[0]
    except Exception as e:  # noqa: BLE001
        print("（列模型失败，按原名发：%s）" % e)
    return model


def main():
    brief = sys.argv[1] if len(sys.argv) > 1 else "女孩放学回家，写完作业后下楼吃蛋糕"
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    env = M.load_env(os.path.join(ROOT, ".env"))
    prov = {}
    try:
        with open(os.path.join(ROOT, "data", "custom_providers.json"), encoding="utf-8") as f:
            prov = (json.load(f).get("providers") or {}).get(env.get("LLM_PROVIDER")) or {}
    except Exception:  # noqa: BLE001
        pass
    base = prov.get("base_url") or env["DEEPSEEK_BASE_URL"]
    key = prov.get("api_key") or env["DEEPSEEK_API_KEY"]
    model = resolve_model(base, key, prov.get("model") or env["DEEPSEEK_MODEL"])
    print("provider=%s model=%s  system=%d 字" % (prov.get("label") or "deepseek", model, len(A.SYSTEM)))

    llm = M.LLM(base, key, model)
    spec, report = A.generate(brief, pages, llm)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    d = os.path.join(ROOT, "state", "_selfcheck", "agent_%s" % stamp)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
    with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print("\n=== 轮次 ===")
    for r in report["rounds"]:
        print(" 第%d轮 问题%d条 输出%d字" % (r["round"], len(r["issues"]), r["chars"]))
        for x in r["issues"]:
            print("    · " + x)
    print("总耗时 %.1fs  最终%s" % (report["secs"], "通过" if report["pass"] else "仍有问题"))
    for w in report.get("warns") or []:
        print("  (兜底) " + w)

    print("\n=== 每页 ===")
    for p in report["pages"]:
        print(" 第%d页 %s %d格  issues=%s  autofix=%s"
              % (p["n"], p["layout"], p["grid"],
                 "；".join(p["issues"]) or "0", "、".join(p["autofix"]) or "无"))

    # 转成预览格式（pages[].items 直接从 prompt_override 拆出来）
    prev = {"title": spec.get("title"), "pages": []}
    for p in spec["pages"]:
        ov = json.loads(p["prompt_override"])
        prev["pages"].append({"layout": p["layout"], "items": ov.get("items") or [],
                              "summary": ov.get("summary") or {}})
    pj = os.path.join(d, "preview_src.json")
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(prev, f, ensure_ascii=False, indent=1)
    print("\n" + d)
    return 0


if __name__ == "__main__":
    sys.exit(main())

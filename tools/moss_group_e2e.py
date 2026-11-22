# -*- coding: utf-8 -*-
"""群像模式真链路：多角色同屏合照。

    python tools/moss_group_e2e.py

用现有的 LLM 链路跑一遍 moss_agent.generate(group=True)，产物落在
state/_selfcheck/group_<时间>/。只写 spec / 提示词，**不出图**——
出图要走 5174 的队列（重启后在工作台点渲染）。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.features.comic import moss_agent as A   # noqa: E402
from app.features.comic import moss_twopass as M  # noqa: E402

# 四个角色：男孩（普通少年）、女孩（沿用之前的少女设定）、胖大叔、健身者。
# 只写外形不写衣服；体型/年龄/发型三项拉开，避免模型把他们画成同一张脸。
CAST = """群像角色：
boy: a teenage boy about thirteen, slim ordinary build, short neat black hair, round glasses, large dark eyes, an ordinary youthful face
girl: a girl about twelve, slender teen proportions, long legs, small head, long straight dark hair with soft blunt bangs pulled back by a wide fluffy pale cloth headband, large round dark eyes, round soft face, faint blush, a dark fabric scrunchie on her left wrist
chubby_uncle: a chubby middle-aged man in his forties, visibly round belly and broad soft face, thinning receding hair, small tired eyes, short stature relative to the others
athlete: a muscular young man in his early twenties, broad shoulders and heavily built arms, short cropped dark hair, square jaw, athletic powerful stance, noticeably taller than the boy"""

BRIEF = CAST + """
剧情：放学后的学校天台，这四个不同的人挤在一起拍一张大合照——健身者站在最中间把大家往里拢，胖大叔举着手机自拍，男孩和女孩并肩比着手势，四个人都笑着看镜头。
转折：按快门时一阵风把女孩的头带吹歪了，大家笑着停下来帮她扶正，重新挤好再拍。"""


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
    pages = 1  # 单页合照，用户要的就是一页里四个人同屏
    env = M.load_env(os.path.join(ROOT, ".env"))
    prov = {}
    try:
        with open(os.path.join(ROOT, "data", "custom_providers.json"),
                  encoding="utf-8") as f:
            prov = (json.load(f).get("providers") or {}).get(env.get("LLM_PROVIDER")) or {}
    except Exception:  # noqa: BLE001
        pass
    base = prov.get("base_url") or env["DEEPSEEK_BASE_URL"]
    key = prov.get("api_key") or env["DEEPSEEK_API_KEY"]
    model = resolve_model(base, key, prov.get("model") or env["DEEPSEEK_MODEL"])
    print("provider=%s model=%s  system(群像)=%d 字"
          % (prov.get("label") or "deepseek", model, len(A.SYSTEM + A.GROUP_OVERRIDE)))

    llm = M.LLM(base, key, model)
    spec, report = A.generate(BRIEF, pages, llm, group=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    d = os.path.join(ROOT, "state", "_selfcheck", "group_%s" % stamp)
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
    print("总耗时 %.1fs  最终%s" % (report["secs"],
                                  "通过" if report["pass"] else "仍有问题"))

    print("\n=== cast（4 个角色）===")
    for k, v in (spec.get("cast") or {}).items():
        print(" %s: %s" % (k, v[:90]))

    print("\n=== 每页 ===")
    for p in report["pages"]:
        print(" 第%d页 %s %d格  issues=%s  autofix=%s"
              % (p["n"], p["layout"], p["grid"],
                 "；".join(p["issues"]) or "0", "、".join(p["autofix"]) or "无"))

    print("\n=== 整页提示词（character 格）===")
    for p in spec["pages"]:
        ov = json.loads(p["prompt_override"])
        for it in ov.get("items") or []:
            if it.get("label") == "character":
                print(" [%s] %s" % (p["layout"], it.get("caption", "")[:400]))
                print()

    print("产物目录：%s" % d)
    return d


if __name__ == "__main__":
    main()

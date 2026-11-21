# -*- coding: utf-8 -*-
"""真走 5174 的换装压力测试（不是直连 LLM，是页面点「写剧本」那条路）。

`moss_outfit_probe.py` 是本地直接 import `moss_agent` 调的——改完提示词立刻
生效，验的是稿子本身。这个脚本 POST `/api/moss/two_agent`，验的是**跑着的
那个进程**里加载的提示词：改完代码不重启，这里就能看出是不是还在用旧稿。

产物落在 `state/_selfcheck/live_<话题>_<时间>/`：

    resp.json          接口原样返回（含每页 prompt_override）
    prompt_review.txt  文本层检查清单
    outfit.txt         衣着时间线 + 换装问题

用法：
    python tools/moss_live_probe.py             # 跑全部话题
    python tools/moss_live_probe.py 泳池         # 只跑一个
    python tools/moss_live_probe.py --port 5174
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load(name, fname):
    s = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", fname))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


P = _load("moss_outfit_probe", "moss_outfit_probe.py")   # 衣着时间线 + 判据
R = P.R                                                   # 提示词审查

# 和 moss_outfit_probe 同一份档案：里面那件连衣裙是压力源，不许出现在任何一格。
GIRL_ARCHIVE = P.GIRL_ARCHIVE
LOOK = P.LOOK

TOPICS = [
    ("泳池", "女生周六去学校游泳馆，路上穿浅灰色连帽卫衣和黑色运动长裤，背着泳包。"
             "到更衣室，她把卫衣和长裤脱下来叠好放进柜子，换上深蓝连体竞技泳衣、"
             "把头发塞进泳帽，走到池边做热身，最后跳进池里游到对岸扶着池边喘气。"),
    ("起床", "早上闹钟响，女生穿着印小图案的浅色睡衣从床上坐起来，揉着眼睛去卫生间洗脸。"
             "回房间脱掉睡衣，换上白衬衫和藏青背带裙校服，对着镜子系好领结，"
             "抓起书包出门。"),
    ("便利店", "傍晚下着小雨，女生穿白色短袖和牛仔短裤，撑一把透明伞走进便利店，"
               "在冷柜前挑了一瓶草莓牛奶，到收银台付钱，店员递给她找零，"
               "她笑着接过塞进兜里走出去。"),
]


def post(port, brief, pages):
    body = json.dumps({"brief": brief, "pages": pages}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%s/api/moss/two_agent" % port, data=body,
        headers={"Content-Type": "application/json"})
    # 服务端每次 LLM 调用 300s 上限、最多 3 轮 + 修，留足 20 分钟。
    # 显式空 ProxyHandler：会话里注入的 HTTP_PROXY 会把 127.0.0.1 也拐走。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=1200) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    port = "5174"
    args = [a for a in sys.argv[1:]]
    if "--port" in args:
        i = args.index("--port")
        port = args[i + 1]
        del args[i:i + 2]
    want = args[0] if args else ""
    todo = [t for t in TOPICS if not want or want in t[0]]
    summary = []

    for name, idea in todo:
        brief = GIRL_ARCHIVE + "; " + LOOK + ".\n" + idea
        print("\n" + "=" * 66)
        print("话题：%s（走 127.0.0.1:%s）" % (name, port))
        t0 = time.time()
        d = os.path.join(ROOT, "state", "_selfcheck",
                         "live_%s_%s" % (name, time.strftime("%Y%m%d_%H%M%S")))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "brief.txt"), "w", encoding="utf-8") as f:
            f.write(brief)
        try:
            out = post(port, brief, 5)
        except Exception as exc:                 # noqa: BLE001 - 要看到服务端原话
            msg = str(exc)
            body = ""
            if hasattr(exc, "read"):
                try:
                    body = exc.read().decode("utf-8", "replace")[:2000]
                except Exception:                # noqa: BLE001
                    pass
            print("请求失败：%s\n%s" % (msg, body))
            summary.append((name, time.time() - t0, "请求失败", None, [], []))
            continue
        secs = time.time() - t0
        with open(os.path.join(d, "resp.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        if not out.get("ok"):
            print("接口报错：%s" % out.get("error"))
            summary.append((name, secs, "接口报错", None, [], []))
            continue

        spec = {"cast": out.get("cast") or {}, "pages": out.get("pages") or []}
        rows, pissues, pwarns = R.review(spec)
        with open(os.path.join(d, "prompt_review.txt"), "w", encoding="utf-8") as f:
            f.write(R.dump(rows, pissues, pwarns, os.path.join(d, "resp.json"), spec))
        lines, oissues = P.check_outfit(spec, name)
        with open(os.path.join(d, "outfit.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines + ["", "衣着问题："] + oissues) + "\n")

        ta = out.get("two_agent") or {}
        print("任务 %s  耗时 %.1fs  引擎 %s  体检%s  轮次 %s" % (
            out.get("task"), secs, ta.get("engine"),
            "通过" if ta.get("pass") else "有问题",
            [len(r.get("issues") or []) for r in ta.get("rounds") or []]))
        print("骨架 " + " / ".join("%s(%d格)" % (p.get("layout"), len(p.get("panels") or []))
                                   for p in spec["pages"]))
        print("\n".join(lines))
        for m in ta.get("notes") or []:
            print("  [体检] " + m)
        for m in oissues:
            print("  [衣] " + m)
        for lv, m in pissues:
            print("  [%s] %s" % (lv, m))
        summary.append((name, secs, "体检" + ("过" if ta.get("pass") else "否"),
                        out.get("task"), oissues, pissues))

    print("\n" + "=" * 66)
    print("汇总")
    for name, secs, ok, task, oi, pi in summary:
        print(" %-5s %6.1fs %-8s 衣着问题 %d  提示词错 %d  %s"
              % (name, secs, ok, len(oi), len(pi), task or "-"))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

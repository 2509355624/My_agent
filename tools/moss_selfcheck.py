"""MOSS 自测：用**当前工作区代码**直接出一页图，绕开 5174。

为什么需要它（2026-11-21）：5174 里跑的是进程启动时加载的旧模块，改了提示词
必须重启才生效；而 AI 自查迭代不能每次都让用户手动重启 + 出图 + 截图。
这个脚本直接读最新代码生成提示词、直接 POST ComfyUI，用于「改完立刻看图」。

产物落在 `state/_selfcheck/`，不碰 `web/moss漫画/`（用户的正式出图目录）。

用法：
    python tools/moss_selfcheck.py <任务目录名> <页号> [text|soda] [steps]

    text = 模型自己画字（现行链路）
    soda = Soda 程序排字（MOSS_SODA_LETTER 那条逃生门）
"""
import glob
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.features.comic import moss_comic, soda_letter          # noqa: E402
from app.core.skills import load_workflow                       # noqa: E402
from app.tools.normal.generate_image import apply_prompt_placeholders  # noqa: E402

HOST = "http://127.0.0.1:8188"
SKILL = "moss-qunyou"
STEPS, CFG, WIDTH, HEIGHT = 30, 3.5, 1024, 1536
OUT_DIR = os.path.join(ROOT, "state", "_selfcheck")


def _node_id(wf, cls, field):
    for nid, node in wf.items():
        if node.get("class_type") == cls and field in (node.get("inputs") or {}):
            return nid
    raise RuntimeError("工作流里找不到 %s.%s" % (cls, field))


def build_graph(spec, page, mode, seed):
    prompt = moss_comic.build_page_prompt(spec, page, with_text=(mode == "text"))
    wf = load_workflow(os.path.join(ROOT, "skills", SKILL, "workflow.json"))
    if not wf:
        raise RuntimeError("读不到 skills/%s/workflow.json" % SKILL)
    lat = _node_id(wf, "EmptyFlux2LatentImage", "width")
    wf[lat]["inputs"]["width"], wf[lat]["inputs"]["height"] = WIDTH, HEIGHT
    ks = _node_id(wf, "KSampler", "steps")
    wf[ks]["inputs"]["steps"], wf[ks]["inputs"]["cfg"] = STEPS, CFG
    wf[_node_id(wf, "SaveImage", "filename_prefix")]["inputs"][
        "filename_prefix"] = "moss-selfcheck/selfcheck_%s_p%02d" % (mode, page.get("n"))
    if mode == "soda":
        info = soda_letter.page_info(spec, page)
        wf = soda_letter.rewire(wf, info)
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)


def submit(graph):
    body = json.dumps({"prompt": graph}).encode("utf-8")
    req = urllib.request.Request(HOST + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["prompt_id"]


def wait_and_fetch(pid, dest):
    q = urllib.parse.quote
    for _ in range(600):
        time.sleep(2)
        with urllib.request.urlopen("%s/history/%s" % (HOST, pid), timeout=10) as r:
            hist = json.loads(r.read())
        if pid not in hist:
            continue
        for msgs in (hist[pid].get("status") or {}).get("messages") or []:
            if isinstance(msgs, list) and msgs[0] == "execution_error":
                return None, "execution_error: %s" % (msgs[1:] and msgs[1] or "")
        for node_out in (hist[pid].get("outputs") or {}).values():
            for img in node_out.get("images") or []:
                url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                    HOST, q(img["filename"]), q(img.get("subfolder", "")),
                    q(img.get("type", "output")))
                urllib.request.urlretrieve(url, dest)
                return dest, ""
        return None, "跑完了但没找到图"
    return None, "timeout"


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    task, page_n = argv[1], int(argv[2])
    mode = (argv[3] if len(argv) > 3 else "text").lower()
    if mode not in ("text", "soda"):
        print("mode 只能是 text / soda")
        return 2
    global STEPS
    if len(argv) > 4:
        STEPS = max(1, min(int(argv[4]), 150))

    # 任务目录先按正式目录找，再退到 state/_selfcheck（agent_<时间> 形态的试跑产物）。
    d = os.path.join(ROOT, "state", "moss_comic", task)
    if not os.path.isdir(d):
        d = os.path.join(OUT_DIR, task)
    if not os.path.isdir(d):
        for base in (os.path.join(ROOT, "state", "moss_comic"), OUT_DIR):
            cands = [p for p in glob.glob(os.path.join(base, "*"))
                     if os.path.basename(p) == task]
            if cands:
                d = cands[0]
                break
    if not os.path.isdir(d):
        print("找不到任务目录: %s" % task)
        return 2
    spec = json.load(open(os.path.join(d, "spec.json"), encoding="utf-8"))
    page = next((p for p in spec.get("pages") or [] if p.get("n") == page_n), None)
    if not page:
        print("没有第 %s 页" % page_n)
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    dest = os.path.join(OUT_DIR, "%s_p%02d_%s.png" % (
        os.path.basename(d), page_n, mode))
    seed = 20261121
    graph = build_graph(spec, page, mode, seed)
    pid = submit(graph)
    print("submitted", pid, "->", dest)
    got, err = wait_and_fetch(pid, dest)
    if not got:
        print("FAILED:", err)
        return 1
    print("OK", got)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

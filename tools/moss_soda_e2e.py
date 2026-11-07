"""端到端验证：moss_comic spec → Soda 程序排字 → ComfyUI 真出一页。

走 `moss_api._draw` 同一条装配路（同一份工作流模板、同一组参数覆盖、
`apply_prompt_placeholders` 同一套替换），只是提交绕开 image_jobs 队列
（5174 还跑着旧代码），直接 POST ComfyUI /prompt。

用法： D:/AI/confyui_env/Scripts/python.exe tools/moss_soda_e2e.py
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, r"D:\AI\agent_my_test")
from app import moss_comic, soda_letter                     # noqa: E402
from app.skills import load_workflow                        # noqa: E402
from app.tools.normal.generate_image import apply_prompt_placeholders  # noqa: E402

HOST = "http://127.0.0.1:8188"
WF_PATH = r"D:\AI\agent_my_test\skills\moss-qunyou\workflow.json"
OUT = r"D:\AI\tmp\moss_soda_e2e.png"
STEPS, CFG, WIDTH, HEIGHT, SEED = 30, 3.5, 1024, 1536, 20261108

SPEC = {
    "title": "厨房试味",
    "style": "soft warm evening light through a kitchen window, gentle steam glow",
    "cast": {"woman": "an adult woman with long straight black hair, dark brown "
                      "eyes, wearing a cream knit cardigan over a thin camisole"},
    "pages": [{
        "n": 1, "layout": "1t2m2b",
        "scene": "a small cozy kitchen at dusk, steam rising from a pot on the stove",
        "panels": [
            {"desc": "A medium shot of {woman} lifting the pot lid, steam curling "
                     "past her face, she squints and smiles",
             "caption": "她掀开锅盖，热气扑了满脸。",
             "bubbles": ["好香。", "快点快点。"]},
            {"desc": "A close-up of the pot, rich broth bubbling with corn and "
                     "carrot pieces visible",
             "sfx": "咕嘟咕嘟", "bubbles": []},
            {"desc": "A close-up of {woman} tasting the broth from a small spoon, "
                     "one eyebrow raised",
             "bubble": "嗯，淡了一点。", "bubbles": []},
            {"desc": "A medium close-up of {woman} reaching for the salt jar on "
                     "the counter", "bubbles": []},
            {"desc": "An extreme close-up of her fingers sprinkling a pinch of "
                     "salt into the pot", "caption": "盐要一点一点地放。",
             "bubbles": []},
        ],
    }],
}


def _node_id(wf, cls, field):
    for nid, node in wf.items():
        if node.get("class_type") == cls and field in (node.get("inputs") or {}):
            return nid
    raise RuntimeError("找不到 %s.%s" % (cls, field))


def build_graph():
    page = SPEC["pages"][0]
    prompt = moss_comic.build_page_prompt(SPEC, page, with_text=False)
    info = soda_letter.page_info(SPEC, page)
    wf = load_workflow(WF_PATH)
    if not wf:
        raise RuntimeError("读不到 workflow.json")
    lat = _node_id(wf, "EmptyFlux2LatentImage", "width")
    wf[lat]["inputs"]["width"], wf[lat]["inputs"]["height"] = WIDTH, HEIGHT
    ks = _node_id(wf, "KSampler", "steps")
    wf[ks]["inputs"]["steps"], wf[ks]["inputs"]["cfg"] = STEPS, CFG
    save = _node_id(wf, "SaveImage", "filename_prefix")
    wf[save]["inputs"]["filename_prefix"] = "soda-e2e/moss_letter_test"
    wf = soda_letter.rewire(wf, info)
    # apply_prompt_placeholders 接 JSON 串、返回 dict
    return apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False),
                                     prompt, SEED)


def main():
    body = json.dumps({"prompt": build_graph()}).encode("utf-8")
    req = urllib.request.Request(HOST + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        pid = json.loads(r.read())["prompt_id"]
    print("submitted", pid)
    for _ in range(300):
        time.sleep(2)
        with urllib.request.urlopen("%s/history/%s" % (HOST, pid),
                                    timeout=10) as r:
            hist = json.loads(r.read())
        if pid not in hist:
            continue
        outs = hist[pid].get("outputs") or {}
        for node_out in outs.values():
            for img in node_out.get("images") or []:
                q = urllib.request.quote
                url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                    HOST, q(img["filename"]), q(img.get("subfolder", "")),
                    q(img.get("type", "output")))
                urllib.request.urlretrieve(url, OUT)
                print("saved", OUT, img["filename"])
                return 0
        if any(m.get("status") == "error"
               for m in hist[pid].get("status", {}).get("messages", [])
               if isinstance(m, list) and m[0] == "execution_error"):
            print("execution error")
            return 1
    print("timeout")
    return 1


if __name__ == "__main__":
    sys.exit(main())

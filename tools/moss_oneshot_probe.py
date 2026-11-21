# -*- coding: utf-8 -*-
"""MOSS 一次出整本：话题 → 完整 pages[] Layout JSON（单次请求）。

用途：验证「把编剧 + 逐页编译合成一次请求」到底行不行。
不写进 app，不碰 5174；只读复用 moss_twopass 的骨架表、画风锁、autofix、check_page。

    python tools/moss_oneshot_probe.py "女孩放学回家，写完作业下楼吃蛋糕" 5
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.features.comic import moss_twopass as M  # noqa: E402

OUT_DIR = os.path.join(ROOT, "state", "_selfcheck")

STYLE_LOCK = M.STYLE_LOCK
GIRL_TAIL = M.TAIL_GIRL_ONLY
BOY_TAIL = M.TAIL_WITH_BOY
NO_TAIL = M.TAIL_NO_TEXT
S1_SENT = M.LAYOUT_SENTENCE["S1"]
L6_SENT = M.LAYOUT_SENTENCE["L6"]
D1_SENT = M.LAYOUT_SENTENCE["D1"]

# ── 示例页：L6 五格，密度照这个来 ──
EXAMPLE = {
    "layout": "L6",
    "scene": "2",
    "summary": {
        "meta": ["comic", "manga", "screentone"],
        "canvas": "multi_panel",
        "caption": (
            L6_SENT + " " + STYLE_LOCK + " "
            "1girl, long black hair, blunt bangs, white headband, white dress with "
            "frilled collar and front buttons, white knee socks, black mary jane shoes, "
            "small school backpack. "
            "An apartment interior with a dining table, two wooden chairs, a cake box "
            "on the table, light from a side window. " + BOY_TAIL
        ),
    },
    "items": [
        {"label": "character", "bbox": [-750, -494, -8, -8], "caption": (
            "Wide shot, the girl coming down the last two steps of the staircase, one "
            "hand sliding along the metal handrail, skirt hem lifting with the step, "
            "face turned toward the dining table; the staircase wall behind her is a "
            "solid screentone mass. 1girl, long black hair, blunt bangs, white headband, "
            "white dress with frilled collar and front buttons, white knee socks, black "
            "mary jane shoes, small school backpack. monochrome greyscale screentone, "
            "bold black ink outline, flat shading, no color cast.")},
        {"label": "character", "bbox": [-750, 8, -8, 494], "caption": (
            "Medium close-up with head and shoulders in frame, the girl standing at the "
            "table, both hands resting on the closed cake box, chin dipped toward it, "
            "eyes on the lid; the side window rims her hair. 1girl, long black hair, "
            "blunt bangs, white headband, white dress with frilled collar and front "
            "buttons, white knee socks, black mary jane shoes, small school backpack. "
            "monochrome greyscale screentone, bold black ink outline, flat shading, "
            "no color cast.")},
        {"label": "text", "bbox": [-201, 256, -45, 460], "text": "哇，是草莓味。",
         "caption": (
            "Vertical rounded speech bubble with a thin black ink outline, Chinese text "
            "written vertically with each character stacked top to bottom, 1 column, "
            "short pointer tail pointing toward the girl's face, compact bubble sized "
            "so several can share this panel.")},
        {"label": "object", "bbox": [8, -494, 750, -172], "caption": (
            "Close shot of the tabletop: a closed white cake box with a paper ribbon, "
            "one fork laid beside it, the wooden grain of the table picked out in "
            "hatched shadow. No speech bubble and no text in this panel.")},
        {"label": "text", "bbox": [38, -471, 194, -314], "text": "作业本还摊在楼上桌上。",
         "caption": (
            "Small rectangular caption box in the upper left corner of this panel, "
            "Chinese text set vertically in columns read right to left, no bubble tail.")},
        {"label": "character", "bbox": [8, -160, 750, 160], "caption": (
            "Medium close-up with head and shoulders in frame, the girl lifting her head "
            "to look straight at the camera, mouth just opening, one strand of hair "
            "slipping off her cheek. 1girl, long black hair, blunt bangs, white headband, "
            "white dress with frilled collar and front buttons, white knee socks, black "
            "mary jane shoes, small school backpack. monochrome greyscale screentone, "
            "bold black ink outline, flat shading, no color cast.")},
        {"label": "text", "bbox": [38, 16, 201, 138], "text": "写完了就吃吧。",
         "caption": (
            "Boy plain text: bare Chinese dialogue overlaid directly on the panel, thick "
            "white outline around every character, NO speech bubble, no bubble tail, no "
            "pointer tail, no enclosing frame, Chinese text written vertically with each "
            "character stacked top to bottom, 1 column.")},
        {"label": "character", "bbox": [8, 172, 750, 494], "caption": (
            "Medium shot, the girl holding a small fork above the open cake, the fork "
            "hanging still in the air, the other hand keeping the lid tipped back, eyes "
            "on the slice. 1girl, long black hair, blunt bangs, white headband, white "
            "dress with frilled collar and front buttons, white knee socks, black mary "
            "jane shoes, small school backpack. monochrome greyscale screentone, bold "
            "black ink outline, flat shading, no color cast.")},
        {"label": "text", "bbox": [557, 316, 713, 471], "text": "等我先拍一张。",
         "caption": (
            "Vertical rounded speech bubble with a thin black ink outline, Chinese text "
            "written vertically with each character stacked top to bottom, 1 column, "
            "short pointer tail pointing toward the girl's face, compact bubble sized "
            "so several can share this panel.")},
    ],
    "bbox_coord_space": "abs_centralized",
    "bbox_format": "yxyx",
}

SYSTEM = """你一次写完整条漫：想清楚剧情，直接输出整本的 Layout JSON。输入是一句话题和一个页数，输出是一个 JSON 对象。
只输出 JSON，不要围栏、不要解释、不要在 JSON 前后写任何字。

════ 一、输出结构 ════
{
  "title": "标题，12 字以内",
  "cast": {"girl": "英文外形串"},
  "scenes": {"1": "英文场景串", "2": "英文场景串"},
  "pages": [
    {"layout": "L6", "scene": "2",
     "summary": {"meta": ["comic","manga","screentone"], "canvas": "multi_panel",
                 "caption": "英文长句，见第三节"},
     "items": [
       {"label": "character", "bbox": [...], "caption": "..."},
       {"label": "text", "bbox": [...], "text": "台词原文", "caption": "..."}
     ],
     "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}
  ]
}
canvas：这一页只有一格写 "single"，两格以上写 "multi_panel"。
bbox 一律 [y0, x0, y1, x1]，整数，画布 1024×1536（y ∈ [-768,768]，x ∈ [-512,512]）。

════ 二、剧情怎么走（在脑子里过一遍，不要输出） ════
1. 一句话：谁、在哪、想干什么、被什么挡住。
2. 五拍铺开：建场 → 诱发 → 升级 → 转折 → 收束。一拍一页；页数不够就把拍合并，不要把一个拍摊薄成两页。
3. 每页只发生一个完整的小回合：她做了什么 → 对方怎么应 → 她有什么变化。
4. 台词一句一个意思，十来个字；旁白交代时间、地点、她在想什么，一框一句。
5. 状态对账：上一页结尾她在哪、身上有什么、什么已经成了事实，下一页第一格必须还站在这儿。跨页只往前走，不回退、不重演。
6. 一页只有一个地点。

════ 三、summary.caption 的固定顺序 ════
① 分格句（按 layout 从下面原样抄一行）
S1: """ + S1_SENT + """
D1: """ + D1_SENT + """
L6: """ + L6_SENT + """
L0: """ + M.LAYOUT_SENTENCE["L0"] + """
② 画风锁（原样抄，一个字不改）
""" + STYLE_LOCK + """
③ 本页出现的人物外形串（从 cast 逐字抄，完整连续，不缩写、不调序），接着场景串（从 scenes 逐字抄）
④ 尾句（三选一，按本页实际有哪些人的话）
只有女生说话：""" + GIRL_TAIL + """
有男主画外话：""" + BOY_TAIL + """
整页没有任何文字：""" + NO_TAIL + """
caption 全英文，一个汉字都不许有。

════ 四、骨架（bbox 整组照抄，一个字符都不许改） ════
S1 · 满版一格（一页只装一个瞬间：一张脸、一双手、一个对视）
  [-768, -494, 768, 494]
D1 · 上下二格（同一空间里前后两个动作）
  [-768, -494, -8, 494]   [8, -494, 768, 494]
L6 · 五格，两上三下（信息密的一页，阅读顺序从上到下、从左到右）
  [-750, -494, -8, -8]    [-750, 8, -8, 494]
  [8, -494, 750, -172]    [8, -160, 750, 160]    [8, 172, 750, 494]
L0 · 五格，右通高主格 + 左栏三小 + 顶条
  [-768, -494, -692, 494]   [-692, -262, 754, 494]
  [-378, -494, 117, -272]   [130, -494, 540, -272]   [553, -494, 754, -272]

一个格子最多一条 character / object，bbox 必须正好等于骨架里的某一格，整组不许增删、不许重叠、不许跨骨架借格。
格号就是阅读顺序：格 1 是读者第一眼看到的那格（最上排最左），同一排从左到右数完再数下一排。
这一页只够一个瞬间就写 S1，够两个动作就写 D1，够五拍就写 L6，不要为了显得像漫画去填格子。

════ 五、items.caption 怎么写 ════
character：景别 + 动作 + 已完成的姿态 + 该角色整段外形串（原样连续粘贴）+ 结尾固定写
  monochrome greyscale screentone, bold black ink outline, flat shading, no color cast.
object（空镜 / 道具）：画面内容 + 结尾固定写 No speech bubble and no text in this panel.
text：只描述容器，台词内容只进 text 字段。
  女生的话（气泡）：Vertical rounded speech bubble with a thin black ink outline, Chinese text written vertically with each character stacked top to bottom, N columns, short pointer tail pointing toward the girl's face.
  男主画外的话（裸字，绝不画气泡）：Boy plain text: bare Chinese dialogue overlaid directly on the panel, thick white outline around every character, NO speech bubble, no bubble tail, no pointer tail, no enclosing frame, Chinese text written vertically with each character stacked top to bottom, N columns.
  旁白（方框）：Small rectangular caption box in the upper corner of this panel, Chinese text set vertically in columns read right to left, no bubble tail.
  拟声：Hand-lettered sound effect, no bubble.
每个 label=text 的 item 必须带 text 字段：该句原文，一字不改，不留空。
本页剧本里给了几句台词，就产出几条带 text 的 item，数量对得上。

════ 六、文字框 bbox（格子 [y0,x0,y1,x1]，h=y1-y0，w=x1-x0，算完取整） ════
气泡从下往上排，最多 4 条，每条高 0.21h、间隔 0.03h：
  第1条 [y0+0.74h, x1-0.49w, y0+0.95h, x1-0.07w]
  第2条 [y0+0.50h, x1-0.49w, y0+0.71h, x1-0.07w]
  第3条 [y0+0.26h, x1-0.49w, y0+0.47h, x1-0.07w]
  第4条 [y0+0.02h, x1-0.49w, y0+0.23h, x1-0.07w]
男主裸字贴右上（与气泡上下错开）：
  第1条 [y0+0.04h, x0+0.55w, y0+0.26h, x1-0.07w]
  第2条 [y0+0.28h, x0+0.55w, y0+0.48h, x1-0.07w]
  同格既有裸字又有气泡：裸字占上半，气泡只排最下两条。
旁白贴左上，往下叠，最多 4 框，每框高 0.21h、间隔 0.02h：
  第1框 [y0+0.04h, x0+0.07w, y0+0.25h, x0+0.56w]
  第2框 [y0+0.27h, x0+0.07w, y0+0.48h, x0+0.56w]
  第3框 [y0+0.50h, x0+0.07w, y0+0.71h, x0+0.56w]
  第4框 [y0+0.73h, x0+0.07w, y0+0.94h, x0+0.56w]
拟声居中：[y0+0.34h, x0+0.24w, y0+0.66h, x1-0.24w]
文字框离格边至少留 2%。气泡宽度约为格宽的 42%。
同一格多句：先说的在上、后说的往下排。

════ 七、画什么（第一人称 POV 硬约束） ════
镜头就是男主的眼睛，画面里只有女生（也可以有两三个女生同框）。
男主是镜头后的人，不入画：需要他的手时写 pov hands / disembodied hand，要他的声音时只写画外裸字。
人物镜头最紧到中近景：medium close-up with head and shoulders in frame。
正文不出现颜色词（red/blue/brown/pink/green/yellow/orange/purple），只写明暗、材质、网点。
衣物动作写成已完成的静态结果：her hand resting on the notebook。
双人同框时第二个人的描述开头显式互斥（In sharp contrast, the other girl has NO long hair, NO glasses），互斥只列外形与衣服，不写随身物件。
外形串里的东西以 cast 为唯一基准，衣服只能写已经发生的变化，不许新增。

════ 八、一页长这样（照这个密度写，不要缩水） ════
""" + json.dumps(EXAMPLE, ensure_ascii=False, indent=1) + """

现在把话题写成 pages。"""


def resolve_model(base, key, model):
    """配置里的短模型名 → 端点实际接受的 id（带日期后缀）。"""
    import urllib.request
    if os.environ.get("MOSS_ONESHOT_MODEL"):
        return os.environ["MOSS_ONESHOT_MODEL"]
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=20) as r:
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
    model = prov.get("model") or env["DEEPSEEK_MODEL"]
    # 中转站会把模型名加上日期后缀（deepseek-v4.1-flash → deepseek-v4.1-flash-1017），
    # 配置里记的短名直接发过去会 404，这里按前缀在 /models 里对一次。
    model = resolve_model(base, key, model)
    print("system %d 字" % len(SYSTEM))

    user = ("话题：%s\n\n写成 %d 页，%d 是上限——素材只够 %d 页就写 %d 页。"
            % (brief, pages, pages, pages, pages)
            + "\n每页按上面的规矩写好 layout / summary / items，一次输出整本。")

    llm = M.LLM(base, key, model)
    t0 = time.time()
    raw = llm(SYSTEM, user, max_tokens=16000, temperature=0.85, timeout=600, tag="oneshot")
    dt = time.time() - t0

    out_dir = OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(out_dir, "oneshot_%s_raw.txt" % stamp), "w", encoding="utf-8") as f:
        f.write(raw)

    print("\n耗时 %.1fs，输出 %d 字" % (dt, len(raw)))
    try:
        js = M.pick_json(raw)
    except Exception as e:  # noqa: BLE001
        print("!! 解析失败：%s" % e)
        print(raw[:1500])
        return 1

    with open(os.path.join(out_dir, "oneshot_%s.json" % stamp), "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=1)

    pgs = js.get("pages") or []
    print("title=%s  pages=%d" % (js.get("title"), len(pgs)))
    print("cast=%s" % json.dumps(js.get("cast") or {}, ensure_ascii=False))
    print("scenes=%d 条" % len(js.get("scenes") or {}))

    cast = js.get("cast") or {}
    for i, p in enumerate(pgs, 1):
        lay = p.get("layout") or ""
        items = p.get("items") or []
        chars = [x for x in items if x.get("label") in ("character", "object")]
        texts = [x for x in items if x.get("label") == "text"]
        print("\n── 第 %d 页 %s：%d 格 / %d 条文字"
              % (i, lay, len(chars), len(texts)))
        for t in texts:
            print("    「%s」" % (t.get("text") or "(空)"))
        # 拿现有校验器体检（copy 一份，不动原对象）
        pj = json.loads(json.dumps(p))
        try:
            notes = M.autofix(pj, cast, None, lay or None)
            issues = M.check_page(pj, i, cast, lay or None, None)
        except Exception as e:  # noqa: BLE001
            notes, issues = [], ["autofix 崩了：%s" % e]
        used = M.detect_layout(pj)
        print("    版式实际=%s  autofix=%s" % (used, "、".join(notes) or "无"))
        print("    校验=%s" % ("；".join(issues) if issues else "0 问题"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

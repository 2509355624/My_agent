"""moss_comic 分格 → Soda Storyboard 程序排字（page_info）。

链路（2026-11-08）：`build_page_prompt(with_text=False)` 出底图（MOSS 只画画面，
格子里声明 No speech bubble）→ 工作流 `SaveImage` 前插一个 `SodaLetterPage`
节点 → 它拿 page_info 在成图上画气泡 + 竖排中文。台词不再交给模型画，
根治气泡位置随机 / 贴边乱码（2026-11-01 起一直在跟这个问题搏斗）。

为什么手构 page_info，而不是走 bundle / import_moss：
- `import_moss` 把我们 `label: character` 的画面条目当物件框（它要求格子
  条目的 caption 带 panel/格 关键词），直接喂会丢掉全部格子；
- bundle 编译会按角色卡自动重写 caption，`build_page_prompt` 精心展开的
  画面描述（外形串、空格声明）会被覆盖；
- agent 进程跑在 my_env（soda-storyboard 只装在 confyui_env），代码里不能
  import soda_storyboard，只能按它的序列化 schema 手写 dict。

schema 对照 soda-storyboard 0.5.0：
- page_info = {"version": 1, "page": Page.to_dict()}（nodes/core.parse_info）；
- Page.text_font_size 默认 42、text_max_chars 默认 10——我们把 max_chars
  设 6：分列数 = ceil(字数/limit)，旁白框只有 0.23 格高，12 字全塞一列
  字号会缩到 ~9px 难读（实测）；6 字一列旁白 ~18px、气泡短句不受影响。
- 坐标全部左上原点（RectShape x/y/w/h），我们的 bbox 是中心原点 yxyx，
  `_to_tl` 负责换算。
"""

import json
import os

from app.features.comic import moss_comic

# 环境开关：**默认关**（2026-11-12 用户拍板恢复「模型自己画气泡」的老链路——
# Soda 程序排字会出几个大气泡糊脸）。MOSS_SODA_LETTER=1 显式开回程序排字，
# 作为逃生门，不用改代码。
_ENV = "MOSS_SODA_LETTER"


def enabled():
    return os.getenv(_ENV, "0") != "0"


def _to_tl(cell):
    """`[y0, x0, y1, x1]`（中心原点 yxyx）→ Soda 左上原点 rect dict。

    画布 1024×1536 与 `moss_comic._CANVAS_W/_H` 必须一致，偏移量写死在这。
    """
    y0, x0, y1, x1 = cell
    return {"type": "rect",
            "x": int(x0 + moss_comic._CANVAS_W / 2),
            "y": int(y0 + moss_comic._CANVAS_H / 2),
            "w": int(x1 - x0), "h": int(y1 - y0)}


def _clamp_narr(bb, max_w=330, max_h=232):
    """旁白框收一收：左上角不动，右下角往里收。

    `bbox_caption` 是按**格宽比例**算的（0.49w × 0.21h），S1 满版时约 500×322px，
    两句话的白底框直接盖住人脸（2026-11-21 实测「大气泡糊脸」）。排字这一层
    知道真实字数，按字数给框就够了：字多了 Soda 自己会缩字号、排多列。
    """
    y0, x0, y1, x1 = bb
    return [y0, x0, y0 + min(y1 - y0, max_h), x0 + min(x1 - x0, max_w)]


def _panel_of(bb, boxes):
    """文字 bbox（中心原点 yxyx）落在哪一格 → panel id。"""
    cy, cx = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
    best, bi = None, 0
    for i, c in enumerate(boxes):
        y0, x0, y1, x1 = c
        if y0 <= cy <= y1 and x0 <= cx <= x1:
            return "p%d" % (i + 1)
        d = abs((y0 + y1) / 2.0 - cy) + abs((x0 + x1) / 2.0 - cx)
        if best is None or d < best:
            best, bi = d, i
    return "p%d" % (bi + 1)


def _texts_from_override(page, boxes):
    """从双段链路的 `prompt_override` 里取文字条目（每条一句 + 精确 bbox）。

    2026-11-21：双段链路把文字钉进 override 的 text items——每条一句、bbox
    已算好；而 `panels[].caption` 是**多句拼成的一段**。Soda 原来只读 panels，
    于是「蛋糕是她惦记了一路的事。说这话时她没抬头…」两句被塞进同一个框、
    位置也和画面里的排版对不上（实测页 2）。有 override 就以它为准。
    """
    ov = page.get("prompt_override")
    if not isinstance(ov, str) or not ov.strip():
        return []
    try:
        d = json.loads(ov)
    except Exception:                          # noqa: BLE001  坏 JSON 退回旧路
        return []
    out = []
    for i, it in enumerate(d.get("items") or []):
        if it.get("label") != "text":
            continue
        txt = (it.get("text") or "").strip()
        bb = it.get("bbox")
        if not txt or not (isinstance(bb, list) and len(bb) == 4):
            continue
        cap = (it.get("caption") or "").lower()
        # 判定顺序有讲究：旁白的 caption 里也带 "bubble" 字样（`no bubble tail`），
        # 先认 "caption box" 才不会把旁白误判成气泡。
        if "sound effect" in cap or "katakana" in cap:
            kind, bubble, tid = "sfx", "shout", "sfx-ov%d" % i
        elif "caption box" in cap or "no bubble tail" in cap:
            kind, bubble, tid = "narration", "narration", "cap-ov%d" % i
        elif "speech bubble" in cap:
            kind, bubble, tid = "dialogue", "speech", "bub-ov%d" % i
        else:
            # narration = 白底方框（Soda 的 BUBBLE_TYPES 里唯一的旁白框型；
            # 旧代码传 "none" 不在枚举内，等于不画框，字直接压在画面上）
            kind, bubble, tid = "narration", "narration", "cap-ov%d" % i
        if kind == "narration":
            bb = _clamp_narr(bb)
        out.append({"id": tid, "text": txt, "kind": kind,
                    "panel_id": _panel_of(bb, boxes), "direction": "vertical",
                    "bubble": bubble, "bbox": _to_tl(bb)})
    return out


def page_info(spec, page):
    """一页 spec → SodaLetterPage 的 page_info JSON 字符串。

    只放排字需要的东西：格子矩形（气泡锚定 / 预览）、文字条目（内容、
    框、方向、气泡形状）。画面描述填进去是为了火柴人预览可看，排字不用。
    """
    lay = page.get("layout") or "single"
    boxes = moss_comic.LAYOUT_BOXES[lay]
    cast = spec.get("cast") or {}
    seen = set()
    panels, texts = [], []
    for i, panel in enumerate(page.get("panels") or []):
        cell = boxes[i] if i < len(boxes) else boxes[-1]
        pid = "p%d" % (i + 1)
        panels.append({
            "id": pid, "order": i + 1, "shape": _to_tl(cell),
            "description": moss_comic._fill_page(
                (panel.get("desc") or "").strip(), cast, seen),
        })
        # 旁白：贴左上，白底方框（bubble="narration"）。
        cap = (panel.get("caption") or "").strip()
        if cap:
            texts.append({"id": "cap-%d" % i, "text": cap, "kind": "narration",
                          "panel_id": pid, "direction": "vertical",
                          "bubble": "narration", "bbox": _to_tl(
                              moss_comic._text_bbox(cell, "caption"))})
        # 拟声：格中大字，shout 气泡（爆炸框）。
        sfx = (panel.get("sfx") or "").strip()
        if sfx:
            texts.append({"id": "sfx-%d" % i, "text": sfx, "kind": "sfx",
                          "panel_id": pid, "direction": "vertical",
                          "bubble": "shout", "bbox": _to_tl(
                              moss_comic._text_bbox(cell, "sfx"))})
        # 气泡：一至两条，speech 气泡；位置沿用 _text_bbox 的实测口径
        # （第 0 条贴右下、第 1 条叠上方，条间留缝）。
        for j, line in enumerate(moss_comic._bubbles(panel)):
            texts.append({"id": "bub-%d-%d" % (i, j), "text": line,
                          "kind": "dialogue", "panel_id": pid,
                          "direction": "vertical", "bubble": "speech",
                          "bbox": _to_tl(
                              moss_comic._text_bbox(cell, "bubble", j))})

    # 双段链路的 override 里有拆好的逐条文字 + 精确 bbox，优先用它；
    # 没有 override（老任务 / 单段链路）才退回上面从 panels 拼的那份。
    ov_texts = _texts_from_override(page, boxes)

    info = {
        "version": 1,
        "page": {
            "page_id": "page-%s" % page.get("n", 1),
            "canvas_width": moss_comic._CANVAS_W,
            "canvas_height": moss_comic._CANVAS_H,
            # 2026-11-21：中文竖排的列序改成从左往右（句首在最左列）。
            # 原来写 "rtl"（日式），读者会把第二列当句首，台词顺序整个反掉。
            "reading_direction": "ltr",
            "dialogue_mode": "lettering",      # letter_image 只排这个模式的字
            "text_direction": "vertical",
            "color_mode": "color",
            "text_font_size": 42,              # 1024 宽下的基准字号（Soda 默认）
            "text_max_chars": 6,               # 6 字一列，见文件头字号说明
            "panels": panels,
            "texts": ov_texts or texts,
        },
    }
    return json.dumps(info, ensure_ascii=False)


def rewire(wf, page_info_str):
    """往 API 格式工作流里插 SodaLetterPage，返回新工作流（副本）。

    接线：原 SaveImage 的图源 → SodaLetterPage.image；SaveImage 改接
    排字输出。节点 id 取现有最大数字 +1，不写死（`moss_api._node_id` 的
    教训：写死编号换渠道就是静默坏图）。
    """
    wf = json.loads(json.dumps(wf))
    save = next(k for k, v in wf.items() if v.get("class_type") == "SaveImage")
    src = wf[save]["inputs"]["images"]
    nid = str(max((int(k) for k in wf if str(k).isdigit()), default=0) + 1)
    wf[nid] = {"class_type": "SodaLetterPage",
               "inputs": {"image": src, "page_info": page_info_str}}
    wf[save]["inputs"]["images"] = [nid, 0]
    return wf

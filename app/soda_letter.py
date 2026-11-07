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

from app import moss_comic

# 环境开关：默认开（排字比让模型画字稳定得多），MOSS_SODA_LETTER=0 退回
# 老链路（MOSS 自己画气泡）——出问题时的逃生门，不用改代码。
_ENV = "MOSS_SODA_LETTER"


def enabled():
    return os.getenv(_ENV, "1") != "0"


def _to_tl(cell):
    """`[y0, x0, y1, x1]`（中心原点 yxyx）→ Soda 左上原点 rect dict。

    画布 1024×1536 与 `moss_comic._CANVAS_W/_H` 必须一致，偏移量写死在这。
    """
    y0, x0, y1, x1 = cell
    return {"type": "rect",
            "x": int(x0 + moss_comic._CANVAS_W / 2),
            "y": int(y0 + moss_comic._CANVAS_H / 2),
            "w": int(x1 - x0), "h": int(y1 - y0)}


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
        # 旁白：贴左上，无气泡。
        cap = (panel.get("caption") or "").strip()
        if cap:
            texts.append({"id": "cap-%d" % i, "text": cap, "kind": "narration",
                          "panel_id": pid, "direction": "vertical",
                          "bubble": "none", "bbox": _to_tl(
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

    info = {
        "version": 1,
        "page": {
            "page_id": "page-%s" % page.get("n", 1),
            "canvas_width": moss_comic._CANVAS_W,
            "canvas_height": moss_comic._CANVAS_H,
            "reading_direction": "rtl",
            "dialogue_mode": "lettering",      # letter_image 只排这个模式的字
            "text_direction": "vertical",
            "color_mode": "color",
            "text_font_size": 42,              # 1024 宽下的基准字号（Soda 默认）
            "text_max_chars": 6,               # 6 字一列，见文件头字号说明
            "panels": panels,
            "texts": texts,
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

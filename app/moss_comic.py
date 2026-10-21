"""moss 漫画 · 独立编剧（2026-10-21 用户拍板新建）。

**和 `comic_agent.py` 完全两套，不要互相 import。** 那边是 Anima 的
Danbooru tag 工程师（32.8 万标签库 + POV 判据 + 补正员），这边是自然语言编剧。
为什么能整段拆掉：

  · moss 的文本编码器是 Qwen3.5-9B，原生吃自然语言；工作流里连 LoRA 槽都是空的
    （`models/loras/` 没东西），`(tag:1.2)` 在它这儿只是普通文字，反而干扰。
  · moss 没有「漏了 1girl / 犯了 POV / 角色串味」这类硬规则，所以**不需要补正 AI**。
    唯一的硬约束是「这一页几格、每格方位写没写」——脚本自己数一下就知道。

契约（双层 JSON）：模型只写「这一格画什么」，**画风串/人物外形/场景串一个字都不用
抄**，全部由脚本按布局模板拼进去。这样一致性由代码保证，抄漏、抄跑、改一处忘一处
从根上没有，还省掉大量重复 token。

    {
      "title": "刀鞘",
      "style": "anime style, clean line art, ...",
      "cast":  {"woman": "…", "man": "…"},
      "pages": [
        {"n": 1, "layout": "single", "scene": "…",
         "panels": [{"desc": "…{woman}…", "bubble": "", "sfx": "", "caption": "…"}]}
      ]
    }
"""
import json
import re

from . import llm

# ── 版式：模板句 + 每格方位名 ─────────────────────────────────
LAYOUTS = {
    "single": (
        "A single full-page comic panel with no borders and no gutters.",
        [""],
        "整页一格：开场建立镜头、情绪定格",
    ),
    "2h": (
        "A comic page divided into exactly two horizontal panels of equal size, "
        "separated by a thin white gutter.",
        ["Top panel", "Bottom panel"],
        "上下两格：对话、一进一退",
    ),
    "3h": (
        "A comic page divided into exactly three horizontal panels of equal size, "
        "separated by thin white gutters.",
        ["Top panel", "Middle panel", "Bottom panel"],
        "上中下三格：三段式推进",
    ),
    "3t": (
        "A comic page divided into exactly three panels: one large wide panel filling "
        "the top half, and two smaller panels side by side on the bottom half, all "
        "separated by thin white gutters.",
        ["Top panel", "Bottom left panel", "Bottom right panel"],
        "上大下二小：一个大场面 + 两个细节特写",
    ),
    "2x2": (
        "A comic page divided into exactly four panels in a two by two grid, separated "
        "by thin white gutters.",
        ["Top left panel", "Top right panel", "Bottom left panel", "Bottom right panel"],
        "2×2 四格：快节奏连续动作",
    ),
}

# 画风预设（`--style` 可以直接传这些名字）
# ⚠️ 只放「画法 + 光照类型 + 色调 + 气质」，**不放具体环境**——写进
#    "dusk backlight / warm orange sky" 这种，雪夜客栈那页就会跟场景打架。
STYLES = {
    "wuxia": ("anime style, clean line art, flat cel shading, high contrast lighting, "
              "muted earthy tones, wuxia atmosphere"),
    "daily": ("anime style, clean line art, flat cel shading, bright soft daylight, "
              "vivid colors, comedic exaggerated expressions"),
    "suspense": ("anime style, clean line art, flat cel shading, cool desaturated tones, "
                 "deep shadows, quiet suspenseful atmosphere, film grain"),
    "soft": ("anime style, clean line art, flat cel shading, soft cinematic lighting, "
             "muted color palette"),
}

# 三种页面文字（全部拼在格里，moss 30 步实测零错字）
_BUBBLE = ("A speech bubble with clean handwritten Chinese characters reads 「%s」, "
           "placed close to the mouth of the character who is speaking.")
_SFX = "A hand-lettered sound effect reads 「%s」."
_CAPTION = ("A narrow rectangular caption box in the corner of the panel contains the "
            "handwritten Chinese text 「%s」.")
_NO_TEXT = "No speech bubble and no text anywhere on the page."

_PH_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ══════════════════════════════════════════════════════════
#  编剧提示词
# ══════════════════════════════════════════════════════════
_SYSTEM = """你是漫画编剧，也负责把每一页写成**生图模型能直接读懂的英文画面描述**。
你面对的是 MOSS-Image（文本编码器 Qwen3.5-9B），它吃**自然语言**，不吃 Danbooru 标签。

# 输出格式
只输出一个 JSON 对象，不要 markdown 围栏，不要任何解释文字：

{
  "title": "短标题",
  "style": "画风串（英文，见下）",
  "cast": {"角色名": "英文外形串", "另一个角色": "…"},
  "pages": [
    {"n": 1, "layout": "single", "scene": "英文场景串",
     "panels": [{"desc": "英文画面描述", "bubble": "", "sfx": "", "caption": ""}]}
  ]
}

# 字段规则

**style**：一句话概括全篇画风与色调，英文，15~30 词。例：
  anime style, clean line art, flat cel shading, dramatic dusk backlight, dust in the air, wuxia atmosphere
  不要写成形容词堆砌，要包含：画法 + 光照 + 色调 + 一个气质词。

**cast**：每个角色一条英文外形串，**只写不随场景变的属性**——性别年龄、发型发色、
脸型、常穿的衣服、固定配饰。**手持的、临时拿的道具绝对不要写进来**（写进来会
每页都长在手上）。key 用**英文小写单词**：woman / man / girl / boy / cat / old_man。例：
  "woman": "a young woman with long black hair tied up high, a white long-sleeved robe, a dark blue sash"

**scene**（每页一条）：这一页发生在哪，英文 15~40 词，写清地点、时间、光源、
两三个具体物件。

**panels**：格数必须和 layout 完全一致：
  single=1格 / 2h=2格 / 3h=3格 / 3t=3格 / 2x2=4格

**desc**（每格）：英文 25~60 词，一到三句，按这个顺序写：
  ① 谁在做什么（角色一律写 {角色名} 占位符，**不要重复写外形**）
  ② 景别：wide shot / medium shot / medium close-up / close-up / extreme close-up
  ③ 机位：eye level / from above / from below / from behind / over the shoulder
  ④ 一个能让画面对的东西（手的动作、视线方向、前景物件、光线落点）

**bubble**（对白气泡）：中文台词，**≤10 字**，像真人说话，不要书面语。
  每页最多 2 句。**台词不要说画面已经画出来的事**（画出来就是废话）。
**sfx**（拟声字）：中文 2~3 字，只在真的有动作声时写，每页最多 1 个。
**caption**（旁白框）：中文**≤10 字**，只在需要交代时间/地点/前情时写，每页最多 1 个。
  三个文字字段都没有内容就写空字符串 ""。

⚠️ 字数是有实测根据的：moss 画中文，**11 字以内基本零错字**（11 字竖排两行气泡
全对、9 字横排旁白全对），**12 字就开始出错**——实测「雪夜，客栈只剩一个客人。」
被写成「客栈一个剩个客人」，漏字 + 字序乱。所以宁短不长，能拆成两句就别写成一句。

# 画面描述的硬规则
1. **禁止 tag 语法**：不写 1girl / 2boys / from_above / score_7 / (smile:1.2)。
2. 人物一律用 {角色名} 占位符，外形由脚本替换。你不写外形。
   **占位符只在一个人物第一次出现时用**，同一页之后直接写 she / he / it，
   不要再写占位符（脚本兜底会把重复的占位符换成 the woman 这种，人名会变
   "the raiden"，很别扭）。
3. **不要写 {woman}'s 或 the hand of {woman}**——替换后会变成
   "…at her waist's hand" / "the hand of the raiden"，语法崩掉。
   所有格和身体部位直接写 her / his：her hand、his eyes、the eyes under the hat。
4. **一格只写一个瞬间**。**不要用 while / as / and then** 把两个动作（尤其两个
   不同角色的动作）塞进同一格——模型会画成一半一半。收尾格只画结果，不要在
   同一格里又交代前面的动作。
5. **这一格特有的道具必须写进这一格的 desc**（例如「她右手握着一把出鞘的长剑」）。
6. 景别和机位必须明说，不然模型会全画成中景。
7. 局部特写可以写，模型画得出来：眼睛、手、剑身反光、鞋踩进雪里。
8. 不要写真实人名、真实地名，角色名就用 cast 里的 key。
9. 不要每一格都写「看着对方」，镜头要动起来。

# 版式选择（配合叙事节奏）
  single —— 开场建立、结尾定格、一个很大的情绪画面
  2h     —— 一来一回的对话，上下两格最好是一问一答
  3t     —— 大场面推进 + 底下两个细节特写（手、眼睛、道具）
  2x2    —— 连续动作的四拍，节奏要快
  3h     —— 三段式推进

# 叙事要求
- 页与页之间必须往前走，**不要重复同一个构图**。
- 最后一页要收住，可以无字。
- 全篇是一个完整小故事，有起、有转折、有收。

# 示例（只示范格式和粒度，不要照抄内容）
{
  "title": "雨夜",
  "style": "anime style, clean line art, flat cel shading, cool blue night lighting, wet reflections, quiet mood",
  "cast": {"girl": "a girl with shoulder-length wavy brown hair, a mustard yellow knit scarf, an olive green duffel coat"},
  "pages": [
    {"n": 1, "layout": "2h", "scene": "the entrance of a small convenience store at night, heavy rain, wet asphalt reflecting warm light from inside",
     "panels": [
       {"desc": "{girl} stands outside the glass door holding an umbrella low, seen from behind in a medium wide shot at eye level, rain streaks crossing the frame and the bright window reflecting on the wet ground", "bubble": "", "sfx": "", "caption": ""},
       {"desc": "A close-up of her hand pushing the glass door open, water dripping from the umbrella tip, the warm light spilling onto her sleeve, shot from the side at waist height", "bubble": "", "sfx": "", "caption": ""}
     ]}
  ]
}
"""


def _user_brief(brief, pages, style, ratio, layout=""):
    lines = ["剧情要求：%s" % brief.strip(), ""]
    lines.append("总共 %d 页。" % pages)
    if layout:
        lay = LAYOUTS.get(layout)
        if lay:
            lines.append("**版式固定**：每一页都用 `%s`（%d 格）——%s。不要换成别的版式。"
                         % (layout, len(lay[1]), lay[2]))
            lines.append("这 %d 格必须是**连续的瞬间**：一格推进一拍，读完是一段完整的"
                         "小故事；收尾格只画结果，不要回头补前面的动作。" % len(lay[1]))
    if style:
        lines.append("画风固定用这一串（照抄进 style 字段）：%s" % style)
    lines.append("画面比例：竖版 %s。" % ratio)
    lines.append("")
    lines.append("按前面的 schema 直接输出 JSON。")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
#  JSON 解析 / 体检
# ══════════════════════════════════════════════════════════
def parse_json(text):
    """从模型回复里抠出第一个 JSON 对象。解析不了返回 (None, 原因)。"""
    if not text or not text.strip():
        return None, "空回复"
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return None, "回复里没有 JSON 对象"
    chunk = raw[i:j + 1]
    try:
        return json.loads(chunk), ""
    except Exception as exc:                    # noqa: BLE001
        # 常见毛病：尾逗号、中文引号
        fixed = re.sub(r",\s*([}\]])", r"\1", chunk).replace("“", '"').replace("”", '"')
        try:
            return json.loads(fixed), ""
        except Exception:                       # noqa: BLE001
            return None, "JSON 解析失败：%s" % exc


def _words(s):
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", s or ""))


def check(spec):
    """体检报告。**只报不拦**——判据全表也不该当准入闸门。"""
    issues = []
    if not isinstance(spec, dict):
        return ["不是 JSON 对象"]
    cast = spec.get("cast") or {}
    if not isinstance(cast, dict) or not cast:
        issues.append("cast 缺失或为空")
    if not (spec.get("style") or "").strip():
        issues.append("style 为空")
    pages = spec.get("pages") or []
    if not pages:
        issues.append("pages 为空")
        return issues
    for p in pages:
        n = p.get("n")
        lay = p.get("layout") or ""
        if lay not in LAYOUTS:
            issues.append("第 %s 页 layout「%s」不认识（可用 %s）"
                          % (n, lay, "/".join(LAYOUTS)))
            continue
        want = len(LAYOUTS[lay][1])
        panels = p.get("panels") or []
        if len(panels) != want:
            issues.append("第 %s 页 %s 要 %d 格，给了 %d 格"
                          % (n, lay, want, len(panels)))
        for k, panel in enumerate(panels, 1):
            desc = panel.get("desc") or ""
            w = _words(desc)
            if w < 12:
                issues.append("第 %s 页第 %d 格画面描述只有 %d 词，太短" % (n, k, w))
            elif w > 80:
                issues.append("第 %s 页第 %d 格画面描述 %d 词，偏长" % (n, k, w))
            for ph in _PH_RE.findall(desc):
                if ph not in cast:
                    issues.append("第 %s 页第 %d 格用了 {%s}，cast 里没有" % (n, k, ph))
            b = (panel.get("bubble") or "").strip()
            if len(b) > 11:
                issues.append("第 %s 页第 %d 格气泡「%s」%d 字，超 10 字（实测 12 字起会漏字乱序）"
                              % (n, k, b, len(b)))
            cap = (panel.get("caption") or "").strip()
            if len(cap) > 11:
                issues.append("第 %s 页第 %d 格旁白「%s」%d 字，超 10 字"
                              % (n, k, cap, len(cap)))
    return issues


# ══════════════════════════════════════════════════════════
#  拼装：JSON → 一整页的英文提示词
# ══════════════════════════════════════════════════════════
_NOUN_RE = re.compile(r"\b(woman|man|girl|boy|cat|creature|child|person|figure)\b")


def _pronoun(cast_val):
    """从外形串里挑一个能当指代的名词——`the raiden` 这种人名指代在英文里不成话。"""
    m = _NOUN_RE.search(cast_val or "")
    return ("the " + m.group(1)) if m else "the character"


def _fill_page(text, cast, seen):
    """同一页里，每个角色**只在第一次出现时展开完整外形**，之后用 `the woman` 这种指代。

    整页是一次生成的，同页共享上下文，重复写外形纯属浪费——实测第 3 页
    三个格把外形串抄了 5 遍，336 词里 175 词是重复。
    """
    def sub(m):
        key = m.group(1)
        val = cast.get(key)
        if not val:
            return m.group(0)
        if key in seen:
            return _pronoun(val)
        seen.add(key)
        return val if val.lower().startswith(("a ", "an ", "the ")) else "the " + val
    return _PH_RE.sub(sub, text or "")


def build_page_prompt(spec, page, with_text=True):
    """把一页 JSON 拼成生图提示词。布局句、画风、场景、人物串都在这里补上。"""
    lay = page.get("layout") or "single"
    lay_text, wheres, _ = LAYOUTS[lay]
    cast = spec.get("cast") or {}
    parts = [lay_text, (spec.get("style") or "").strip() + "."]

    scene = (page.get("scene") or "").strip()
    if scene:
        parts.append(scene + ".")

    seen = set()
    panels = page.get("panels") or []
    for i, panel in enumerate(panels):
        where = wheres[i] if i < len(wheres) else ""
        desc = _fill_page((panel.get("desc") or "").strip(), cast, seen)
        parts.append(("%s: %s" % (where, desc)) if where else desc)

        cap = (panel.get("caption") or "").strip()
        bub = (panel.get("bubble") or "").strip()
        sfx = (panel.get("sfx") or "").strip()
        if with_text:
            if cap:
                parts.append(_CAPTION % cap)
            if bub:
                parts.append(_BUBBLE % bub)
            if sfx:
                parts.append(_SFX % sfx)

    has_text = with_text and any((p.get("bubble") or p.get("sfx") or p.get("caption"))
                                 for p in panels)
    if not has_text:
        parts.append(_NO_TEXT)
    return " ".join(parts)


# ══════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════
def resolve_style(name):
    """`--style wuxia` → 长串；传原串则原样返回；空则空（让模型自己定）。"""
    key = (name or "").strip()
    if not key:
        return ""
    return STYLES.get(key.lower(), key)


def write(brief, pages=4, style="", ratio="1024x1536", provider=None,
          timeout=240, progress=None, layout=""):
    """一句话剧情 → 整篇 spec。返回 (spec, raw, error)。"""
    style_text = resolve_style(style)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _user_brief(brief, pages, style_text, ratio, layout)},
    ]
    if progress:
        progress("编剧开始")
    text = llm.call_llm(messages, timeout=timeout, provider=provider)
    raw = text or ""
    spec, why = parse_json(raw)
    if spec is None:
        return None, raw, why
    if style_text:                       # 预设画风以脚本为准，模型抄跑就改回来
        spec["style"] = style_text
    spec.setdefault("cast", {})
    spec.setdefault("pages", [])
    for i, p in enumerate(spec["pages"], 1):
        p["n"] = i
    return spec, raw, ""

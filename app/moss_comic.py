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
import os
import re

from . import llm
from .config import BASE_DIR

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
# 所有风格共用的**硬约束**（2026-10-28 实测后重写）。
#
# 只写 `flat cel shading` 是不够的：模型会自动把它升级成半写实厚涂——头发
# 出体积、皮肤出渐变、背景出景深（当天第一版端到端就是这么翻的）。必须同时
# 做三件事才压得住：
#   ① 写明「每个面只有两阶色」（`exactly two cel tones per surface`）；
#   ② 逐项封掉渐变 / 体积光 / AO / 侧逆光 / 景深（缺一项就会漏一项）；
#   ③ 收一句 `flat TV anime screencap look` 把整体拉回赛璐璐。
#
# ⚠️ **不许写 soft / hazy / dreamy / cinematic / glow / bokeh 这类氛围词**。
# 它们不是"修饰"，是直接对应「柔焦 + 湿画法」的视觉后果：8 步的采样预算足够
# 把柔焦画出来，却不够再收回去。原 `daily` 预设里的 `bright soft daylight`
# 就是当天画风翻车的直接原因。
# ── 提示词模板：照抄群友 `工作流MOSS-Image-SFT .json` 的 node 41 ──────────
#
# 群友的画风**只靠三个 meta tag + 7 个 LoRA 权重**，caption 里没有任何形容词
# 长串。2026-10-28 之前这里有一大段自创的 `_CEL_LOCK`（30 词锁平涂），实测
# 五次都收不住画风——因为它跟负向池里的 `lineart only / thick black outlines`
# 正面打架。按群友的做法：**画风交给 meta 和 LoRA，caption 只写内容**。
_META = ["comic", "anime_illustration", "cel_shading"]

# 群友 caption 的媒介句，他每张都以此开头
_MEDIUM = "A soft luminous 2D anime manga page"

# 群友 caption 的固定尾句：画法 + 光 + 「画面里有清晰中文」这一句。
# 最后那半句是他在 caption 里为字幕做的唯一铺垫（其余全在 items 里）。
_TAIL = ("Clean flat anime coloring with delicate cel shading, %s, "
         "crisp clean Chinese comic text in sharp speech bubbles.")

# 只放「光」。画风不在这里（已由 _META + LoRA 决定），所以这里不写任何
# 画法/媒介词，避免又变成自创的画风串。
STYLES = {
    "wuxia": "muted earthy tones with low dusk side light and dust in the air",
    "daily": "bright even daylight filling the room with a clean neutral tone",
    "suspense": "cool desaturated tone with hard-edged shadows and low key light",
    "soft": "muted pastel palette under even diffused light",
}

# 三种页面文字的排版模板。**群友的关键做法是明确「竖排 + 列数 + 有没有尾巴」**，
# 而不是只说"一行中文"：竖排中文每个字独立成形、不依赖连字排版，稳定性明显
# 高于横排；气泡尾巴的方向也写死，免得模型自己找地方放。
# 文字内容仍走 `items[].text`，caption 只描述「长什么样、在哪」。
_TXT_BUBBLE = ("Tall rounded oval speech bubble with a thin black outline and clean "
               "vertical Chinese dialogue text in %s, short pointer tail on its edge "
               "pointing toward the speaker's face.")
_TXT_CAPTION = ("Tall white rectangular narration box with a thin black outline and "
                "clean vertical Chinese text in %s (no bubble tail).")
_TXT_SFX = ("Hand-lettered Chinese sound effect drawn large inside this panel in clean "
            "vertical strokes.")
_NO_TEXT = "No speech bubble and no text anywhere on the page."


def _cols(text):
    """竖排列数。必须由代码给，让模型自己定就会串行、糊字。

    群友 50 步原图里 21 字用 3 列、16 字用 2 列。但那套列数是 8 步档时期定的：
    当时字形精度撑不住多列，11 字的「今天收到哥哥送的新衣服」按两列生成时，
    右列会变成「番昊唢顾您装腈」这种乱码，所以 16 字以内一律单列。

    ⚠️ 2026-10-28 晚渠道定档 **25 步**（8 步 / 50 步档已下架），文字精度比 8 步
    好得多（实测 21 字旁白单列全对），但**这里的列数阈值故意没跟着放宽**：
    放宽是排版美学选择，会影响每一页的观感，没实测就动等于拿成品冒险。
    真要改，先按 25 步跑几页对照再定。
    """
    n = len([c for c in (text or "") if not c.isspace()])
    if n <= 16:
        return "one column"
    if n <= 30:
        return "two columns"
    return "three columns"

# ── 画布几何（§15 `abs_centralized` / `yxyx`）─────────────────
# 1024×1536（与 `moss_api.WIDTH/HEIGHT` 必须一致），原点在画布中心，
# x 向右、y 向下 → x∈[-512,512]、y∈[-768,768]。
# 换尺寸必须同步改这两个数，否则 bbox 会整体偏移。
_CANVAS_W, _CANVAS_H = 1024, 1536
_HX, _HY = _CANVAS_W // 2, _CANVAS_H // 2
# 三分版的上下切点：按画布高度算，不写死（1536 时代写死过 -256/256）
_TQ = _HY // 3

# 每种版式的格子区域，顺序与 `LAYOUTS[lay][1]` 的方位名一一对应。
# 格式 [y_min, x_min, y_max, x_max]（= bbox_format "yxyx"，不是常见的 xyxy）。
LAYOUT_BOXES = {
    "single": [[-_HY, -_HX, _HY, _HX]],
    "2h": [[-_HY, -_HX, 0, _HX], [0, -_HX, _HY, _HX]],
    "3h": [[-_HY, -_HX, -_TQ, _HX], [-_TQ, -_HX, _TQ, _HX], [_TQ, -_HX, _HY, _HX]],
    "3t": [[-_HY, -_HX, 0, _HX], [0, -_HX, _HY, 0], [0, 0, _HY, _HX]],
    "2x2": [[-_HY, -_HX, 0, 0], [-_HY, 0, 0, _HX],
            [0, -_HX, _HY, 0], [0, 0, _HY, _HX]],
}

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

**style**：**只写光**，不写画法。英文 12~25 词，说清光源方向、色温、软硬。
画风（二次元赛璐璐）由系统固定的 meta tag + 7 个 LoRA 权重决定，**你不需要
也不许写任何画法词**——不要出现 `cel shading` / `lineart` / `二阶色` / `平涂`
这类描述，写了反而会和负向池打架。例：
  bright even daylight from a window on the left, clean neutral tone, one soft shadow on the floor
  warm indoor window light from the upper left with rich shadow depth
⚠️ 不要写 soft / hazy / dreamy / cinematic / glow / bokeh / film grain 这类氛围词，
它们会直接把画面拉成柔焦或厚涂。

**cast**：每个角色一条英文外形串，**只写不随场景变的属性**——性别年龄、发型发色、
脸型、常穿的衣服、固定配饰。**手持的、临时拿的道具绝对不要写进来**（写进来会
每页都长在手上）。key 用**英文小写单词**：woman / man / girl / boy / cat / old_man。例：
  "woman": "a young woman with long black hair tied up high, a white long-sleeved robe, a dark blue sash"

**scene**（每页一条）：这一页发生在哪，英文 15~40 词，写清地点、时间、光源、
两三个具体物件。

⚠️ **一页只能一个地点**。格与格之间是**同一空间里的连续瞬间**，不是过场剪辑。
剧情要换地点（浴室 → 厨房 → 玄关）就必须换页；一页里写
`… in the bathroom, then a sunny kitchen` 这种两个场景，模型会把两个地点
画进同一张图，四格全部串味（2026-10-28 实测：一句话剧本塞 4 格，出了
浴室+厨房混合背景）。页数给得不够时，宁可只画前几个地点，也不要跨地点压进一页。

**panels**：格数必须和 layout 完全一致：
  single=1格 / 2h=2格 / 3h=3格 / 3t=3格 / 2x2=4格

**desc**（每格）：英文 25~60 词，一到三句，按这个顺序写：
  ① 谁在做什么（角色一律写 {角色名} 占位符，**不要重复写外形**）
  ② 景别：wide shot / medium shot / medium close-up / close-up / extreme close-up
  ③ 机位：eye level / from above / from below / from behind / over the shoulder
  ④ 一个能让画面对的东西（手的动作、视线方向、前景物件、光线落点）

**bubble**（对白气泡）：中文台词，**≤10 字**，像真人说话，不要书面语。
  **每一页都必须给一句**，不许空着。**台词不要说画面已经画出来的事**（画出来就是废话）
  ——写她此刻在想什么、在抱怨什么、在催谁。
**caption**（旁白框）：中文**≤10 字**，交代时间、地点、前情或内心状态。
  **每一页都必须给一条**，和 bubble 同时出现，不许空着。
**sfx**（拟声字）：中文 2~3 字，只在真的有动作声时写，每页最多 1 个。
  能写就写（脚步、水声、翻页），没有就留空。sfx 不占 bubble/caption 的额度。
  旁白框固定在画面左上、气泡固定在右下，两者不会打架，所以都写也不挤。

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
  "style": "cool blue night light from the shop window on the right, wet reflections on the asphalt, hard shadow edges",
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


# ══════════════════════════════════════════════════════════
#  提示词规范注入（2026-10-28 起接 SKILL.md 全文）
# ══════════════════════════════════════════════════════════
# 事实源分级见 `skills/_moss_comic/CHARTER.md` §1：
#   · `skills/moss-image-prompt/SKILL.md` 管「一句话怎么写成模型能画准的
#     英文描述」（分块、语序、年龄锁定、防克隆、字幕字数）；
#   · 下面的 `_SYSTEM` 管「输出什么 JSON 结构」。
# 两者**互补，谁也替代不了谁**——SKILL.md 通篇没有一句讲 title/style/cast/pages，
# 而 `_SYSTEM` 也没讲分块和语序。所以系统提示是两份拼起来，不是二选一。
_SKILL_MD_PATH = os.path.join(BASE_DIR, "skills", "moss-image-prompt", "SKILL.md")
_SKILL_CACHE = {"mtime": 0.0, "text": ""}
_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n.*?\n\s*---\s*\n", re.S)

# 规范的「适用边界」声明，**必须放在规范正文之前**。
#
# 为什么需要：`SKILL.md` 是给「人手工写一张图的提示词」设计的编译 Agent 规范，
# 它的产出契约（Positive/Negative 一对 + 采样参数建议）、默认次元档（三次元
# 真人写实 + §7 毛孔基底 + 负面池封杀 `anime / 2d / cel shading`）、固定采样点
# （640×960 / 25 步 / cfg 3.5 / SLG 空）与我们这件事**正面冲突**：
# 编剧只写一句英文画面描述，画风串由 `_CEL_LOCK`/`STYLES` 注入，参数由
# `moss_api` 固定成 1024×1536 / 8 步 / cfg 3 / SLG 22。
#
# 2026-10-28 查证：注入后实测的 spec 里没有出现 photoreal / pore / film grain /
# 640 / 960（13 篇历史 spec 逐个扫过），说明 `_SYSTEM` 的优先级那句已经兜住了一部分。
# 但那是「没炸」，不是「不会炸」——下面这段把边界显式写死，不靠模型自己权衡。
#
# ⚠️ 本文本会走 `%` 格式化，**里面不能出现裸百分号**。
_SKILL_HEAD = """下面是一份 MOSS-Image 提示词规范，是所有画面描述的唯一写法依据。

┌────────────────────────────────────────────────────────────┐
│ 先读：本任务的适用边界（它决定下面五万字里哪些条款管你）        │
└────────────────────────────────────────────────────────────┘

这份规范是给「手工写一张图、并给出采样参数」的场景写的。**你的任务不是那个**：
你只写画面描述，不组装提示词、不碰画风、不碰参数。所以下列条款**不适用**：

· §0 的产出契约「Positive + Negative 一对，附采样参数说明」——你只输出本节
  要求的 JSON，不输出负向提示词，不提任何参数。
· §11 的 Positive/Negative 模板、§17 的 `<|...|>` 闭合标签外壳——你写的是
  `desc` 字段里一句连贯英文，不是拼接好的成品提示词。
· §0 默认的「三次元真人写实」档、§4 的 A/B/C 摄影档位（`fair skin`、
  `microscopic pores`、`amateur direct flash`、`35mm analog film grain` 那一套）
  ——画风由系统固定的 meta tag 和 7 个 LoRA 权重决定，你不需要也不许自己定。
· §7 的毛孔基底、§11 负面池里封杀 `anime, illustration, 2d, cel shading` 那组
  ——不要照搬，也不要反过来在描述里堆画风词。
· §14 的固定采样点（640×960 / 25 步 / cfg 3.5 / SLG 置空）——已换成
  1024×1536 / 8 步 / cfg 3 / SLG 22，由脚本写死。§14 的证据强度声明只是标注，
  不影响你要写的内容。

**三条铁律**：

1. `desc`、`scene`、`cast` 里**不许出现**画风词、媒介词、摄影设备词（如
   `photorealistic` / `film grain` / `35mm` / `shot on` / `cel shading` /
   `anime style` / `8k` / `high resolution`）。画风已经由 meta tag 和 LoRA 锁死，
   写进去只会和负向池打架。
   `style` 字段是唯一允许写的画风相关字段，但**它只写光**（光源方向、色温、软硬），
   不写画法词——同理，写画法会和负向池里的 `lineart only` / `thick black outlines`
   互相拉扯，实测会让画风在几种风格之间摇摆。
2. **不许提任何采样参数**：分辨率、步数、CFG、SLG、seed、画质等级，一个都不要。
   也不要写「高清」「精细」「超清」这类画质暗示词——它们不改变采样，只会干扰构图。
3. §17 的**分块思想仍然有效**：同一个角色的方位、发色、瞳色、服装、配饰、手持
   道具必须全部写在**同一条** `desc` 里，不许拆到不同的 `item`；拆开会导致颜色、
   服装在画面里交叉错位。

**仍然适用的部分**（这才是你写 `desc` 的主要依据）：
§2 词条清洗、§5 结构分流、§8 多人防克隆、§9 画面内文字、§11 自检清单的第
2/5/6/7 条（衣物写成静态结果、多人互斥发色、液体描述、删掉器官与半裸词）、
§15 Layout JSON schema。

────────────────────────────────────────────────────────────
以下是规范全文：
────────────────────────────────────────────────────────────

%s

════════════════════════════════════════════════════════════
以上是规范。以下是你的任务和输出格式：
规范决定「一句话怎么写」，本节决定「输出什么结构」。冲突时以本节的结构要求为准。
════════════════════════════════════════════════════════════

"""


def skill_md():
    """读 moss 提示词规范全文（去 frontmatter），按 mtime 缓存。

    每次现读磁盘、而不是 import 时读一次：这份文件是**唯一事实源**，改它
    不该要求重启 flask。mtime 没变就走缓存——5 万字符，重复读+正则没必要。

    读不到返回空串，调用方退回只走 `_SYSTEM`，不阻断编剧。
    """
    try:
        st = os.stat(_SKILL_MD_PATH)
    except OSError:
        return ""
    if _SKILL_CACHE["mtime"] == st.st_mtime:
        return _SKILL_CACHE["text"]
    try:
        with open(_SKILL_MD_PATH, encoding="utf-8") as fh:
            text = _FRONTMATTER_RE.sub("", fh.read()).strip()
    except OSError:
        return _SKILL_CACHE["text"]
    _SKILL_CACHE.update(mtime=st.st_mtime, text=text)
    return text


def system_prompt():
    """系统提示 = SKILL.md 全文 + 内置任务与格式段（理由见本段顶部）。

    用拼接而不是 `%` 模板：`_SYSTEM` 里全是 `{...}` 的 JSON 示例，套进格式化
    会被当成占位符吃掉。
    """
    body = skill_md()
    if not body:
        return _SYSTEM
    return (_SKILL_HEAD % body) + _SYSTEM


def _user_brief(brief, pages, style, ratio, layout=""):
    lines = ["剧情要求：%s" % brief.strip(), ""]
    lines.append("总共 %d 页。" % pages)
    lines.append("**一页只画一个地点**：剧情跨了几个地点就需要几页。页数不够时"
                 "只画前面能画完的地点，不要把两个地点塞进同一页——模型会把它们"
                 "画进同一张图，整页串味。")
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
    # 每页两种文字都要有：这是版面要求，不是叙事需要。放在 user 段而不是
    # system 段，是因为 system 里有五万字的 SKILL.md，规则容易被稀释。
    lines.append("**每页的文字**：`caption`（旁白，≤10 字，交代时间/地点/状态）和 "
                 "`bubble`（台词，≤10 字）**两条都必须写，都不许空**。"
                 "旁白框钉在左上、气泡钉在右下，位置由脚本给定，两者不会互相挤压。"
                 "`sfx` 有动作声就写，没有留空。")
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


def _text_bbox(cell, kind):
    """在一格内部给文字框定位（同样是 yxyx）。

    位置按实测收敛：旁白贴左上、气泡贴右下、拟声放中间。**box 不贴画面边缘**
    —— 贴边实测会生出前缀乱码（「陋升郑凯出门啦。」这种）。
    """
    y0, x0, y1, x1 = cell
    h, w = y1 - y0, x1 - x0
    if kind == "caption":
        return [int(y0 + h * 0.05), int(x0 + w * 0.07),
                int(y0 + h * 0.23), int(x0 + w * 0.56)]
    if kind == "bubble":
        return [int(y1 - h * 0.27), int(x1 - w * 0.49),
                int(y1 - h * 0.06), int(x1 - w * 0.07)]
    return [int(y0 + h * 0.34), int(x0 + w * 0.24),
            int(y0 + h * 0.66), int(x1 - w * 0.24)]


def build_page_prompt(spec, page, with_text=True):
    """把一页 JSON 编译成 §15 官方 Layout JSON **字符串**。

    为什么不用散文（2026-10-28 改）：散文只有一个整页描述，格怎么分、气泡
    放哪全靠模型自己决定，实测结果是气泡位置随机、旁白贴边生乱码。JSON 能
    逐个元素指定 bbox，这是唯一能把「旁白左上、气泡右下」钉死的办法。

    输出是一条字符串（不是 dict）：调用方要把它塞进 workflow 的
    `__MULTI_PROMPTS__`，那边本来就按文本替换。
    """
    lay = page.get("layout") or "single"
    lay_text = LAYOUTS[lay][0]                 # 分格句
    boxes = LAYOUT_BOXES[lay]
    cast = spec.get("cast") or {}

    # summary.caption 的顺序**照群友 node 41**：
    #
    #   媒介句 →（多格才有的分格句）→ 第一格内容 → 场景 → 尾句（画法+光+文字）
    #
    # 群友的写法里没有"画风串"：`A soft luminous 2D anime manga page` 一句就是
    # 全部画风信息，剩下的交给 `meta` 三个 tag 和 7 个 LoRA。人物描述必须紧跟
    # 媒介句——中间插任何概括句，模型都会丢掉二次元定性（10-28 实测）。
    cap = [_MEDIUM + ","]
    if lay != "single":
        cap.append(lay_text)

    scene = (page.get("scene") or "").strip()
    light = (spec.get("style") or "").strip() or STYLES["daily"]

    panels = page.get("panels") or []
    if panels:
        # 独立的 seen：summary 这一份要完整展开外形，别把 items 那份的
        # 首次出现额度用掉（否则 items 里第一个出现的角色变成 "the girl"）。
        first = _fill_page((panels[0].get("desc") or "").strip(), cast, set())
        if first:
            cap.append(first.rstrip(" .,") + ".")
    if scene:
        cap.append(scene.rstrip(" .,") + ".")
    cap.append(_TAIL % light)

    seen = set()
    items = []
    has_text = False
    for i, panel in enumerate(panels):
        cell = boxes[i] if i < len(boxes) else boxes[-1]
        desc = _fill_page((panel.get("desc") or "").strip(), cast, seen)
        items.append({
            "label": "character",
            "caption": desc,
            "bbox": list(cell),
        })
        if not with_text:
            continue
        for key, tpl in (("caption", _TXT_CAPTION), ("bubble", _TXT_BUBBLE),
                         ("sfx", _TXT_SFX)):
            text = (panel.get(key) or "").strip()
            if not text:
                continue
            has_text = True
            # 旁白/气泡要带列数，拟声字是单个大字不需要
            cap_text = (tpl % _cols(text)) if "%s" in tpl else tpl
            items.append({
                "label": "text",
                "caption": cap_text,
                "text": text,
                "bbox": _text_bbox(cell, key),
            })

    if not has_text:
        cap.append(_NO_TEXT)

    summary = {
        "meta": list(_META),
        "canvas": "single" if lay == "single" else "multi_panel",
        "caption": " ".join(cap),
    }
    # 群友模板里有 palette（5 个十六进制色），用于把整页配色钉住。
    # 编剧给就用，不给就不写这个字段——色值不自己编。
    pal = spec.get("palette")
    if isinstance(pal, list) and pal:
        summary["palette"] = [str(c) for c in pal]

    return json.dumps({
        "summary": summary,
        "items": items,
        "bbox_coord_space": "abs_centralized",
        "bbox_format": "yxyx",
    }, ensure_ascii=False)


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
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": _user_brief(brief, pages, style_text, ratio, layout)},
    ]
    if progress:
        progress("编剧开始")
    # 输出预算按页数给：接了 SKILL.md 之后每格 desc 会写得更细（规范要求
    # desc 25~60 词、scene 15~40 词），一页 2×2 四格的 JSON 约 800 token。
    # 不显式开口子就会被服务端默认上限截在半句话上 → JSON 解析直接失败，
    # 白烧一次调用。同 `comic_story.write_story` 的口径。
    budget = max(4096, int(pages) * 900)
    text = llm.call_llm(messages, timeout=timeout, provider=provider,
                        max_tokens=budget)
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

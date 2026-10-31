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
    # 2026-10-31：moss 小漫画频道要「单页讲完一个小故事」。
    # 为什么是 3×2 而不是 5 格：5 格只能写成「上一下四」或「二上三下」，
    # 版式句说不准，模型会自己乱分格，格子边界一歪气泡就压线上；3×2 是规整
    # 网格，一句话能钉死。代价是每格宽度从 512 掉到 341（高度 768 没变），
    # 气泡/旁白的 bbox 是按格内比例算的，会自动跟着缩。
    "3x2": (
        "A comic page divided into exactly six panels in a two by three grid, three "
        "equal panels in the top row and three equal panels in the bottom row, all "
        "separated by thin white gutters.",
        ["Top left panel", "Top middle panel", "Top right panel",
         "Bottom left panel", "Bottom middle panel", "Bottom right panel"],
        "3×2 六格：单页讲完一段小剧情（起承转合 + 两个反应）",
    ),
    # 2026-10-31：五格「上三下二」。
    #
    # 为什么不是别的五格排法：`上一下四` 下排每格只有 256×1152（宽高比 0.22，
    # 比 3×2 更窄）；`二上三下` 上排是 512×512 方格，人像在方格里难放。
    # 只有「上三下二」既保住上排的推进节奏，又让下排两格宽到 512（宽高比
    # 0.67，接近 3:4 的正常人像比例），天然形成「小格堆节奏 + 大格砸高潮」。
    #
    # 版式句能钉死：行内全部等宽，只是两行列数不同，模型对「3 等分 / 2 等分」
    # 这种规整描述服从度高，不像不规则拼贴会自己乱分。
    "3t2b": (
        "A comic page divided into exactly five panels: three equal narrow panels "
        "in the top row and two equal wider panels in the bottom row, all separated "
        "by thin white gutters.",
        ["Top left panel", "Top middle panel", "Top right panel",
         "Bottom left panel", "Bottom right panel"],
        "上三下二五格：上排三格快速推进（脸与局部），下排两个大格放高潮与收尾",
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

# 2026-10-31：整页无文字时**必须换尾句**。
# 原来无条件用 `_TAIL`（说「有清晰中文气泡」），后面再追加 `_NO_TEXT`
# （说「页面上任何地方都没有文字」），同一段 caption 里两句互相打脸，
# 模型择一执行——这是「空格子被补出台词」的一个直接诱因，不是只有 bbox 软约束。
_TAIL_NOTE = ("Clean flat anime coloring with delicate cel shading, %s, "
              "no text and no speech bubbles anywhere on the page.")

# 单格无文字时拼进该格 caption 尾部的硬声明（逐格用，不是整页）。
# 注意前导空格：拼接时是 `body + "." + _NO_TEXT_CELL`，少了这个空格会变成
# `…covering her body.No speech bubble…`（2026-10-31 首跑实测就是粘连的）。
_NO_TEXT_CELL = " No speech bubble and no text in this panel."

# 模型会**误用**这条规则：在明明有气泡的格子里也自己写一句
# `no speech bubble, no text in this panel`（2026-10-31 第二跑实测，第 2 格
# 有气泡「就是现在」却写着 no text）。这类自写的否定句会和真实存在的文字元素
# 打架，所以有字的格子要把它们清掉，没字的格子才保留/追加。
_NO_TXT_CLAUSE = re.compile(
    r"\s*[,.;]?\s*(?:no speech bubbles?(?:,?\s*no text)?|no text)(?:\s+in this panel)?",
    re.I)

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


def _thirds():
    """横向三等分的两个切点（画布中心为原点）。

    单独算成函数而不是写死 `-171 / 170`：换尺寸时 `_HX` 变了，这里跟着走，
    不用再去改一串魔数。1024 宽下是 -171 / 170（三段 341/341/342）。
    """
    step = (2 * _HX) // 3
    a = -_HX + step
    return a, a + step


_C3A, _C3B = _thirds()

LAYOUT_BOXES["3x2"] = [
    [-_HY, -_HX, 0, _C3A], [-_HY, _C3A, 0, _C3B], [-_HY, _C3B, 0, _HX],
    [0, -_HX, _HY, _C3A], [0, _C3A, _HY, _C3B], [0, _C3B, _HY, _HX],
]

# 五格「上三下二」：上排沿用横向三等分，下排横向二等分（每格 512 宽）。
LAYOUT_BOXES["3t2b"] = [
    [-_HY, -_HX, 0, _C3A], [-_HY, _C3A, 0, _C3B], [-_HY, _C3B, 0, _HX],
    [0, -_HX, _HY, 0], [0, 0, _HY, _HX],
]

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
    {"n": 1, "layout": "3t2b",
     "plan": {"key_panel": 4,
              "shots": ["close-up", "medium shot", "close-up", "wide shot", "wide shot"]},
     "scene": "英文场景串",
     "panels": [{"desc": "英文画面描述", "bubble": "", "sfx": "", "caption": ""}]}
  ]
}

# 字段规则

**style**：**只写光**，不写画法。**必须用英文写**，一个中文字都不要（这条链路的
positive 提示词是英文的，中途夹一段中文会让光照描述被截断）。英文 12~25 词，
说清光源方向、色温、软硬。
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

**plan**（每页一条，**必须写在 scene 和 panels 之前**）：
  先想清楚整页怎么走，再动笔写每一格。这是防止「六格全画中景、整页平淡」的唯一手段——
  逐格局部决策必然产出一模一样的构图。三个键：
  - `key_panel`：这一页的**视觉重心**是第几格。必须是格数最大的那一格
    （3t2b 是第 4 或第 5 格；3x2 任选；2x2 任选）。整页最贵的那个画面放这里。
  - `shots`：每格的景别，英文，**长度必须等于格数**，按顺序对应。
    相邻两格不许同景别，整页至少三档（close-up / medium shot / wide shot）。
  - `state`：这一页的**穿着与状态轨迹**，一句话说清哪几格什么状态、从第几格起变化。
    例：`panels 1-3 wearing the black feathered gown; panels 4-5 completely nude,
    the gown lying on the stage floor behind her`。
    这是治「上一格脱了、下一格又穿回去」的唯一手段——`cast` 里写的是常穿的衣服，
    模型每格都会默认把它画回来，必须靠这句轨迹压住。
  写完 plan 之后，下面每一格的 `desc` 必须**按 shots 定的景别、按 state 定的状态执行**，
  不许临时改。

**scene**（每页一条）：这一页发生在哪，英文 15~40 词，写清地点、时间、光源、
两三个具体物件。

⚠️ **一页只能一个地点**。格与格之间是**同一空间里的连续瞬间**，不是过场剪辑。
剧情要换地点（浴室 → 厨房 → 玄关）就必须换页；一页里写
`… in the bathroom, then a sunny kitchen` 这种两个场景，模型会把两个地点
画进同一张图，四格全部串味（2026-10-28 实测：一句话剧本塞 4 格，出了
浴室+厨房混合背景）。页数给得不够时，宁可只画前几个地点，也不要跨地点压进一页。

**panels**：格数必须和 layout 完全一致：
  single=1格 / 2h=2格 / 3h=3格 / 3t=3格 / 2x2=4格 / 3x2=6格 / 3t2b=5格

**desc**（每格）：英文 25~60 词，一到三句，按这个顺序写：
  ① **景别，直接放在第一句开头**：`A close-up of …` / `A wide shot of …` /
     `A medium shot of …`。景别写在句子后半截，模型经常只取前半句，
     最后整页变成同一个景别——这是整页平的直接原因。
  ② 谁在做什么（角色一律写 {角色名} 占位符，**不要重复写外形**）
  ③ 机位：eye level / from above / from below / from behind / over the shoulder
  ④ 一个能让画面对的东西（手的动作、视线方向、前景物件、光线落点）
  ⑤ **该角色的固定锚点，最多 6 词复述一次**：发色 + 瞳色 + 一个配饰。
     每一格都要写，不能只在第一格写。六格共用一个 latent，不重复写锚点，
     第 2 格开始头发颜色、瞳色、配饰就会各自漂移。
  ⑥ **这一格最容易被画错的东西，用一句 no X covering Y 反向钉住**。例：
     `no hands or hair covering her face`；`no speech bubble, no text in this panel`；
     `no other hands in frame`。反向钉住比正面多写十个形容词有效。

**把过程写成结果**：衣物、动作、姿态一律写成**已经完成的状态**，不用过程动词。
写 `her cardigan already slipped off one shoulder`，不写 `she pulls her cardigan down`
——过程动词会让同一格里的手数量失控，长出第三只手。

**bubble**（对白气泡）：中文台词，**≤10 字**，像真人说话，不要书面语。
  **台词不要说画面已经画出来的事**（画出来就是废话）——写她此刻在想什么、
  在抱怨什么、在催谁。
**caption**（旁白框）：中文**≤10 字**，交代时间、地点、前情或内心状态。
**sfx**（拟声字）：中文 2~3 字，只在真的有动作声时写，每页最多 1 个。
  能写就写（脚步、水声、翻页），没有就留空。sfx 不占 bubble/caption 的额度。
  旁白框固定在画面左上、气泡固定在右下，两者不会打架，所以都写也不挤。

**每页的排版节奏**：一页里**至少有一格有字**（caption 或 bubble 至少一条），
不要整页空白。但**允许单格无字**——收尾定格、纯视觉冲击的那一格留空更有力。
留空的那一格，编译时会自动追加 `No speech bubble and no text in this panel`，
你不用自己写。**你绝对不要在 `desc` 里自己写这句话**——只有真的没有文字的格子
才该有它，写进有气泡的格子会和气泡打架，气泡会被挤掉或者位置乱跑。
也**不要为了填满硬凑一句废话**。
⚠️ 别连着空两格以上：模型对空格的服从度靠这句声明维持，连续空格会失效。

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
10. **状态必须跨格延续**。任何一格改掉了角色的状态（脱下衣服、衣服落地、头发湿透、
    衣服被扯开、跌倒后沾了灰），**后面每一格都要用一句短语重申当前状态**
    （`still completely nude`、`her coat still lying on the floor behind her`）。
    不重申，模型会退回 `cast` 里那套默认服装——实测出现过「上一格全裸、下一格
    又穿回原来的短裙套装」，读者直接出戏。
11. **同一页里相邻的两格不要写同一句台词或旁白**。实测第 5、6 格都写「台下只剩光点」，
    两格并排出现一模一样的字，观感上等于失误。每格的 `bubble` / `caption` 都要是
    不同的句子。
12. **一页必须有视觉落差，不要每格一样大**。这是「整页平淡」的根因，逐条执行：
    - 至少一格 `close-up` 或 `extreme close-up`（脸、手、眼睛、局部）
    - 至少一格 `wide shot`（全身 + 环境，人物在画面里可以很小）
    - `key_panel` 那一格写全页信息量最大的画面：全身、双人同框、大情绪或大场面
    - 其余格用 `medium shot` / `medium close-up` 过渡
    全部写成中景或胸像 = 整页平，这是最常见的翻车方式。

# 版式选择（配合叙事节奏）
  single —— 开场建立、结尾定格、一个很大的情绪画面
  2h     —— 一来一回的对话，上下两格最好是一问一答
  3t     —— 大场面推进 + 底下两个细节特写（手、眼睛、道具）
  2x2    —— 连续动作的四拍，节奏要快
  3h     —— 三段式推进
  3x2    —— 单页讲完一整段小剧情：起承转合之后还有两个反应的位置
  3t2b   —— 单页五格：上排三格快节奏推进（脸、局部、反应），下排两个大格
            放高潮与收尾。**每格更宽、有大小对比，比 3x2 不平**，
            适合「一个动作 + 两个反应 + 两个大画面」这种故事

⚠️ 3x2 的每一格只有 341 像素宽（2x2 是 512），**气泡和旁白加起来别超过 20 字**，
  格子越窄越吃字数；六格全塞满长台词，中文会开始漏字。

# 叙事要求
- 页与页之间必须往前走，**不要重复同一个构图**。
- 最后一页要收住，可以无字。
- 全篇是一个完整小故事，有起、有转折、有收。
- **剧情要求什么就写什么，不要自行降级**。用户写「露出身体」就明确写
  `completely nude` / `her bare breasts visible`，不要退成 `bare shoulders`
  这种含糊词糊过去——含糊词会让模型只画到肩膀为止。
  同时保持 `anatomically correct and natural`，并用一句 `no hands or hair
  covering …` 钉住关键部位别被挡住。
- 角色一律按成年写（除非用户明说年龄）：`an adult woman in her mid-twenties`，
  不要用 `girl` / `teen` 泛指成年角色。

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
# 2026-10-31：改用**六格专用裁剪版**（4.4k 字符，原版 48.5k）。
#
# 原版是给「人手工写一张图 + 给采样参数」设计的，整篇塞进 system 后 88% 的
# 提示词都在讲摄影档位、Negative 池、二次元画风通道、Layout JSON——这些在这条
# 链路上要么由代码接管、要么由固定负向池接管，模型读完 4.8 万字才能开始干活。
# 裁剪版只留「一格怎么写成模型能画准的英文」那几条。
#
# 原版仍被 `agents/moss/agent.json`（主页 moss 助手）与两份 skill.md 引用，
# 所以**不改原文件**，只把这里指过去。要回退就把文件名改回 SKILL.md。
_SKILL_MD_PATH = os.path.join(BASE_DIR, "skills", "moss-image-prompt", "SKILL.comic.md")
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
│ 先读：本任务的适用边界（它决定下面这份规范里哪些条款管你）        │
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
· 原版 §14 的固定采样点，以及**一切分辨率 / 步数 / cfg / SLG 的具体数值**——
  实际档位由脚本常量固定，且会随渠道调整；你不写参数，也不需要知道当前是
  多少。§14 的证据强度声明只是标注，不影响你要写的内容。

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
§1 词条清洗、§2 一人一块、§3 镜头距离、§4 年龄与体型锁、§5 液体写法、
§6 多人防克隆、§7 图内文字、§8 输出前自检。

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
    lines.append("**每页的文字**：一页里**至少有一格有字**（`caption` 旁白 ≤10 字，"
                 "或 `bubble` 台词 ≤10 字）。**允许单格无字**——收尾定格、纯视觉冲击"
                 "的那一格留空更有力，编译时会自动补一句 "
                 "`No speech bubble and no text in this panel`，你不用自己写。"
                 "但别为了填满硬凑废话，也别连着空两格以上。"
                 "旁白框钉在左上、气泡钉在右下，位置由脚本给定，两者不会互相挤压。"
                 "`sfx` 有动作声就写，没有留空。")
    lines.append("**每页先写 `plan` 再写 `panels`**：`plan.key_panel` 是整页视觉重心"
                 "（必须落在最大那格），`plan.shots` 是每格景别、长度等于格数、"
                 "相邻不许相同、整页至少三档。写完 plan 再逐格写 `desc`，"
                 "景别照 plan 执行——不先规划整页，六格会全变成一样的胸像。")
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
        # plan 校验（只报不拦）：没有 plan 的页基本就是「逐格局部决策」，
        # 出图八成是整页同景别。这里把它报出来，别等出图才发现平。
        plan = p.get("plan") if isinstance(p.get("plan"), dict) else {}
        shots = plan.get("shots") or []
        if not plan:
            issues.append("第 %s 页没有 plan（缺整页视觉规划）" % n)
        else:
            if len(shots) != want:
                issues.append("第 %s 页 plan.shots 给了 %d 个，应为 %d 个"
                              % (n, len(shots), want))
            else:
                norm = [str(s or "").strip().lower() for s in shots]
                for k in range(1, len(norm)):
                    if norm[k] and norm[k] == norm[k - 1]:
                        issues.append("第 %s 页 plan.shots 第 %d、%d 格景别相同（%s）"
                                      % (n, k, k + 1, norm[k]))
                levels = {s for s in norm if s}
                if len(levels) < 3:
                    issues.append("第 %s 页 plan.shots 只有 %d 档景别，整页会平"
                                  % (n, len(levels)))
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

    # 尾句推迟到循环之后：要先知道这一页到底有没有文字，才能选 `_TAIL` 还是
    # `_TAIL_NOTE`（见 `_TAIL_NOTE` 注释里那对自相矛盾的句子）。

    seen = set()
    items = []
    has_text = False
    for i, panel in enumerate(panels):
        cell = boxes[i] if i < len(boxes) else boxes[-1]
        desc = _fill_page((panel.get("desc") or "").strip(), cast, seen)
        # 2026-10-31：**逐格**声明空格子。
        # 原来只有「整页一个字都没有」才追加 `_NO_TEXT`，一格有字一格没字时
        # 什么都不声明，模型会按「每格都该有气泡」的版面先验自己补一个
        # （实测第 5 格凭空冒出了第 4 格的台词）。这里改成空格子自己说清楚。
        cell_texts = [panel.get(k) or "" for k in ("caption", "bubble", "sfx")]
        has_cell_text = any(t.strip() for t in cell_texts)
        if with_text and not has_cell_text:
            # 没字的格子：模型自己写过的就不重复追加
            if not _NO_TXT_CLAUSE.search(desc):
                body = desc.rstrip(" .")
                # 不 strip：`_NO_TEXT_CELL` 自带前导空格，拼出来才是
                # `…covering her body. No speech bubble…`
                desc = (body + "." if body else "") + _NO_TEXT_CELL
        elif with_text:
            # 有字的格子：模型误写的否定句必须清掉，否则和气泡打架
            cleaned = _NO_TXT_CLAUSE.sub("", desc).strip(" ,;.")
            if cleaned:
                desc = cleaned + "."
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
    # 有文字才说「有清晰中文气泡」，没文字就换成「页面上没有文字」——
    # 两句不能同时出现（见 `_TAIL_NOTE` 注释）。
    cap.append((_TAIL if has_text else _TAIL_NOTE) % light)

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

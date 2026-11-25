# -*- coding: utf-8 -*-
"""MOSS 单 Agent 链路：一次写完整本 → 代码体检 → 定向重修 → 再体检。

为什么合并（2026-11-21）
────────────────────────────
原来「编剧 AI 写逐格稿 → 每页编译 AI 出 JSON」是 1+N 次请求，7000 字编译契约
每页重发一遍。3 页下来约 2.3 万 token 输入，其中 2.1 万是同一份约束的重复。
更大的问题是两个 AI 互不知情：编剧不知道最终 JSON 长什么样，只能写「画面：…」
让下游猜；下游不知道这句为什么这么写，只能照抄。中间那份 Markdown 是纯损耗。

这里只留一个 AI，让它直接产出最终格式。省下来的注意力放在「写得好不好」上。

自查为什么是代码
────────────────────────────
「再问一遍模型你觉得行吗」= 多烧一次钱，还拿不到判据。
`audit()` 只比事实：格子对不对得上骨架、文字有没有落进格子、外形串有没有原样
粘上、尾句跟本页有几种人的话是否一致、同一句台词有没有重复。
判据都是可数的，所以不误判、不重跑没问题的页——把清单交给模型，它只改这几处。

调用预算
────────────────────────────
1 次成稿 + 最多 2 次重修；一轮就过就只有 1 次。修完仍不过就交给 autofix 兜底，
不阻断出图（宁可出一版能改的，也不让人卡在这里）。
"""
import json
import re
import time

from app.features.comic import moss_twopass as M

STYLE_LOCK = M.STYLE_LOCK
GIRL_TAIL = M.TAIL_GIRL_ONLY
BOY_TAIL = M.TAIL_WITH_BOY
NO_TAIL = M.TAIL_NO_TEXT
S1_SENT = M.LAYOUT_SENTENCE["S1"]
L6_SENT = M.LAYOUT_SENTENCE["L6"]
D1_SENT = M.LAYOUT_SENTENCE["D1"]

MAX_ROUNDS = 2          # 最多重修几轮
REPAIR_TEMP = 0.3       # 重修时压低随机性，只改点名的错
REVIEW_TEMP = 0.3       # 二轮审稿同样压低随机性：审稿是纠错不是再创作

# 只用这三套：L0（顶条+右通高+左三小，图 1 那套标准格式）、S1、D1。
# 2026-11-21 撤掉 L6 / L5：它们的窄格是 1:2.3 / 1:3.1 的竖条，人塞进去必然被
# 拉成竹竿（用户实拍「角色突然变得很高」）。骨架少了，模型也不用挑。
# 2026-11-21（第二次）：用户拍板「只跑 1 格、2 格、3 格、5 格，4 格不要」，
# 3 格用 T3（上大横格 + 下两格），5 格只留 L0（顶部条 + 右通高 + 左三小）。
# D2「左右通高两格」实测 486×1536 = 1:3.2，比 L5 还窄，全身站进去必然拉长——不放进来了。
AGENT_LAYOUTS = ("L0", "S1", "D1", "T3")

# ── 示例页：L6 五格，密度照这个来 ──
EXAMPLE = {
    "layout": "L0",
    "scene": "1",
    "summary": {
        "meta": ["comic", "anime_illustration", "cel_shading"],
        "canvas": "multi_panel",
        "caption": (
            "A full-color Japanese anime comic page divided into five panels: one tall "
            "dominant panel filling the right side from top to bottom, a narrow left "
            "column of three small stacked panels, and a thin horizontal establishing "
            "strip across the very top, all separated by thin white gutters and a thin "
            "black ink border around the whole page. Full-color anime comic page, clean "
            "cel shading, flat anime coloring, crisp black ink outlines, sharp "
            "hard-edged cel shadows. In "
            "the dominant right "
            "panel, a girl about twelve, slender teen proportions, long legs, small head, "
            "long straight dark hair "
            "with soft blunt bangs pulled back by a wide fluffy pale cloth headband, "
            "large round dark eyes, round soft face, faint blush, wearing a plain "
            "short-sleeve white track shirt with a small chest number bib, loose white "
            "running shorts, a dark fabric scrunchie on her left wrist, white ankle "
            "socks and dark running shoes, stands on the track doing a standing quad "
            "stretch, one hand pulling her right ankle up behind her, her other arm "
            "raised for balance. School athletic field late afternoon, a cinder running "
            "track with faded white lane lines, a low chain-link fence and a row of "
            "school buildings behind, a water bottle and a small towel on the grass "
            "edge, paper bunting overhead, long low sun casting long warm "
            "shadows. Clean flat anime coloring, clear Chinese text: the girl's words in "
            "sharp speech bubbles with short pointer tails; no bare text, no off-screen "
            "speaker."
        ),
    },
    "items": [
        {"label": "object", "bbox": [-768, -494, -692, 494], "caption": (
            "Thin top strip panel, establishing view of an empty school athletic field "
            "in late afternoon: cinder track curving away, faded white lane lines, a low "
            "chain-link fence, paper bunting strung overhead, a water bottle and a folded "
            "towel on the grass edge, long shadows raking across the ground. "
            "No speech bubble and no text in this panel.")},
        {"label": "character", "bbox": [-692, -262, 754, 494], "caption": (
            "Tall dominant right panel, full body shot from a low front three-quarter "
            "angle: a girl about twelve, slender teen proportions, long legs, small head, "
            "long straight dark hair "
            "with soft blunt bangs pulled back by a wide fluffy pale cloth headband, "
            "large round dark eyes, round soft face, faint blush, plain short-sleeve "
            "white track shirt with a small chest number bib, loose white running "
            "shorts, dark fabric scrunchie on her left wrist, white ankle socks, dark "
            "running shoes, standing on one leg in a standing quad stretch, her right "
            "hand pulling her right ankle up behind her thigh, her left arm raised out "
            "for balance, torso upright, head turned slightly toward the viewer, cinder "
            "track and white lane lines under her standing foot, fence and school "
            "building far behind. clean cel shading, flat anime coloring, crisp black "
            "ink outlines, hard-edged cel shadows.")},
        {"label": "text", "bbox": [364, 124, 667, 441], "text": "再压一下就好",
         "caption": (
            "Tall rounded oval speech bubble in the lower right of the dominant panel, "
            "thin black ink outline, Chinese text written vertically with each "
            "character stacked top to bottom, 2 columns ordered left to right, short "
            "pointer tail pointing toward the girl's face.")},
        {"label": "character", "bbox": [-378, -494, 117, -272], "caption": (
            "Small left-top panel, close-up of her face from the front: teenage facial "
            "proportions, small head, long straight dark hair gathered back by the wide "
            "pale headband, blunt bangs, large round dark eyes, round soft face, faint "
            "blush, brows drawn slightly together, a bead of sweat at her temple. "
            "clean cel shading, flat anime coloring, crisp black ink outlines, hard-edged "
            "cel shadows. No speech bubble and no text in this panel.")},
        {"label": "character", "bbox": [130, -494, 540, -272], "caption": (
            "Small left-middle panel, medium shot from behind at three-quarter angle: "
            "the same girl, long straight dark hair over her back, white track shirt and "
            "running shorts, both hands pulling the raised ankle toward her hip, chin "
            "lifted a little, the field and fence beyond her. a girl about twelve, "
            "slender teen proportions, long legs, small head, long straight dark hair "
            "with soft blunt bangs pulled "
            "back by a wide fluffy pale cloth headband, large round dark eyes, round "
            "soft face, faint blush. clean cel shading, flat anime coloring, crisp black "
            "ink outlines, hard-edged cel shadows.")},
        {"label": "text", "bbox": [178, -446, 306, -366], "text": "一 二 三 四",
         "caption": (
            "Rounded oval speech bubble in the upper right of the panel, thin black "
            "outline, Chinese text written vertically with each character stacked top "
            "to bottom, 1 column, short pointer tail pointing toward the girl's head.")},
        {"label": "object", "bbox": [553, -494, 754, -272], "caption": (
            "Small left-bottom panel, close-up at ground level on the cinder: a dark "
            "running shoe planted on a faded white lane line, loose ash and small stones "
            "around the sole, the corner of a folded towel at the edge of the frame. "
            "No speech bubble and no text in this panel.")},
    ],
    "bbox_coord_space": "abs_centralized",
    "bbox_format": "yxyx",
}

SYSTEM = """你写全彩日漫整页分镜，直接输出 Layout JSON。输入是这一本里有谁、她是谁、这一集发生什么，
还有一个页数上限；输出是一个 JSON 对象。
只输出 JSON，不要围栏、不要解释、不要在 JSON 前后写任何字。

════ 输出 ════
{
  "title": "标题，12 字以内",
  "palette": "整本固定的英文配色串，只写一次，规则见下一节",
  "cast": {"girl": "英文外形串：只写脸、身材、发型、配饰，绝不写衣服"},
  "scenes": {"1": "英文场景串", "2": "..."},
  "pages": [ 每页一个对象，字段如下 ]
}
每页：{"layout": "L0", "scene": "1",
        "beat": "一句中文：这一页发生了什么变化，不是她做了什么动作",
        "link": "一句中文：第 1 页写这一集从哪里开始；后面每页写清接上一页的地点、人物站位、身体姿势、谁手里拿着什么",
        "outfits": {"girl": "英文：这一页她身上每一层的衣着，按下面 ③ 的顺序写全"},
        "summary": {...}, "items": [...],
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}
summary.canvas 一格页写 single，多格页写 multi_panel（代码会按格数核对一遍）。

════ 这一集的配色（全本只定一次，代码替你抄到每一页） ════
这是全彩页，颜色不写进 cast，也不许每页临时改。动笔前先定好这一集的 `palette`：
· 从这一集的衣服、头带、场景里挑 4~5 个**具体颜色**，写成一段英文串，每个颜色都带上它归谁：
  `"navy blue sailor top, pale blue sailor collar, off-white fuzzy headband, warm brown wooden racks, cream walls"`
· 只写画面里真会出现的东西。没有出现的颜色不要写进来；不要写抽象形容词
  （soft / beautiful / aesthetic），也不要写 hex 码。
· 串里的词你在每页 ③ 的衣着和 ④ 的场景里照用，**不许换同义词**：
  这一页 navy blue，后面就不许写 dark blue / deep blue；同一件衣服换词＝模型当成两件。
· 代码会把这段 palette 原文接到每一页 caption 后面，你不要在每页 caption 里重复抄它。
· **palette 缺失，整本颜色就会逐页漂**：这是必填字段，不能省。

════ 这一本里有谁（先定这个，再动笔） ════
输入开头会写清。**这一节决定整本有没有第二个人，读错了后面全错**：
· 「只有她一个人」→ 全书只有她。不许写男方框、不许写「他」说的任何话、不许出现男人的手或影子，
  也不许出现「镜头后面的我」——这一本的镜头是第三个人在看她，不是谁的眼睛。
  她的情绪、她的决定、她的反应，全部交给表情、身体和镜头。
· 「男主（画外，不出镜）」→ 他只在字里，不在画里。**但他不必说话**：整本一句不说完全正常，
  只有剧情真的需要一个回应（她递东西给他、他必须给个交代）时才写。
  **整本最多两三句**，一页最多一句；能让她自己做完的事，不要他去开口。
  五页里他出现两三页已经是上限，剩下几页交给她和镜头。
· 输入里根本没提这一节 → 按「有他，但能不说就不说」处理。
**没有男主的戏，就是没有。** 不许为了把格子填满就给他编话——
「我来我来，先回家」「衣服放门口了」这种句子，落在纯女主的一集里就是凭空多出一个人。
她在泥里摔了一跤、自己走回家、脱鞋、冲澡，这一路本来就不需要谁开口。

════ 整本怎么排：先排 beat，再拆页 ════
输入末尾会写「这一集要出的意外」。那是这一本的脊梁，比她做了哪几个动作重要。
· 用户填了转折 → 它必须在某一页**真的发生**，不许绕开、不许改成她心里想想就算了。
· 用户没填 → 自己设计一个只在这一集成立的小意外（灯坏了、东西找不到、被人撞见、
  她做了个反常的决定都行），但不要和上一集重复。
· 先把整本拆成五段：钩子（她要做什么／卡在哪）→ 推进 → 意外（转折发生）→ 她的反应 → 收尾。
  页数多于五页时一段可以占两页，少于五页就合并，但五段都要有落点。
· **页数多（8 页以上）或剧情带时间跨度（「七日」「这几天」「撑过去」「一整夜」）时，
  中段每一页必须落在一个具体事件上**：点的数在变（柴火从五根到三根到一根）、
  伤/冷/饿在变、天气在变、她试过的办法在变——读者翻过去要能数出过了几天。
  **绝不许把「这几天怎么过的」压成一页里两个人各说一句话**：那不是分镜，是跳过。
  一段两页就写两个不同日子、两件不同的事；页数就是天数时能一页一天最好。
  用户如果给了「分集大纲」逐页点了发生什么，严格按大纲一页一条，不许合并、不许跳过。
· **每一页写 `beat`**：一句中文，写这一页**发生了什么变化**。
  「她在扫地」是动作流水账，不算 beat；「她发现灯绳断了」才算。
· 相邻两页的 beat 不许是同一件事——连着两页都在扫同一个地，等于没写剧情。
· 转折和情绪一律靠画面：不写旁白、不写她心里想什么、不让男主用台词解释发生了什么。
· beat 是**页与页之间**的骨架，不塞进任何一格。拆完页之后照旧守下面的规矩：
  一页一个地点一个动作、一格一个定格、连着两个状态用两页。

════ 人：脸是固定的，衣服跟着这一页走 ════
输入里的「她是谁」通常是一段现成的英文外形串。那是**角色档案**，重点是脸和身材：年龄、体型、发型发色、头带、眼睛、脸型、配饰。
档案里写的那件衣服不要抄。衣服是她这一集穿的东西，不是她这个人。

⚠ **分集大纲点名要求的衣着状态，优先于本段以下所有服饰规则（2026-11-25 加）。**
大纲在具体某一页写了穿什么、换掉、脱掉、不带、不着衣物，那一页就照大纲写，
本段以下规则只用来**填充大纲没指定的部分**，不许反过来推翻大纲点名的状态。
尤其是「不穿 / 没有外套了 / 光着」这类**减法**：大纲减掉的，这一页的 outfits
就必须真的没有它——不许因为上一页有这么一件、或者本段说「同一件衣服跨页用同一串词」
就让它复活。衣服的连贯是**跟大纲走**，不是跟上一页走。

**「不穿」怎么写**：outfits 要写成**正向的状态**，别写 `no shirt` 这种否定式
（出图模型读否定句只会看见 shirt）。身上没有衣物就正着写她身上是什么——
`fully nude, bare skin`、`topless, bare chest`、`a plain white underlayer only, no
outer layer`、宠物就写 `bare fur, no coat`。否定式一句都不要留在 outfits 里。
· **cast 里必须有一项比例锚点，固定写这一串**：`slender teen proportions, long legs, small head`。
  档案里写 slender small frame / petite / tiny / childlike 的，**换成这一串**——年龄、发型、头带、
  眼睛、配饰一个字都不动，只换这一项。small frame / petite 出图会把她画成小孩，
  而这一本里她是十几岁的学生（2026-11-22 泳池话题：P1 是正常少女，P2 靠池边喘气那格直接画成 Q 版）。
· **cast.girl 只写不变项**，一项都不许带 wearing / dressed in / 任何衣物。
  衣服一进 cast，这本漫画从第一页到最后一页就只能穿那一件，换装剧情永远失败。
· 每一页先定「这一页她身上是什么」，写进 summary 的 ③。
· 每一格按景别写人物，**一格只写一次**：
  - 看得到身体的格（全身、中景、中近景）：完整外形串 + 这一页的衣着，抄一次，不缩写不调序
  - 纯局部特写（close-up of her face / of her hand / of her feet）：只写那一处——脸就写发型、
    眼睛、表情，手就写手指和入画的袖口。**不写身材、不贴全身外形串、不写整件衣服**。
    脸特写里写 long legs 或 wearing …，出图只能把镜头拉远成中景
· **看得到身体的每一格，都必须把这页 outfits 那串衣着原样写进 caption**（2026-11-24 用户：
  「角色穿什么衣服，每个格子都给我写清楚，不要指望写一个女生就行了」）。
  **光写 `1girl, sitting by the window` 就是没交作业**——出图模型会自己给她编一件，
  下一格又编另一件，于是「同一套衣服页页不一样」「要求的换装不生效」。
  写法：`1girl, <外形串>, wearing <这一页 outfits 原文>, <动作>, <神态>`，
  衣着串一个字都不许改（改一个词＝另一件衣服）。代码会在出图前把 outfits 原文逐格补上，
  但**你自己写对**才是对的——补的那一份只是兜底，兜不住细节。
· 一格里的衣服只提一次。先写了「背后湿透的衬衫」，结尾就不要再贴一遍外形串和衣着——
  同一格写两遍，出图会当成两件衣服。
· 换装：换之前那页写旧衣；**脱下来那一格写「已经脱掉」**（内搭在身上、湿衣在手里），
  不写半脱——"半脱"就是两个状态挤在一格，出图必然穿一件拿一件。换完那一页起每页都写新衣，不许再出现旧衣。
  衣服递出去了、送人了、被拿走了，后面就不许再画它。
· 拿到新衣还没换上的那一页：写她穿着内搭、手里是新衣（一件在身上、一件在手里，各归各位，
  两件都写清楚）。不要写「正在套上」，也不要让她凭空换好。
· **脱下来的衣服不许再挂在身上**：那一格写 held / balled up / dangling from her hand，
  同一格里再写 wearing 它，出图就会画成「身上穿着一件、手里还拿着一件」。
  脱到只剩内搭就写她穿着内搭（a plain white underlayer），别让她光着。
· 档案里那件衣服不进任何一格，也不许拿它的细节做对比（不写 the lace collar of the old one、
  不写 unlike the previous one）——读者没看过档案。
· 湿、脏、破写在这一页的衣着里：soaked, translucent, clinging, muddy at the hem。不写进 cast。
· 「干净的新衣服」写 fresh, dry，不要写 dry clean（会被读成干洗）。
· **每件衣服都写清款式**：领型（collar / open collar / V-neck / sailor collar）、门襟和扣子
  （buttoned placket with small buttons）、袖长、版型、口袋。只写 a white shirt，出图会自己编一件——
  扣子说没就没、领子说变就变，下一页还换成 polo。
  **同一件衣服跨页用同一串词**：这一页写 a white short-sleeve button-up shirt with a collar and a
  buttoned placket，后面每一页都照抄这一串，不许换说法（shirt / blouse / polo 混着写＝三件衣服）。
· **外衣是湿透、半透、或者这一集要脱掉的，第一次出现就把里面那一层写清**：
  颜色 + 款式（a plain white underlayer tank / a dark sports bra with thin straps），后面每页沿用同一串词。
  不写内搭，模型会自己给她配一件——第一页是黑运动内衣、第三页变白背心，前后就接不上了。
  内搭是「她这一身」的一部分：只有换装剧情才许换，换了要写清在哪一格换的。
  **内搭不是后面才揭晓的东西**：她从头到尾都穿着它，所以**第一页就得写**，
  湿透的白外衣要写成 clinging translucent over her plain white underlayer tank（从领口看得见那两根细带），
  **绝不写 clinging to her skin / against her bare skin**——那等于她里面什么都没穿，下一页就没得接了。
· **写 ③ 按顺序写全**：内搭 → 外衣 → 下装 → 鞋袜。三层都在这页身上，就三层都写；
  只写外衣，等于把里面那层交给模型自己编。
· **每一页都要写 "outfits" 字段**（{"girl": "这一页她身上每一层的英文衣着串"}）——
  这是「这一页身上是什么」的**权威状态**，代码会把这串原文钉进这一页的出图 JSON。
  剧情里衣服变了（变身 / 解除变身 / 脱下 / 换装 / 淋湿），**从变化那一页起 outfits
  就换成新状态，之后每页都写新状态，绝不再出现旧衣**（实测翻车：P5 已经脱了裙子、
  出图还穿着裙子；P6 该穿水手服、变身服又复活了）。格子里的衣着描述必须和
  outfits 用**同一串词**；多角色同框的群像本给每个出场角色各写一条（key 照 cast 抄）。

════ 场景也是逐页钉死的：出图时模型看不见别的页 ════
顶层 "scenes" 表是「这一页在哪里」的权威状态，代码会把这一页的 scene 串原文
钉进这一页的出图 JSON。但**每一页是独立送画**的——画这一页的模型看不见上一页，
所以：
· **每条 scene 串必须自包含**：地点 + 时间 + 光线 + 地面/墙面材质 + 这一页会
  拍到的关键物件，一段写全。**绝不写 the same plaza / the same bathroom /
  同上一页** 这类引用——「the same」到了出图端就剩一个光杆地名，时间和材质
  全丢（实测翻车：P2 写 the same stone plaza 丢了 dusk，爆炸页画成大白天；
  P6 写 the same bathroom 丢了瓷砖和浴缸，布局凭空换了）。
· **同一个场景连续几页，scene 串逐字照抄同一份**——不是每页重新描述一遍
  （每页各写各的，柱子数量、灯的位置、瓷砖颜色就会页页不一样）。换了地方
  （回家 / 进浴室 / 到客厅），从那一页起写一份新串。
· 时间光线必须显式写在串里（at dusk / at night / daylight / warm indoor
  light），不许指望「爆炸的光」替你交代时间。
· 画面里出现的陈设必须来自 scene 串。串里没写的家具模型会自己编
  （实测：玄关页凭空长出一个马桶）。

════ 比例是硬的：表情不会把她变小 ════
整本只有一副身体。张嘴大笑、睁大眼睛、惊讶、害羞、委屈，都只是同一张脸上换表情。
出图端最容易的翻车是**把可爱画成幼态**：头大了一圈、眼占半张脸、三头身——那一格就变成了 Q 版，
跟前面几页不是一个人。防它就靠三件事：
· cast 里那串比例锚点（`slender teen proportions, long legs, small head`）**每一格原样抄**，
  看得到身体的格一次都不许省；情绪越大的格越要抄——大笑、蹦跳、举拳欢呼就是漂移高发格。
· **脸特写也要写比例**：close-up of her face 里头会挤满整个格子，最容易被画成小孩。
  脸特写里加 `teenage facial proportions, small head`，然后再写发型、眼睛、表情。
· 情绪写在**表情**上，不写在**尺寸**上：mouth open in a wide grin / eyes wide with delight
  是表情，不是让她变成大头娃娃。**不写 chibi / super-deformed / cute small body / childlike**。
· **欢呼 / 举拳 / 大动作那一格最容易画成幼态**：头变大、五官往中间挤。这些格子里
  比例锚点一个字都不能省，照抄 `slender teen proportions, long legs, small head`，
  再补 `adult teenage face, not a child's round cheeks`。
· 收稿前自己比一眼：这一页的头，是不是比上一页大了一圈？大了就是错的，不是风格差异。

════ 地图是虚构的：不画现实世界 ════
· 剧情里的地图 / 文件 / 屏幕插画，一律写成**虚构地貌**：
  `an invented fantasy map of an imaginary island chain, abstract contour lines,
   unlabeled routes and markers, no recognizable real geography`。
· **绝不写现实国家 / 地区 / 省份的轮廓、边界或版图形状**，也不写国家名字。
  模型见到「前线基地 + 地图」就爱画成某国的雄鸡版图——那是内容事故
  （2026-11-22 异界前线 P10 实测）。要画地图就画虚构岛屿群或抽象路线图，
  认不出任何真实疆界才合格。
· 这是硬要求，和画风无关：任何一页、任何格都不例外。

════ 发型是硬的：长发就是一直披着 ════
· cast 写了 `long dark hair`（或任何 long / straight hair）就是**披肩长直发**，
  每一格都写 `long straight hair worn loose, falling past her shoulders`。
  模型爱把它随手扎成单马尾 / 丸子头，那是换了个人（2026-11-22 异界前线 P5 实测）。
· 同一本里发型只有一种：脸特写、战斗格、欢呼格抄的都是同一串，
  不写 `ponytail` / `tied back` / `hair gathered` 这类会改发型的词。
· 想让她真的扎起来，只在 cast 里写明并整本照抄那一种；中途不许自己换。

════ 用镜头讲，别用字讲 ════
一页只说一件事，靠换机位把它说清。想让人读出「她冷」「她脏」「她不想被碰」，
不要写台词，换一个能看见这件事的镜头：
· 建场（远）：环境比她大——雨里的巷子、门口的水洼、亮着灯的浴室。人不一定在画面正中。
· 动作（中）：手在做什么——捏着湿透的衣角、扶着门框、鞋跟在台阶上蹭泥。
· 局部（特写）：只说一件事的一小块——塞满泥的指甲、滴水还往下淌的发梢、脚背上冲下来的黑水。
· 反应（近）：脸和眼睛。她看的方向，就是她把注意力放在哪儿。
一页里把「远 → 中 → 特写」串起来，读者自己补得出因果；
三张同样的半身像平铺，等于什么都没说。
**S1 一格只在「这一页就是那一张脸 / 那一个对视」时才用。**
她站着说一句话不是理由——那件事要用 D1（说之前 → 说之后），
或者 T3（人 + 她看的东西 + 手边）来讲。

════ 机位、她朝哪、目标在哪（三样一起写） ════
一格里的空间关系只有一个写法是对的：**先定她面朝哪，再定机位站哪，最后写目标落在画面哪一侧。**
· **机位不许站在她和目标之间。** 她要跳进泳池，机位就不能在她正前方——镜头在她正前方，
  泳池就跑到镜头背后去了，画面里根本装不下，出图只能把水面糊到背景里，看上去就成了
  「她背对泳池、朝镜头做准备」，方向整个反了（2026-11-22 泳池话题 P1 实测：
  文字写的是 the lane water stretching ahead of her，画出来泳池在她身后）。
· 目标方位**只写画面方位，不写相对她的方位**：写 `the lane water fills the lower half of the frame`、
  `the pool surface recedes toward the top of the frame`、`the water runs along the left edge of the panel`。
  **不写** ahead of her / in front of her / before her——模型算不出她朝哪，这一句它当没写。
· 三种能同时装下人和目标的机位，按这一格要什么挑一个：
  - 侧后：`seen from behind at a three-quarter back angle, only the edge of her cheek in view`
    —— 她的背和侧脸，泳池 / 门 / 走廊在画面深处延伸。
  - 侧面：`in profile from her right side` —— 她的朝向横穿画面，目标在同一排的远处。
  - 高位俯拍：`high angle from above and behind` —— 台子、水面、地面在画面下方摊开。
    （蹲在出发台上准备起跳，用侧后或高位俯拍；要脸就另给一格脸特写，别让主格去转正面。）
· **想让她正面朝镜头，就别让她正对着要去的地方**：她可以回头、可以停下来、可以面对着镜头说话，
  但「正在往那儿去」「正要跳出去」的那一格，不能同时又正面朝镜头。
· 一格里的「朝哪」和「目标在哪」两句话缺一句，这一格的空间关系就交给运气了。

════ 一页怎么想 ════
一页只放一个地点、一个动作。读者翻过这页，要能说出「她刚做了什么」。
写之前先定三件事：她现在在哪、身体停在什么姿势、脸上是什么表情。
动作写成已经定住的静态结果：her right hand pulling her right ankle up behind her thigh。
一页的动作写一件事；换地点就换页，不在一页里跨两个房间。
上一页结尾她在哪、身上有什么，下一页第一格还在那儿。

════ 一格 = 一个定格（比什么都重要） ════
一格是读者一眼能看完的一个瞬间，里面只有一个状态。**一格不是一段过程。**
· **动作和它的结果不许画在同一格**：手还在扯领口（动作）＋ 衣服已经团在手里（结果）＝ 两个状态，
  出图会画成身上一件、手里一件。第二件事要占第二格。
· **连续动作只在状态跳变处切格**，中间过程不画——读者靠格与格之间的空白自己补。
  脱衣、穿衣、走过去、转身，都是这样：只画"脱完了没脱完"、"穿上没穿上"，不画半截。
· **一格只有一种衣物归属**：在身上（wearing），或者已经离身（held / balled up / dangling from her hand / 在头上）。
  写了 wearing 它，同一格就绝不许再写手里拿着它。
· **绝对不许写中间态**：half-off / one shoulder bare / pulled down to her waist / halfway / mid-change /
  slipping it over。要画"正在脱"，就画一个确定的瞬间：湿衣已经从头顶拉脱、翻套在头上遮住脸
  （衣服在头上，不在身上），或者已经整件褪到手里。
· 一格里只有一个人、一个动作、一个表情、一个衣着状态。

**一根时间线怎么排**（她连着做几个动作，比如换衣）：
一格两状态 → 用 D1 一页一页往下串，不要塞进 L0 的五格。
  第1页 D1   上格：已经脱净（内搭 + 湿衣团在手里）  |  下格：递出去，她的手空了
  第2页 D1   上格：接住新衣，新衣在手还没穿        |  下格：套上，头已经钻过去
  第3页 S1   穿好，看着镜头道谢                    ← 换好之后另起一页
**翻页 = 状态重置**：新一页的第一格必须是稳定状态（穿着、拿着、站在那儿），不许还在换。
**状态只许往前走**：已经扔出去、递出去、掉在地上的东西，后面几页只许出现在地上、画面外、
或者我们手上，不许又回到她手里、怀里。她做过一次的反应（发现、回头、愣住）也不许再做第二遍
——那是同一个 beat 用了两次。
**格数 = 状态数**。状态比格多就加页，绝不许把两个状态压进一格。

════ 踩在东西上，就把接触面写出来 ════
她站在出发台、领奖台、台阶、池边、椅子、床沿上，出图最常见的坏法是**脚陷进去**：
方块画到脚前面，脚踝以下消失。原因是只写了 standing on the block——没写台面在哪，
模型就自己决定台面高度，脚和台面一交叉就成了穿模（2026-11-22 泳池话题 P4 实测）。
· 站 / 蹲 / 坐 / 跪在任何东西上，三样一起写：
  ① 接触面：`both bare feet planted flat on the flat top of the block`（谁的什么部位 + 那个平面）
  ② 平面看得见：`the pale top surface of the block visible under and around her feet`
  ③ 边缘关系：`her toes a little short of the front lip, the block's side faces dropping away below her ankles`
· 坐：`sitting with her seat and the backs of her thighs on the top surface, her lower legs hanging
  over the front edge`。跪：`knees and shins flat on the surface, the tops of her feet on the same plane`。
· 手撑着某个面：`both palms flat on the tile beside her hips, fingers spread on the surface`。
· **绝不写** standing in the block / her feet sinking into / her ankles behind the block。
· 特写格也照这个写：`close-up of her bare feet on the flat top surface of the block` 比
  `close-up of her feet on the block` 稳得多——脚特写最容易穿模。
· 站在**地面**上不用写这一套（地面没有边界），但站在**有高度边的东西**上一定要写。

════ 脚是脚、手是手、身体连着腿（2026-11-22 实测） ════
出图把「脚的动作」画到手的位置、把坐着的人画成身体和腿两截，根因都是只写了一个
部位名，没写它归谁、接在哪。写脚/腿的格，遵守三条：
· **脚的动作必须带上腿和身体**：不写 `one boot pressed into the embers`（一只靴子踩在火里
  ——模型会在她伸手的位置凭空长出一只靴子，看着像她的手变成了鞋）。写
  `her booted foot resting on the flat stone hearth edge, her leg extending up to her knee
  under the coat hem, the toe just clear of the embers`，让脚 → 小腿 → 膝盖 → 身体
  连成一条，落脚面也写清（见上一节）。踩火 / 踩雪 / 踩水都照这个写。
· **坐着的姿势要把身体和腿接起来**：不写 `sitting up in bed, blanket falling to her waist`
  再加一句裸腿——模型会把上半身和腿画成两块拼在一起。写
  `sitting upright on the bed with her hips and the backs of her thighs on the mattress,
  both knees drawn up, lower legs continuing down to her feet tucked under the blanket,
  one continuous body from her shoulders through her hips to her feet`。
  髋、大腿、膝、小腿、脚一路写全，中间不许断在毯子边界上。
· **特写格只放大一处，但不许让肢体凭空出现**：手特写就只有手和它连着的那截手腕，
  脚特写就只有脚和它连着的那截脚踝；`a boot` / `a hand` 单独悬在画面里，模型会自己
  把它接到最近的肢体上，接错就成「手是鞋子」。

════ 字（这些是上限，不是配额） ════
一格页 0~1 句；两格页 0~2 句；三格页 0~3 句；五格页 0~4 句。
**镜头能说清的就不写字。** 整本一句台词都没有也成立——她摔进泥里、爬起来、进门脱鞋、站在花洒下，
画面自己会讲；读者不需要有人替她说明。
为了把格子填满去写「好……」「嗯」「唉」是错的，读者只会觉得吵。
台词一句一个意思，十来个字，落在说话人自己在的那一格。
**台词跟动作对齐**：一句台词必须落在「这件事真正发生」的那一格——说「给你」「接着」，
就得是东西已经离手、她的手伸向画面外、朝我递过来的那一格。
上一格她只是把湿衣服抱在胸前 / 团在手里，就只写动作、一个字都不许说。
「拿着」和「递出」是两个状态，拆成两格时台词永远在**后面**那一格。
一句台词只出现一次，不许为了把格子填满而在两格各写一遍。
**这一集最多两种字：她的圆气泡、他的直角方框。**（没有男主时只有第一种，那就只有气泡，或者干脆没有字。）
不许旁白框、不许独白框——她心里怎么想、她打算做什么，一个字都不上画面；
时间地点交给画面本身（拍到哪就是哪）。想让她说什么，就让她自己在气泡里说出口。
单个格子可以完全没有字，整页也可以一个字都没有——**判断标准是「画面把这个瞬间说清了没有」**，
不是「这一页有没有字」。一张脸愣在那儿、一只手抓着湿衣服、一排水洼里的倒影，本来就不需要谁来解说。

════ 骨架（只有这四种，bbox 整组照抄，一个字符都不许改） ════
L0 · 五格 · 顶条建场 + 右通高主格 + 左栏三小
  [-768, -494, -692, 494]   顶条
  [-692, -262, 754, 494]    右通高主格
  [-378, -494, 117, -272]   左上小格
  [130, -494, 540, -272]    左中小格
  [553, -494, 754, -272]    左下小格
S1 · 一格 · 一页只装一个瞬间（一张脸、一双手、一个对视）
  [-768, -494, 768, 494]
D1 · 两格 · 上下（同一空间里前后两个动作：递出去 → 接住）
  [-768, -494, -8, 494]    [8, -494, 768, 494]
T3 · 三格 · 上大横格 + 下两格（上面一个主画面，下面两处补充）
  [-768, -494, 96, 494]
  [112, -494, 768, -8]      [112, 8, 768, 494]

选法：一页一拍就 S1；一条时间线上前后两个状态 D1（脱净 → 递出、接住 → 套上）；
一个主画面加两处空间补充 T3；一整段过程（建场 → 动作 → 反应）才用 L0。
**L0 装的是同一时刻的不同侧面**（一个建场、一个主画面、三处局部），不是时间线：
五个格子里不许出现"她先做了 A、又做了 B、最后做了 C"——那是把一段过程拆碎塞进小格，
页内会出现两个互相打架的状态（比如一格已经穿好、另一格还在脱）。连续动作一律用 D1 往下排。
默认往少格走，一页能用一个画面讲完就不要拆。**永远不做四格**，也不要做左右通高的两格
（486×1536 那种会把人物拉长），更不要自己发明窄竖条。
窄格只放局部：左栏三小只放脸、手、脚、道具这些特写，**全身只放在 S1 或 L0 的右通高主格**。
相邻两页换着用，不要连着两页同一个骨架。
格号 = 阅读顺序：从上到下、从左到右。L0 是 顶条 → 右通高主格 → 左上 → 左中 → 左下；T3 是 上主格 → 下左 → 下右。
一格一条 character 或 object，bbox 正好等于这一格，整组不许增删、不许重叠、不许跨骨架借格。

════ summary.caption：一整段英文，连着写完，不分行不标号 ════
依次写这五样，用逗号句号连起来：
① 分格：A full-color Japanese anime comic page divided into five panels: one tall dominant panel filling the right side from top to bottom, a narrow left column of three small stacked panels, and a thin horizontal establishing strip across the very top, separated by thin white gutters and a thin black ink border around the whole page.
② 画风：Full-color Japanese anime comic page, clean cel shading, flat anime coloring, crisp black ink outlines, sharp hard-edged cel shadows, no screentone, no greyscale.
③ 主格：景别 + 她在做什么 + 完整外形串（从 cast 逐字抄，不缩写不调序）+ 本页衣着（这一页她身上穿什么，这一页在换装就写成变化）+ 构图位置
④ 场景：地点 + 时间 + 光源 + 两三个具体物件
⑤ 文字：有字写 Clean flat anime coloring, clear Chinese text: the girl's words in sharp speech bubbles with short pointer tails；
   本页还有男主画外话，补 the off-screen boy's words in a plain rectangular box with a thin black outline and no tail
   ——**这一本没有男主时，这一句一个字都不许出现**；
   整页没有字写 no text and no speech bubbles anywhere on the page.
全英文，一个汉字都不许有。
①里的格线永远细：thin white gutters + a thin black ink border。不要写 thick / heavy / bold frame——五页连起来翻，外框粗细一变，页面就会跳。

════ items ════
character：这一格画什么 —— 景别 + 定住的姿势 + 外形串（看得到身体的格：完整原样粘贴；
  纯脸 / 手的特写：只写那一处相关的项，别贴全身串和整件衣服）+ 本页衣着（照抄本页 ③ 里那一段；
  正在换装就写变化）+ 结尾固定写
  clean cel shading, flat anime coloring, crisp black ink outlines, hard-edged cel shadows.
object：空镜或道具 + 结尾固定写 No speech bubble and no text in this panel.
text：只写容器形状和字的排法，台词内容只进 text 字段。**只有两种容器，按「谁在说」选**：
  ① 她本人在这一格画面里开口 → 圆气泡，尾巴指向她的脸：
     Vertical rounded speech bubble with a thin black ink outline, Chinese text written vertically with each character stacked top to bottom, N columns, the first column on the left, reading left to right, short pointer tail pointing toward the girl's face, compact bubble.
  ② 只有男主画外的一句话 → 直角矩形方框（不是气泡、也不是裸字），贴这一格右上：
     Boy speech box: plain rectangular box with a thin black ink outline, sharp square corners, no bubble tail, no pointer, Chinese text written vertically with each character stacked top to bottom, N columns, the first column on the left, reading left to right.
  **每条圆气泡 text item 必须写 `who` 字段**，值是说话人在画面里的身份：女生写 `girl`，
  另一个人写他在这一格的外形（如 `the commanding officer in a dark navy uniform`）。
  尾巴指向谁由这个字段决定；一格两人一问一答时两条必须写不同的 who——
  都留空或都写 girl，两条尾巴就都连到女生脸上，台词全串（2026-11-22 异界前线 P2 实测）。

**没有旁白框**：不要 rectangular caption box，不要「她心想 / 她决定 / 她意识到」这类文字。
读者需要知道的事，让画面自己说，或者让她用气泡说出口。

**男主的话一律用矩形方框**：他不出镜，给他画气泡，尾巴就只能指向女生，读者会把他的话当成她说的。
方框是他的身份标记——**直角方框＝他，圆气泡＝她**，一眼分得开；不许写成压在画面上的白边裸字
（裸字没有边界，翻页时和拟声、色块糊在一起）。方框位置固定在右上，永远不跟她的气泡重叠。

**归属不能图省事。** 男主的方框只放「他对她说的一句话」，一页最多一句，而且必须是在回应她上一格的动作或话。
**写成一句直接的要求**：衣服给我 / 接着 / 转过来看看——而不是反问（有换的吗 / 你不冷吗）。
反问把动作推回给她，画面就空着；直接说他要什么，她的动作才接得上。
凡是「接下来该去做什么」的念头、她自己下的决定、她的感受和判断，**一个字都不许上画面**（不写独白、不写旁白），
交给表情和动作。她的提议确实要出口，就用 ① 让她自己说。
**多列中文一律从左往右排**：句首落在最左一列，读者按最左列 → 右边列的顺序读（不要说 right to left）。
**同一格里同时有她的话和他的话时，必须是两个容器**：圆气泡带尾巴指向她，直角方框放另一侧。
两条不同归属的字绝不许并进同一个气泡。
每个 label=text 的 item 必须带 text 字段：这句原文，一字不改，不留空。

════ 文字框 bbox（格 [y0,x0,y1,x1]，h=y1-y0，w=x1-x0，算完取整） ════
气泡从下往上叠：第1条 [y0+0.74h, x1-0.49w, y0+0.95h, x1-0.07w]
              第2条 [y0+0.50h, x1-0.49w, y0+0.71h, x1-0.07w]
              第3条 [y0+0.26h, x1-0.49w, y0+0.47h, x1-0.07w]
男主方框贴右上：第1条 [y0+0.04h, x0+0.55w, y0+0.26h, x1-0.07w]
              第2条 [y0+0.28h, x0+0.55w, y0+0.48h, x1-0.07w]
框离格边至少留 2%。气泡宽度约为格宽的 42%。同一格多句，先说的在上。

════ 画什么 ════
**有男主时**，镜头就是我们：她冲着镜头做动作——递过来、扔过来、抬眼看过来、把东西推到你面前，动作方向直接朝画面外。
**没有男主时**，镜头是旁观者：她做给谁看都不为，动作朝着画面里她正在处理的东西（手里的衣角、脚下的水洼、墙上的挂钩）。
**镜头后面的「我」不进画面**：POV 只约束画外的我们，不禁止剧情点名要出场的角色。
**剧情写到的人类角色就画人类**：人类村庄的守卫、村民、旅人、敌兵照剧情写，
不写 person/figure/someone 这种没种族没外形的泛称。远景需要人时按剧情写
two or three distant girl figures 或 two human guards with short brown hair。
**更不许拿怪物填人类的位置**：写 human guard 就不许画成 goblin/orc——
前面九页都在打哥布林，第十页的人类守卫照样必须是人，不许顺着前一节的怪物惯性长过去。

════ 新场景换了角色，外形要写死 ════
换场景时第一次出场的非女主角色（守卫、村民、店主、敌兵、路人），
在那一格写清**种族 + 可见外形**：a human guard with short grey hair, a steel helmet,
a brown cloak, holding a spear。后面用 the human guard 指代，外形串不再重复一整段。
只写 a guard / the guard / a man / a woman / someone 这种光杆称呼＝没交外形，
模型会拿前面几页的角色去填空（实测：人类村庄的守卫被画成前面打了一路的哥布林）。
非人类角色也要写明是哪一种（a goblin with green skin and pointed ears），
不许只写 a creature / a monster。
有男主时，我们只以一只手的形式出现：pov hands / disembodied hand，最多两只，且贴画面边缘；
没有男主时，画面里**连手都没有**——没有「我」，就没有「我的手」，她的东西自己拿、自己放。
递、扔、交、接这类动作，**她的手或小臂必须留在画面里**，东西不能自己悬在半空。
**有男主时，我们的动作也要画出来**：我把东西扔给她，就画成画面边缘伸出的手把东西抛过去、她伸手接住；
不要改成她自己去找、自己去拿——那是把我们的动作偷掉了。
（没有男主时这一条不适用：剧本里没有「我」，就没有需要画出来的「我的动作」。）
湿、汗、水只用材质词：soaked, translucent, clinging, dark and heavy；不要写 dark patch / black stain / wet spot（会画成一块脏）。
人物镜头最紧到中近景：medium close-up with head and shoulders in frame。
**颜色写具体词，但只能从本集 palette 里抄**：navy blue / pale blue 这样带色相的词要写，
不许再用「灰蓝调」「暗色衣服」这种模糊说法代替；palette 没写的颜色不要临时发明。
湿、汗、水仍只写材质（soaked, translucent, clinging），不写 dark patch / black stain。
外形串是唯一事实源：看得到身体的格，cast 写了几项就抄几项，不缩写、不调序、不改词；
纯局部特写只抄和那一处有关的项（脸：比例、发型、头带、眼睛、脸）。
双人同框时第二个人开头显式互斥：In sharp contrast, the other girl has NO long hair, NO headband。
衣服不在 cast 里：只有「这一页她穿什么」，没有「她这个人穿什么」。

════ cast 长这样（照这个抄结构，衣服一个字都不许带） ════
"cast": {"girl": "a girl about twelve, slender teen proportions, long legs, small head, long straight dark hair with soft blunt bangs pulled back by a wide fluffy pale cloth headband, large round dark eyes, round soft face, faint blush, a dark fabric scrunchie on her left wrist"}
她这一页穿什么，不在 cast 里，而在这一页的 ③（英文）：
"...a soaked white short-sleeve track shirt clinging to her, loose white running shorts..."

════ 一页长这样（照这个密度和写法，不要缩水） ════
""" + json.dumps(EXAMPLE, ensure_ascii=False, indent=1) + """

现在把话题写成 pages。"""


# ══════════════════════ 输入：谁在这本里 ══════════════════════
# 用户 2026-11-22 定的输入格式（沿用漫画工作台那套）：
#   男主：无
#   女主：a girl about twelve, …
#   剧情：她在雨里摔进泥坑，自己走回家，脱鞋，冲澡。
# 男主写「无」= 这一集没有男主的戏：全程只有她，不许凭空给他编台词。
# 实测《泥坑之后，热水之前》：剧情里明明没有他，编剧自己塞了「我来我来，先回家」
# 和「衣服放门口了」两句——旧提示词只教了「男主的话怎么画」，从没说过「可以没有男主」。
_ROLE_LINE = re.compile(
    r"^[\s#*\-]*"
    r"(?:[【\[]\s*(男主|女主|剧情|转折)\s*[】\]]\s*[：:]?"  # 【男主】无 / 【转折】：无
    r"|(男主|女主|剧情|转折)\s*[*]{0,2}\s*[：:])"   # 男主：无 / **男主**：无
    r"\s*(.*)$")
# 「无」的各种写法。空的不算：写了「男主：」却没填，是没交作业，不是没有他。
_NO_MALE = {"无", "无男主", "没有", "没有男主", "none", "no", "n/a", "na", "null", "-", "—", "空"}


def parse_brief(text):
    """把输入的「男主 / 女主 / 剧情」拆成三段。

    兼容 `男主：` `男主:` `【男主】`，以及标签下面接着写多行。返回：
    `male` / `female` / `plot`、`labeled`（有没有命中角色行）、`male_none`。
    一行角色行都没有 = 旧格式，整段当剧情（`labeled=False`），
    提示词那边按「有他、但能不说就不说」处理。
    """
    raw = (text or "").strip()
    out = {"male": "", "female": "", "plot": "", "twist": "",
           "labeled": False, "male_none": False}
    if not raw:
        return out
    buckets = {"男主": [], "女主": [], "剧情": [], "转折": [], _SKIP_BUCKET: []}
    cur = None
    for line in raw.splitlines():
        # 分集大纲是独立字段，由 parse_outline 单独取；这里吞掉整段，
        # 否则「第1页：…」会被当成转折的一部分塞进 twist，大纲就被写两遍。
        if _OUTLINE_HEADER.match(line):
            cur = _SKIP_BUCKET
            continue
        if cur == _SKIP_BUCKET:
            continue
        hit = _ROLE_LINE.match(line)
        if hit:
            cur = hit.group(1) or hit.group(2)
            out["labeled"] = True
            buckets[cur].append((hit.group(3) or "").strip())
        elif cur:
            buckets[cur].append(line.strip())
        else:
            buckets["剧情"].append(line.strip())
    for key, val in (("male", "男主"), ("female", "女主"),
                     ("plot", "剧情"), ("twist", "转折")):
        out[key] = "\n".join(x for x in buckets[val] if x).strip()
    out["male_none"] = bool(out["labeled"]) and \
        out["male"].strip().strip("。.，,").lower() in _NO_MALE
    if not out["labeled"]:
        out["plot"] = raw
    return out


# ── 分集大纲（2026-11-22）：可选，用户逐页点了发生什么就严格照它拆页 ──
# 工作台填了就按行拼进 brief；不填时下面这套完全不触发，行为跟以前一样。
_OUTLINE_LINE = re.compile(r"^\s*第?\s*(\d+)\s*[页：:.]?\s*[:：.、\-]?\s*(.+?)\s*$")
_OUTLINE_HEADER = re.compile(r"^\s*[【\[]?\s*分集大纲\s*[】\]]?\s*[：:]\s*$")
_SKIP_BUCKET = "__outline__"   # 大纲段单独走，不能混进剧情/转折的 bucket


def parse_outline(text):
    """从 brief 里摘出「分集大纲：」段，返回按页号排好的 [(n, 一句话), ...]。

    只认显式 `分集大纲：` 标签，避免把普通剧情里的「1：」误当大纲。
    没填、格式不对都返回空列表——调用方据此走原来的五段式。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    lines, inside, out = raw.splitlines(), False, []
    for line in lines:
        if _OUTLINE_HEADER.match(line):
            inside = True
            continue
        if not inside:
            continue
        # 碰到下一个顶层标签就停（分集大纲一般是 brief 最后一段）
        if _ROLE_LINE.match(line) or _ROLE_GROUP.match(line):
            break
        hit = _OUTLINE_LINE.match(line)
        if hit:
            try:
                n = int(hit.group(1))
            except (TypeError, ValueError):
                continue
            body = (hit.group(2) or "").strip()
            if n > 0 and body:
                out.append((n, body))
    out.sort(key=lambda x: x[0])
    return out


def compose_outline(items):
    """把大纲写成编剧必须照做的硬指令；空返回空串。"""
    if not items:
        return ""
    lines = ["== 分集大纲（用户逐页指定，页数与页序都不许改） ==",
             "严格按下面每一条拆成一页：第 N 条就是第 N 页，"
             "一页一条、不许合并两页、不许跳过、不许在中间加页。",
             "每页的 beat 就是那一条里发生的变化；大纲没写的桥段自己补画面，"
             "但不许另起一页。"]
    for n, body in items:
        lines.append("第%d页：%s" % (n, body))
    return "\n".join(lines)


def compose_brief(parsed, fallback=""):
    """把三段拼成编剧能读的段落：谁在这本里 → 她是谁 → 这一集发生什么。

    旧格式（没有角色行）原样返回，一个字都不加。
    """
    if not parsed.get("labeled"):
        return fallback
    head = ["== 这一本里有谁 =="]
    if parsed.get("male_none"):
        head.append("只有她一个人。所有页、所有格子都只有她，没有第二个人，"
                    "也不存在「镜头后面的我」。")
        head.append("不要给她安排任何对话对象：不写男方框、不写「他」说的话、"
                    "不出现男人的手。她的反应靠表情、动作和她看的东西。")
    else:
        male = (parsed.get("male") or "").strip()
        if male:
            head.append("男主（画外，不出镜）：%s" % male)
        else:
            head.append("男主：这一集没指定，按「有他」处理，但他能不说就不说")
        head.append("他不一定说话：整本一句不说完全正常，"
                    "只有剧情真的需要他回应时才写一句。")
    body = []
    if (parsed.get("female") or "").strip():
        body += ["== 她是谁 ==", parsed["female"].strip()]
    if (parsed.get("plot") or "").strip():
        body += ["== 这一集发生什么 ==", parsed["plot"].strip()]
    # 转折是可选项：用户填了就照它铺 beat，一个字不许绕开；
    # 没填才让编剧在这一集里自己设计一个意外。两种都靠画面讲，不上字。
    twist = (parsed.get("twist") or "").strip()
    if twist:
        body += ["== 这一集要出的意外 ==", "转折（必须出现，不许绕开）：" + twist,
                 "整本围着它排节奏，但不要把它写成台词或旁白——用镜头让读者看出来。"]
    else:
        body += ["== 这一集要出的意外 ==",
                 "转折没指定：自己设计一个只在这一集成立的小意外，"
                 "按「钩子 → 推进 → 意外 → 她的反应 → 收尾」铺到各页。"
                 "不许只是把她做的动作顺序列一遍。"]
    return "\n".join(head + body)


def with_outline(ctx, outline):
    """把分集大纲接到组装好的上下文后面；空大纲原样返回。

    单独抽出来是因为单女主 / 群像两条路都要用，且旧格式（compose_brief 的
    fallback）也得能带上大纲——不然用户逐页点了 10 页，旧 brief 会丢掉它。
    """
    tail = compose_outline(outline)
    return (ctx or "") + ("\n\n" + tail if tail else "")


# ══════════════════════ 群像模式（2026-11-22） ══════════════════════
# 单女主 POV 的硬规则散落在 SYSTEM 多个段落里（「这一本里有谁」/「画什么」/
# 「cast 长这样」/「字」）。群像模式不改那几段——而是在 SYSTEM 末尾追加这一段，
# 明确声明它优先。这样单女主链路一个字不动，群像只是多读一段覆写。
# 大模型对「最后、且明确写着『以下优先』」的指令遵从度很高，实测见 tools 探针。
GROUP_OVERRIDE = """
════════════════════════════════════════════════════
⚠ 群像模式：下面这一段**优先于上面所有规则**。
上面凡是说「只画女生 / 不许男性人形 / 男主只在画外 / 只有两种字 / cast 只写 girl」
的，在群像模式里**全部作废**。这是一本多角色合照，不是第一人称 POV。
════════════════════════════════════════════════════

════ 群像：这一本里有谁 ════
输入里会有一段「== 群像角色 ==」，每个角色一行 `key: 英文外形串`。
· **cast 里每个角色写一条，key 原样照抄**（boy / girl / chubby_uncle / athlete 等），
  外形串逐字照抄，不缩写不调序不改词，也不带衣服。
· 这是**第三人称群像**：镜头是旁观者，所有角色都在画面里，没有「镜头后面的我」，
  没有画外方框。谁说话就把气泡放在**那个人自己在的那一格**。
· 同屏合照那一页：把剧情点名的所有人**画在同一个画面里**，一个都不能少。
  写清谁站哪（left / center / right / foreground / background）、彼此的高矮胖瘦差异。

════ 群像：人多怎么画清楚 ════
· 每个角色第一次全身入画时写完整外形串；同一页后面的格用 `the boy` / `the athlete`
  这样的指代（key 是 chubby_uncle 就写 the chubby man）。
· **相邻两个人开头显式互斥**，否则模型会把他们画成同一张脸：
  `In sharp contrast, the athlete has a broad muscular build and short cropped hair, unlike the slim boy beside him.`
  体型差异（胖 / 瘦 / 肌肉）、年龄（少年 / 中年）、发型三项至少互斥一项。
· 特写只写那一个人的局部，别把一堆外形串堆进脸特写。
· 人数多的那一格用宽景别（wide / full group shot）装下所有人，别用 tight close-up。

════ 群像：字 ════
· 每个会说话的角色各写一种气泡，气泡落在他自己那一格，短尾巴指着他的脸。
· 可说话的人最多四种，一页台词仍是上限不是配额；整页 0 句成立。
· 不许旁白、不许内心独白。

════ 群像：面对谁，镜头就从背后看 ════
· 一个角色「面对 / 冲向 / 攻击 / 施法朝」某样东西（敌人、门、人群）时，
  用背对镜头或侧后机位：`seen from behind at a three-quarter angle, facing the monsters`
  或 `back turned toward the viewer, body aimed at the enemy across the field`。
  让读者看见她**朝着目标**，不是朝着读者。
· **不许**写 `facing the camera` / `front view` 又同时说她 facing the enemy——
  这两句打架，模型只会画正面朝镜头，看着像回头看这边（2026-11-22 异界前线 P6/P8 实测）。
  要脸就另开一格脸特写（close-up of her face），主格专心交代朝向。
· 反派 / 目标写在画面深处（`monsters across the middle distance`），别写成
  `in front of her`——模型算不出「她的前方」是哪个方向。

════ 群像：多页怎么接 ════
· 多页时先按**时间顺序**把剧情排好：第 1 页是起点，最后一页是收尾。
  不许把后面的场景插到前面，也不许下一页无声跳回更早的地点（门口 / 楼梯 / 天台）。
· 每页都要写 `link`：第 1 页写这一集从哪里开始；后面每页写清「接上一页：还在哪儿、
  谁站在哪、身体停在什么姿势、谁手里拿着什么」。代码只查这一项写没写，内容由你负责。
· 换地点必须写进 `beat` / `link`，并让下一页第一格就发生在新地方（推门出去 /
  走下楼梯 / 穿过走廊）；不许只换一句场景串就把人搬走。
· 上一页出现的关键道具（纸条 / 手机 / 书包 / 饮料）下一页要么还在谁手里，
  要么写明已经放下 / 被风吹走 / 收进口袋；不许凭空消失又冒出来。
· 会说话的角色每页位置要接得上：上一页站在左边，下一页还在左边，除非写明两人换了位置。

════ 群像：比例与衣服 ════
· 「比例锚点只针对少女」那条只对 girl 适用：男孩、大叔、健身者按各自身形写，
  不要把 `slender teen proportions` 套到成年男性身上。
· 衣服仍不进 cast，写进每一页 summary 的③；同屏时每人衣服各写清楚。
"""

_ROLE_GROUP = re.compile(
    r"^[\s#*\-]*(?:[【\[]?\s*群像角色\s*[】\]]?\s*[：:])\s*(.*)$")


def parse_group(text):
    """群像输入：认 `群像角色：` 段里的多行 `key: 外形串`，外加剧情 / 转折。

    返回 `{"cast": {key: spec}, "plot": ..., "twist": ...}`。没有群像角色行时
    cast 为空（调用方据此退回单女主，不误开群像）。
    """
    raw = (text or "").strip()
    out = {"cast": {}, "plot": "", "twist": ""}
    if not raw:
        return out
    cur = None
    for line in raw.splitlines():
        # 同 parse_brief：大纲段单独取，不能混进 plot / twist。
        if _OUTLINE_HEADER.match(line):
            cur = _SKIP_BUCKET
            continue
        if cur == _SKIP_BUCKET:
            continue
        hit = _ROLE_GROUP.match(line)
        if hit:
            cur = "cast"
            rest = (hit.group(1) or "").strip()
            if rest:
                buckets_cast(rest, out["cast"])
            continue
        h2 = _ROLE_LINE.match(line)
        if h2:
            tag = h2.group(1) or h2.group(2)
            cur = tag
            val = (h2.group(3) or "").strip()
            if tag == "剧情" and val:
                out["plot"] += ("\n" if out["plot"] else "") + val
            elif tag == "转折" and val:
                out["twist"] += ("\n" if out["twist"] else "") + val
            continue
        if cur == "cast" and line.strip():
            buckets_cast(line.strip(), out["cast"])
        elif cur == "剧情" and line.strip():
            out["plot"] += ("\n" if out["plot"] else "") + line.strip()
        elif cur == "转折" and line.strip():
            out["twist"] += ("\n" if out["twist"] else "") + line.strip()
    return out


def buckets_cast(line, into):
    """把一行 `key: 外形串` 写进 cast。key 规范成英文小写下划线。"""
    if ":" in line:
        key, val = line.split(":", 1)
    elif "：" in line:
        key, val = line.split("：", 1)
    else:
        return
    key = re.sub(r"[^a-z0-9_]", "", key.strip().lower())
    val = val.strip()
    if key and val:
        into[key] = val


def compose_group(parsed):
    """群像输入 → 编剧能读的段落。"""
    body = ["== 群像角色 =="]
    for key, val in (parsed.get("cast") or {}).items():
        body.append("%s: %s" % (key, val))
    if (parsed.get("plot") or "").strip():
        body += ["== 这一集发生什么 ==", parsed["plot"].strip()]
    twist = (parsed.get("twist") or "").strip()
    if twist:
        body += ["== 这一集要出的意外 ==", "转折（必须出现）：" + twist]
    else:
        body += ["== 这一集要出的意外 ==",
                 "转折没指定：自己设计一个只在这一集成立的小意外，"
                 "按「钩子 → 推进 → 意外 → 反应 → 收尾」铺到各页。"]
    return "\n".join(body)


# ══════════════════════ 体检 ══════════════════════
def _cells(p):
    return [tuple(i.get("bbox") or ()) for i in (p.get("items") or [])
            if i.get("label") in ("character", "object")]


def _texts(p):
    return [i for i in (p.get("items") or []) if i.get("label") == "text"]


def _overlap(a, b):
    if len(a) != 4 or len(b) != 4:
        return False
    return (min(a[2], b[2]) - max(a[0], b[0]) > 1
            and min(a[3], b[3]) - max(a[1], b[1]) > 1)


def _inside(tb, cb, pad=0):
    if len(tb) != 4 or len(cb) != 4:
        return False
    return (tb[0] >= cb[0] - pad and tb[1] >= cb[1] - pad
            and tb[2] <= cb[2] + pad and tb[3] <= cb[3] + pad)


def _cast_segs(spec_str):
    return [s.strip() for s in re.split(r"[,，]", spec_str or "") if s.strip()]


# 外形串里的年龄：只认「about X」（英文数词或阿拉伯数字都归一成整数）。
# 模型改写年龄时一定还写 about，所以这一条判据不会漏；取不到 age 就不查。
_AGE_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
              "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
              "nineteen": 19}


def _ages(text):
    out = set()
    for m in re.finditer(r"about\s+([a-z]+|\d{1,2})", (text or "").lower()):
        tok = m.group(1)
        if tok.isdigit():
            out.add(int(tok))
        elif tok in _AGE_WORDS:
            out.add(_AGE_WORDS[tok])
    return out


# 「一格一个定格」的三条判据。全是**封闭句式**，不是衣物词表准入：
# 命中就说明这一格真的写了两个状态，改法唯一（拆格 / 改成确定的瞬间），
# 所以算 blocking 交给模型重修；漏检只是少提示一次，不会误伤稿子。
_CLOTH = (r"(?:t-shirt|shirt|tee|blouse|dress|gown|uniform|jersey|sweater"
          r"|cardigan|jacket|coat|hoodie|jumper|skirt|shorts|pants|trousers"
          r"|underlayer|towel"
          # 2026-11-22 补：少这几个，温泉话题整本的浴衣/牛仔裤根本进不了检测，
          # 「穿一件拿一件」「中间态」在换装剧情里全部静默失效（实跑发现）。
          r"|yukata|kimono|robe|jeans|socks|stockings|tights|swimsuit"
          # 2026-11-24 补：`<材质/颜色> top` 是最常见的上衣写法（sailor top /
          # tank top / crop top），旧表达式只认 tank top 这一个死字符串，
          # 于是「她穿 a navy blue sailor top」被当成没写衣服。
          r"|(?:[a-z]+ )?top)")


# 中文大纲里「这句话在讲穿着」的粗判（2026-11-25）。
#
# 用途只有一个：`audit()` 靠它决定「要不要把作者原话摆到审查者面前」。
# **不是**衣着词表闸门——漏检只是少提示一次，误检只是多一条让模型自查的
# 提示，两种情况都不会拦稿、不会替模型判断穿得对不对。
# 所以这里刻意写宽：衣着名、穿脱动作、以及「没了 / 光着 / 只穿」这类减法表述。
_OUTFIT_CN = re.compile(
    r"衣服|服装|衣着|服饰|外套|大衣|夹克|羽绒|棉袄|毛衣|卫衣|衬衫|衬衣|T恤|"
    r"裙子|裙|裤子|牛仔裤|短裤|睡衣|浴衣|和服|泳装|泳衣|内搭|内衣|背心|吊带|"
    r"袜子|丝袜|鞋|靴|围巾|帽子|手套|领带|校服|制服|披风|斗篷|护甲|"
    r"裸|光着|光身|一丝不挂|赤身|袒露|"
    r"穿|脱|褪|换|披|裹|摘下|取下")


def _mentions_cloth(text):
    """这一句（中文大纲原话）有没有在讲穿着。只做粗判，不判断内容。"""
    raw = str(text or "")
    if not raw.strip():
        return False
    if _OUTFIT_CN.search(raw):
        return True
    # 大纲也可能直接写英文衣物（作者抄了英文串进来）
    return bool(re.search(_CLOTH, raw.lower()))

# 半脱：动作的起点和终点叠在一格里。这些句式一旦出现，出图必然穿一件拿一件。
# 注意不认裸的 halfway——"a hand lifted halfway and frozen" 是合法的定格。
_MIDSTATE = (r"half[- ]?off|mid[- ]?change|one bare shoulder|one shoulder bare"
             r"|bare shoulder (?:just )?covered|one arm (?:already )?(?:pulled )?(?:out of|through)"
             r"|pulled down to her waist|slipping (?:it|the [a-z -]+) over"
             r"|pulling (?:it|the [a-z -]+) (?:up )?over"
             # 「正在落/正在套」也是过程：catching … as it settles over her shoulders
             # 等于同一格里既接住又穿好（2026-11-22 更衣话题 P5 实跑）。
             r"|as it (?:settles|slips|slides) (?:over|down)"
             # 浴衣「正从肩上滑落」= 还穿着 + 正在脱，同一格两个状态
             # （2026-11-22 温泉话题 P3 实跑）。主语必须是衣物——裸的
             # `sliding down` 会命中「汗珠滑下脖子」，实测误报过一次。
             + r"|" + _CLOTH + r"\s+(?:sliding|slid|slides?|slipping|slipped) (?:off|down)"
             # 脱到一半的另外两种写法（2026-11-22 更衣话题 P3 实跑）：
             # the shirt pulled halfway up her torso / dragging the shirt up from her waist
             + r"|pulled halfway (?:up|down)"
             + r"|dragging (?:it|the [a-z -]+) (?:up|down)"
             + r"|tugging (?:it|the [a-z -]+) (?:up|down)")

# 同一格里两个「已经/刚」= 两个时间点塞进一格（already pulled through / just covered）。
_PHASE = r"\b(?:already|just)\b"


def _cloth_phrases(caption):
    """抽 <修饰…> <衣物词> 的短语，去掉冠词和所有格后返回。

    同一串在同一格里出现两次，说明这件衣服同时被写成「穿在身上」和「拿在手里」：
    "a soaked white short-sleeve sport shirt and loose white running shorts,
     her fingers gathering the soaked white short-sleeve sport shirt into a tight ball"
    合法的一格两件一定带不同修饰（soaked … vs fresh dry …），不会同串。
    """
    out = []
    for m in re.finditer(r"\b((?:[a-z][a-z-]*\s+){0,4})(" + _CLOTH + r")\b",
                         (caption or "").lower()):
        mods = re.sub(r"\b(?:a|an|the|her|his|its)\b", " ", m.group(1))
        out.append(" ".join((mods + " " + m.group(2)).split()))
    return out


# 「同一件衣服既穿着又拿在手里」只认**离体动作**。两处都写在穿着语境里，是
# 同一格把人写了两遍（啰嗦、不矛盾），不该当矛盾去重修。
# 2026-11-22 实测：更衣话题 P1 背身格先写「背后的湿衬衫 + 短裤」，句末又贴了
# 一遍完整外形串和衣着，旧判据（同串出现两次）把它报成「穿一件拿一件」，
# 白烧两轮重修还是不过——判据必须看语境。
_OFF_BODY = (r"held|holding|balled up|clutched|clutching|dangling|carried|carrying"
             r"|in her (?:right |left )?hand|gathering|gathered|peeled|stripped"
             r"|slipped off|off her body|draped|hanging from her"
             r"|catching|reaching for|settl(?:es|ing) over|pulling on|slipping into")
_ON_BODY = r"wearing|dressed in|clad in"


def _wear_hand_clash(caption):
    """同一件衣服，一处写在身上、另一处写在手里。返回这些衣物短语。"""
    low = (caption or "").lower()
    ph = _cloth_phrases(low)
    dup = {x for x in ph if x and ph.count(x) > 1}
    if not dup:
        return []
    sents = re.split(r"[.;]", low)
    out = []
    for p in dup:
        kinds = set()
        for s in sents:
            if p not in s:
                continue
            if re.search(_OFF_BODY, s):
                kinds.add("off")
            if re.search(_ON_BODY, s):
                kinds.add("on")
        if "off" in kinds and "on" in kinds:
            out.append(p)
    return sorted(out)


# 纯局部特写（脸、手、脚）：画面里没有身体，不需要全身外形串。往里塞
# 「long legs / wearing …」，出图只能把镜头拉远成中景。
# 判据本体在 `moss_twopass.TIGHT_CU`——那边的外形串补齐也要用同一条。
_TIGHT = M.TIGHT_CU
# 特写里**带脸**的那一类（脸、眼睛、嘴、头发、后颈）。只有这一类才该核对
# 「还是同一个人」；手、脚、肩的特写里根本没有脸，要求写 hair/eyes 等于
# 逼它把镜头拉开——2026-11-22 实测便利店：一格「低头数硬币的手」被这条判
# 死，连修三轮还是红，49.8s 白烧，autofix 之后落盘稿其实是对的。
_TIGHT_FACE = re.compile(r"close[- ]up (?:of|on) her (?:[a-z-]+ ){0,2}"
                         r"(?:face|eyes|mouth|lips|head|hair|nape|chin|cheek)", re.I)
_FACE_WORDS = ("hair", "headband", "bangs", "eye", "face")

# 新场景首次出场的角色：光杆称呼＝没交外形。
# 只认「冠词 + 泛称」这个可数事实，不列服饰/发色词表（那是模型的事）。
#  deliberate 不收 girl：女主有下面专门的 cast 外形串逐项核对，别重复拦。
_GEN_ROLE = re.compile(
    r"\b(?:a|an|the|two|three|several)\s+"
    r"(?:guard|man|woman|person|someone|somebody|figure|boy|"
    r"villager|townsfolk|soldier|knight|stranger|customer|clerk|"
    r"creature|monster|beast|enemy)\b", re.I)
# 只要这一格里给了种族或任一可见外形锚，就算交了——具体写什么由模型决定。
# 含 human/goblin 这类种族词：同一格「a human guard … the guard」不再重复拦。
_ROLE_LOOK = re.compile(
    r"\b(?:human|goblin|orc|elf|dwarf|demon|beast|animal|"
    r"hair|eye|face|skin|build|muscular|slim|stout|tall|short|"
    r"young|old|teen|beard|moustache|helmet|cloak|uniform|hat|"
    r"grey|gray|brown|blond|black|white|green|pale)\b", re.I)


def audit(js, cap=12, male_none=False, group=False, outline=None):
    """只看可数的事实。返回 `(blocking, warns)`。

    `blocking` 是要模型改的：代码补不回来的（格子对不上骨架、文字跑出格子、
    外形串漏项、台词重复、声明没男主却出现男方框）。`warns` 是代码能兜底的
    （尾句、页数超限、空字段），只记进报告，不拿它去多烧一次重修——
    autofix 会重建 summary、截断页数。
    `male_none=True` 来自输入里的「男主：无」：这一集没有他的话，就不该有他的框。
    `group=True` 是群像模式：跳过 POV 那套「只画女生」的判据，cast 也不再强制只有 girl。
    `outline` 是 `parse_outline()` 的结果 `[(n, 原文), ...]`：用户逐页点名的原话。
    **只在「这一页原话里提到衣着」时**，把原话和这一页 outfits 并排摆出来，交给
    模型自己判断对不对——代码不判「穿得对不对」，只判「这里有一句作者的话，
    你必须自己核对过」。见下面 `_OUTLINE_CLOTH_HINT` 那段。
    """
    out = []
    warns = []
    pgs = js.get("pages") or []
    if not pgs:
        return ["没有产出任何页"], []
    if len(pgs) > cap:
        warns.append("页数 %d 超过上限 %d（会截断）" % (len(pgs), cap))
    # 配色表缺了不会让这一本出不了图，但每页颜色会各画各的。这是事实可数，
    # 记进报告让编剧知道；措辞、选什么颜色仍然是它的活，代码不猜不填。
    if not str(js.get("palette") or "").strip():
        warns.append("整本没有 palette 配色串——全彩页会逐页漂色；顶层写一次 4~5 个具体颜色")

    cast = js.get("cast") or {}
    girl_spec = cast.get("girl") or ""
    girl_segs = _cast_segs(girl_spec)
    if group:
        # 群像：每个角色的外形串都得非空；不强制 girl、不强制比例锚点
        # （那些锚是给少女用的，套到胖大叔/健身者身上反而画坏）。
        if not cast or not all(isinstance(v, str) and v.strip()
                               for v in cast.values()):
            out.append("群像 cast 有空外形串——每个角色都要写一条英文外形串")
    else:
        if not girl_segs:
            out.append("cast 里没有 girl 外形串")
        elif len(girl_segs) < 6:
            out.append("cast 的 girl 外形串只有 %d 项，太少（年龄、身材、发型+头带、眼睛、脸、配饰写满这几样）"
                       % len(girl_segs))
    # 比例锚点：出图端靠它把「十几岁的少女」和「Q 版大头娃娃」分开。这一项不是文风，
    # 是同一本里体型一致性的唯一锚——2026-11-22 泳池话题实测：同一份 cast，
    # P1 是正常少女，P2 靠池边喘气那一格直接画成三头身。判据只看有没有写（模型漏了这一项），
    # 不查它用什么词——措辞是模型的活。
    if girl_spec and "proportion" not in girl_spec.lower():
        out.append("cast 里缺比例锚点——补一项 `slender teen proportions, long legs, small head`，"
                   "每一格原样抄：缺了它，笑脸大动作的格子会被画成 Q 版")
    # 2026-11-21：cast 里写衣服 = 这本书换不了装。
    # 判据只认一个词「wearing」（以及 dressed in / clad in），不是衣物词表：
    # 外形串里写衣服几乎必然用这个句式，命中即事实，改法也唯一——把这段剪进本页 ③。
    # 群像时检查每个角色，不是只查 girl。
    for role_key, role_spec in (cast.items() if group
                                else [("girl", girl_spec)]):
        cloth = re.search(r"\b(wearing|dressed in|clad in)\b",
                          role_spec or "", re.I)
        if not cloth:
            continue
        if group:
            out.append("cast.%s 写了衣服（%s …）——衣服不许进 cast：挪进每一页 summary 的 ③，"
                       "cast 只留外形，否则整本换不了装" % (role_key, cloth.group(1)))
        else:
            out.append("cast 里写了衣服（%s …）——衣服不许进 cast：把这一段挪进每一页 summary 的 ③，"
                       "cast 只留年龄、身材、发型、头带、眼睛、脸、配饰，否则整本换不了装"
                       % cloth.group(1))
    # 年龄是外形串里最容易被改写的一项：模型会顺着剧情把她写成 another age。
    # 只比数字（about twelve / about 18 都归一成 12），不比措辞——这是事实，不是文风。
    cast_ages = _ages(girl_spec)
    cast_age = sorted(cast_ages)[0] if len(cast_ages) == 1 else None

    # 多页群像的连贯：代码只查「link 交没交」，不判断它写得对不对。
    # 没写就得回去补——这是结构事实；写了什么由编剧决定，模型自己的活。
    if group and len(pgs) > 1:
        for n, p in enumerate(pgs, 1):
            if not str((p or {}).get("link") or "").strip():
                out.append("第%d页：没写 link——第 1 页写这一集从哪里开始，"
                           "后面每页写清接上一页的地点/站位/姿势/手里东西" % n)

    # 服饰连贯（2026-11-22 魔法少女 P5 脱了裙子还穿裙子、P6 变身服复活）：
    # 多页剧本每页必交 outfits——「这一页身上是什么」的结构化状态。缺了出图端
    # 就退回旧衣。字段级「没交作业」记 blocking（同 link）；状态对不对、
    # 有没有写回旧衣，由重修轮和审稿轮的模型判断，代码不猜。
    if len(pgs) > 1:
        for n, p in enumerate(pgs, 1):
            of = (p or {}).get("outfits")
            if not (isinstance(of, dict) and of
                    and all(str(v or "").strip() for v in of.values())):
                out.append("第%d页：没写 outfits（这一页每个出场角色身上是什么）"
                           "——服饰连贯全靠它，缺了出图会穿回旧衣" % n)

    # 每格写没写衣服（2026-11-24 用户：「每页有时候都不去写角色的服饰特征……
    # 角色穿什么衣服，每个格子都给我写清楚，不要指望写一个女生就行了」）。
    #
    # 这是**没交作业**，不是内容好坏：看得到身体的格子里一个衣物词都没有，
    # 就是没有交代她穿什么。判断用的是「这一页 outfits 里那几个衣物词有没有
    # 落到这一格里」——词来自模型自己写的 outfits，不是我另造的词表，所以
    # 不存在「我的词表盖不全」的问题：它穿什么由它定，我只要它抄回来。
    # 命不中就记 blocking 交给重修轮，绝不在代码里替它编一套衣服。
    if len(pgs) > 0:
        for n, p in enumerate(pgs, 1):
            of = (p or {}).get("outfits")
            if not isinstance(of, dict) or not of:
                continue
            words = set()
            for v in of.values():
                words.update(re.findall(_CLOTH, str(v or "").lower()))
            if not words:
                continue

            def _covered(cap, _w=words):
                low = str(cap or "").lower()
                return any(re.search(r"\b" + re.escape(w) + r"\b", low)
                           for w in _w)

            bad = []
            for it in (p.get("items") or []):
                if not isinstance(it, dict) or it.get("label") != "character":
                    continue
                cap = str(it.get("caption") or "")
                if not cap.strip():
                    continue
                if M.TIGHT_CU.search(cap.lower()):
                    continue            # 纯局部特写：画面里没有身体
                if not _covered(cap):
                    bad.append(str(it.get("id") or it.get("name") or "?")[:12])
            if bad:
                out.append("第%d页：格子 %s 里没写她穿什么——这一页的 outfits 是「%s」，"
                           "照抄进每一格看得到身体的 caption（只写一个女生＝没交作业）"
                           % (n, "/".join(bad), "; ".join(
                               str(v) for v in of.values())[:80]))

    # 大纲点名的衣着 vs 实际交的 outfits（2026-11-25 用户：「有时候我会写没有服装，
    # 或者裸体……AI 后面那个提示词会写死了服饰」「不能让 AI 写死」）。
    #
    # 代码只做**检测**：这一页的大纲原话里有没有在讲穿着。讲了，就把原话和
    # 这一页 outfits 并排摆出来，要求模型自己核对——「对不对」由它判，
    # 因为它看得懂「不穿」和「赤着上身」是同一件事，代码看不懂。
    # 这里不查「准不准」，只保证「作者这句话被摆在审查者面前过」。
    # 用词表判「这句提没提衣服」不算闸门：漏检只是少提示一次，不会误杀稿子。
    for n, body in (outline or []):
        if n < 1 or n > len(pgs):
            continue
        if not _mentions_cloth(body):
            continue
        of = (pgs[n - 1] or {}).get("outfits")
        if not isinstance(of, dict) or not of:
            continue                     # 缺 outfits 上面已经报过，不重复
        out.append("第%d页：作者在大纲里点名了衣着「%s」，这一页 outfits 写的是「%s」"
                   "——自己核对一遍：大纲说穿什么就穿什么，大纲说脱掉 / 不带 / "
                   "不着衣物，outfits 就必须真的没有它，不许因为上一页有就让它复活；"
                   "对上了就原样保留，一个字都别改" % (n, str(body)[:60], "; ".join(
                       str(v) for v in of.values())[:80]))

    # 场景引用式写法（2026-11-22 粉光除粘液：P2 the same stone plaza 丢了 dusk
    # 画成白天、P6 the same bathroom 丢了瓷砖浴缸换了布局）：每页出图独立送画，
    # 「the same …」到出图端只剩光杆地名。引用词是封闭集合（same 一词基本只在
    # 引用时出现），查词不算盖不全的开放集；串自不自包含、时间光线写没写全，
    # 判断权在重修轮和审稿轮的模型。
    if len(pgs) > 1:
        scenes = js.get("scenes") or {}
        for n, p in enumerate(pgs, 1):
            sc = str(scenes.get(str((p or {}).get("scene") or "")) or "").strip().lower()
            if sc and re.search(r"\bsame\b", sc):
                out.append("第%d页：scene 串用了「the same …」引用式写法——每页出图是"
                           "独立送画的，必须写全自包含串：地点 + 时间 + 光线 + 材质 + 物件"
                           % n)

    seen_lines = {}
    for n, p in enumerate(pgs, 1):
        # beat 是这一页发生了什么变化；缺了它整本就退回动作流水账。
        # 记 warns 不记 blocking：它是内容质量，不是「没交作业」。
        if not str(p.get("beat") or "").strip():
            warns.append("第%d页：没有 beat（这一页发生了什么变化，不是她做了什么动作）" % n)
        lay = (p.get("layout") or "").strip().upper()
        sk = M.SKELETONS.get(lay) if lay in AGENT_LAYOUTS else None
        if not sk:
            out.append("第%d页：版式 %r 不在 L0/S1/D1/T3 里（只允许 1、2、3、5 格，不要四格、不要左右通高两格）"
                       % (n, lay))
            continue
        cells = _cells(p)
        if sorted(cells) != sorted(tuple(c) for c in sk):
            miss = [c for c in sk if tuple(c) not in cells]
            extra = [c for c in cells if list(c) not in sk]
            msg = "第%d页：格子跟 %s 对不上" % (n, lay)
            if miss:
                msg += "，缺 %s" % miss
            if extra:
                msg += "，多出 %s" % extra
            out.append(msg)
        for i in range(len(cells)):
            for k in range(i + 1, len(cells)):
                if _overlap(cells[i], cells[k]):
                    out.append("第%d页：格子重叠 %s / %s" % (n, list(cells[i]), list(cells[k])))

        # 每个格子都要有人或物
        for c in sk:
            if tuple(c) not in cells:
                out.append("第%d页：格 %s 里没有 character/object" % (n, list(c)))

        # 文字：必须有 text、必须落在某个格子里
        tcnt = 0
        for t in _texts(p):
            tcnt += 1
            txt = (t.get("text") or "").strip()
            if not txt:
                out.append("第%d页：有一条 text item 的 text 是空的" % n)
                continue
            if len(txt) > 26:
                out.append("第%d页：「%s」太长（%d 字），拆成两框" % (n, txt[:12], len(txt)))
            # 2026-11-22：旁白框取消——那一格方框只留给男主的台词。
            # 判据是容器名（模型自己写的 caption），不是台词内容：
            # 有 caption box 又没有 boy speech box，就是她又在心里说话。
            tcap = (t.get("caption") or "").lower()
            if (not group and "caption box" in tcap
                    and "boy speech box" not in tcap):
                out.append("第%d页：「%s」用了旁白框——这一集只有她的圆气泡和他的直角方框，"
                           "旁白/独白一律不写，让她自己说出来" % (n, txt[:10]))
            # 声明「这一集没有男主」却画了男方框：不是风格问题，是这一页凭空多了一个人。
            # 判据只认容器名（结构事实），不猜台词内容归谁——归属是编剧的活。
            if male_none and "boy speech box" in tcap:
                out.append("第%d页：「%s」是男主的方框，但这一集没有男主——"
                           "删掉它；她要说话就用她的圆气泡，不需要说话就整格无字"
                           % (n, txt[:12]))
            tb = tuple(t.get("bbox") or ())
            # 文字框位置由 autofix 按公式重排，模型算歪了不算它的错
            if not any(_inside(tb, c, pad=8) for c in sk):
                warns.append("第%d页：「%s」的框算到格子外了（autofix 会重排）"
                             % (n, txt[:10]))
            if seen_lines.get(txt):
                out.append("第%d页：「%s」在第%d页已经用过，换一句"
                           % (n, txt[:12], seen_lines[txt]))
            else:
                seen_lines[txt] = n
        if tcnt > 6:
            warns.append("第%d页：文字 %d 条，可能挤" % (n, tcnt))

        # 尾句由 autofix 按本页实际文字改写（retail_caption），这里只记一笔
        caption = ((p.get("summary") or {}).get("caption") or "").lower()
        has_boy = any("boy speech box" in (t.get("caption") or "").lower() for t in _texts(p))
        if caption and "off-screen boy" in caption and not has_boy:
            warns.append("第%d页：尾句写了男主画外音，但本页没有男主方框（autofix 改尾句）" % n)
        if caption and has_boy and "off-screen boy" not in caption:
            warns.append("第%d页：本页有男主方框，尾句没写（autofix 改尾句）" % n)

        # 新角色写了没有：换场景第一次出场的非女生角色，只写 a guard / the man
        # 这种光杆称呼＝没交外形，模型会拿前一页的角色填空。
        # 2026-11-22 异界女兵破围实测：前九页全哥布林，第十页只写 a guard，
        # 就把人类村庄守卫画成了哥布林。
        # 代码只查「有没有交外形锚」，不判断外形写得对不对——措辞是模型的活。
        # 群像模式有自己的 cast 外形串检查，整段跳过。
        if not group:
            for it in (p.get("items") or []):
                if it.get("label") == "text":
                    continue
                cc = (it.get("caption") or "").lower()
                hit = _GEN_ROLE.search(cc)
                if hit and not _ROLE_LOOK.search(cc):
                    out.append("第%d页：新出场角色只写了「%s」没写种族+外形——"
                               "写成 a human guard with short grey hair, a steel helmet 这样，"
                               "否则模型会拿前几页的角色填空（哥布林村庄事件）" % (n, hit.group(0)))
                    break

        # 一格一个定格：一格只许一个状态。一格同时写了「动作」和「动作的结果」，
        # 出图就会画成身上一件、手里一件。每页最多报两条，别让重修消息被刷屏。
        zhit = 0
        for it in (p.get("items") or []):
            if it.get("label") != "character" or zhit >= 2:
                continue
            cc = (it.get("caption") or "").lower()
            what = how = ""
            mid = re.search(_MIDSTATE, cc)
            if mid:
                what, how = ("写了中间态「%s」" % mid.group(0),
                             "一格只能一个状态：要么还穿着，要么已经脱在手。"
                             "要画正在脱，就画确定的那一刻（湿衣翻套在头上遮住脸 / 整件已经褪到手里）")
            else:
                ph = _cloth_phrases(cc)
                dup = [x for x in set(ph) if ph.count(x) > 1 and x]
                phases = re.findall(_PHASE, cc)
                clash = _wear_hand_clash(cc)
                if clash:
                    what, how = ("同一格把「%s」写了两次（一次当衣着、一次拿在手里）" % clash[0],
                                 "一格只许一个衣物归属，拆成两格：一格脱净、一格递出")
                elif dup:
                    # 两处都在「穿着」语境：描述重复，不矛盾。不拦，只提醒。
                    warns.append("第%d页：「%s」在同一格里写了两遍（都是穿着），"
                                 "一个字只说一次" % (n, dup[0]))
                elif len(phases) >= 2:
                    what, how = ("同一格里有 %d 个进行中的阶段（%s）" % (len(phases), "、".join(phases)),
                                 "一格只停在一个瞬间：接住就只是接住，穿好就只是穿好")
            if what:
                out.append("第%d页：%s——%s" % (n, what, how))
                zhit += 1

        # 外形串是否原样粘贴。判「这一格画的是不是她」只认 girl 这个词——
        # 模型多数时候写 "a girl about twelve"，极少写 danbooru tag 式的 1girl，
        # 原来按 1girl 判会让这条检查在大多数页上直接跳过。
        chars = [i for i in (p.get("items") or []) if i.get("label") == "character"]
        age_hit = 0
        for ch in chars:
            cc = (ch.get("caption") or "").lower()
            if "girl" not in cc:
                continue
            # 年龄只看数：cast 写 about twelve，这一格写成 about eighteen 就是换了个人。
            if cast_age and age_hit < 1:
                wrong = [a for a in _ages(cc) if a != cast_age]
                if wrong:
                    out.append("第%d页：年龄写成 about %s，cast 里是 about %s——"
                               "外形串照抄，年龄一个数都不许改（改了就换了一个人）"
                               % (n, wrong[0], cast_age))
                    age_hit += 1
            if _TIGHT.search(cc):
                # 纯局部特写里没有身体：要求全身外形串等于逼它把镜头拉开。
                # 带脸的那一类（脸/头发/后颈）才核对「还是同一个人」；
                # 手、脚、肩的特写连脸都不在画面里，不查这一项。
                if _TIGHT_FACE.search(cc):
                    got = sum(1 for w in _FACE_WORDS if w in cc)
                    if got < 3:
                        out.append("第%d页：脸特写格几乎没写她的脸和头发（%d/5 个要素）——"
                                   "脸就在画面里，得是同一个人" % (n, got))
                continue
            miss = [s for s in girl_segs if s.lower() not in cc]
            if miss:
                # autofix 的「补齐 girl 外形串」会补回来（包括模型写 "she stands…"
                # 不带 girl 的那一格），所以不拿它去多烧一轮重修。
                # 实测温泉话题：模型重修时把外形串写掉，audit 判 blocking →
                # 连修三轮、最后还是红，落盘稿却是完整的。
                warns.append("第%d页：人物 caption 漏了外形串 %s（autofix 会补）"
                             % (n, "、".join("「%s」" % m for m in miss[:3])))
                break

    return out, warns


# ══════════════════════ 链路 ══════════════════════
def _stamp_palette(pj, palette):
    """把整本配色串原样接到一页 caption 后面（全彩一致性）。

    让模型每页自己抄，它一定会顺手换同义词（navy → dark blue），同一件
    衣服就变成两件。这里是逐字复制，不猜、不改词、不替它选颜色。
    """
    palette = str(palette or "").strip().rstrip(".")
    if not palette or not isinstance(pj.get("summary"), dict):
        return
    sm = pj["summary"]
    sm["caption"] = ((sm.get("caption") or "").strip().rstrip(".")
                     + ". Fixed palette for every panel: " + palette + ".")


def _stamp_outfits_cells(pj, outfits):
    """把这一页的衣着状态逐格接进每个「人物的格子」的 caption。

    为什么非要逐格钉（2026-11-24 用户原话）：「我要求 ai 严格按照我的剧情大纲
    来，因为现在 ai 出现的问题就是，每页有时候都不去写角色的服饰特征，导致
    有时候我要求显示不同服饰都不行，角色穿什么衣服，每个格子都给我写清楚，
    不要指望写一个女生就行了」。

    根因不是模型不肯写——是**格子里的衣着只是参考描述**，页 caption 里那句
    Outfit state 也不是硬指令，出图模型照样可以按「一个女生」自己编。这里
    把 outfits 原文接在格子 caption 末尾（`wearing: …`），一个词都不改：

      · 只接 character 格：object 格（特写道具、空镜）本来就不该出现衣服，
        接上去反而会把镜头拉远去拍身上；
      · 格子里已经把这串写全了就不重复（同一格写两遍会让模型画成两件衣服）；
      · 纯局部特写（脸 / 手 / 脚）也不接：那张画面里没有身体，写整件衣服
        只能把镜头硬拉开——和 `moss_twopass` 里 TIGHT_CU 那条同一个道理。
    """
    if not isinstance(outfits, dict) or not outfits:
        return
    if not isinstance(pj.get("items"), list):
        return
    pairs = [(k, str(v).strip().rstrip("."))
             for k, v in outfits.items() if str(v or "").strip()]
    if not pairs:
        return
    for it in pj["items"]:
        if it.get("label") != "character":
            continue
        cap = str(it.get("caption") or "")
        low = cap.lower()
        if not low.strip():
            continue
        # 纯局部特写：画面里没有身体，不接整件衣服
        try:
            if M.TIGHT_CU.search(low):
                continue
        except Exception:                   # noqa: BLE001
            pass
        # 按**每一个角色**分别看：这一格自己写全了谁就不补谁，只补漏掉的那份。
        # 判据用整串包含——模型自己写对的时候通常一字不差照抄而不是改写。
        need = [(k, v) for k, v in pairs if v.lower() not in low]
        if not need:
            continue
        bits = "; ".join("%s wears: %s" % (k, v) for k, v in need)
        it["caption"] = (cap.rstrip().rstrip(".") + ". " + bits + ".")


def _stamp_outfits(pj, outfits, note=""):
    """把这一页的衣着状态原文接到页 caption 后面（服饰连贯性）。

    outfits 是编剧逐页维护的「这一页身上是什么」。靠模型在格子里自觉抄，
    一定会有格子漏抄 / 写回旧衣（2026-11-22 魔法少女实测：P5 已经脱了裙子
    还画成穿着裙子、P6 该穿水手服变身服又复活）。这里和 palette 同一套
    做法：逐字复制，不猜、不改词、不替它选衣服。

    `note`（2026-11-25）：用户在大纲里给这一页点名的原话，接在最后当锚。
    outfits 是编剧转述的英文，出图模型只看到转述、看不到用户原话；大纲
    点名「不穿 / 脱掉 / 换掉」的页，一旦转述漂了，光看英文没人能发现。
    这里只是**把原话附上**，不翻译、不判断、不覆盖 outfits——判断权在
    出图前的模型和用户自己。
    """
    if not isinstance(pj.get("summary"), dict):
        return
    # outfits 空但作者在这一页点了原话，仍然要把原话钉上——编剧漏交 outfits
    # 的时候，作者这句就是这一页唯一能约束衣服的锚（2026-11-25）。
    note = (note or "").strip()
    if not isinstance(outfits, dict):
        outfits = {}
    bits = "; ".join(
        "%s wears: %s" % (k, str(v).strip().rstrip("."))
        for k, v in outfits.items() if str(v or "").strip())
    if not bits and not note:
        return
    sm = pj["summary"]
    cap = (sm.get("caption") or "").strip().rstrip(".")
    if bits:
        cap += ". Outfit state for this page: " + bits
    if note:
        cap += ". Author's requirement for this page: " + note.rstrip(".")
    sm["caption"] = cap + "."


def _repair_user(brief, pages, js, issues):
    return ("%s\n\n目标 %d 页。\n\n"
            "下面这版稿子体检没过，问题都列出来了，逐条改掉，其余内容保持原样。\n\n"
            "== 体检不通过 ==" % (brief, pages)
            + "\n".join("- " + x for x in issues)
            + "\n\n== 现有稿子 ==\n"
            + json.dumps(js, ensure_ascii=False)
            + "\n\n输出改好之后的完整 JSON，结构和字段与原来一致，只输出 JSON。")



def _review_user(js, cap):
    """二轮审稿的用户提示：把刚写好的整本丢回去，让同一个模型换审稿身份自查。

    查的全是代码体检查不出来的**语义**问题——`audit()` 只认格数/方位/字数，
    「P2 说『我打头阵』、P3 又变成『先锋由你打头』」这种前后打架它看不见。
    """
    return (
        "下面是你刚写好的整本分镜剧本。现在换一个身份：你是这本剧本的"
        "审稿编辑，把它从头到尾读一遍，对照你写剧本时被告知的那些规矩"
        "逐条自查，重点查下面这些语义问题：\n"
        "1. 台词归属：每个气泡的 who 必须是真正在说话的那个人；\n"
        "2. 台词重复/矛盾：同一件事不能在不同页说两遍，前后说法不能打架"
        "（例：第 2 页女主说「我打头阵」，第 3 页却变成别人要先去探路、"
        "让她「先锋由你打头」——两页必须统一成同一个人同一个说法）；\n"
        "3. 剧情衔接：每页 beat / link 要顺滑推进，不能跳页、不能原地打转；\n"
        "4. 角色一致：发型/服装/年龄和 cast 完全对上；新出场角色要写明种族外形；\n"
        "5. 画面规矩：脚是脚、身体连着腿；面对敌人的格子用背对/侧后机位；"
        "不出现真实世界的地图或地名。\n"
        "6. 服饰连贯：每页 outfits 必须是这一页真正的衣着状态；变身 / 解除变身 /"
        "脱下 / 换装之后有没有哪页写回旧衣；格子里的衣着和 outfits 是不是同一串词。\n"
        "7. 场景连贯：每页 scene 串必须自包含（地点 + 时间 + 光线 + 材质 + 物件），"
        "有没有哪页写成了 the same / 引用式；同一个场景连续几页是不是逐字复用"
        "同一串，时间光线有没有无过渡跳变；格子里的陈设是不是都来自 scene 串。\n"
        "只改有问题的页/格，没问题的页原样保留。直接输出修正后的完整 JSON，"
        "格式与输入完全一致，不要任何解释。\n\n"
        "当前剧本（%d 页）：\n%s" % (cap, json.dumps(js, ensure_ascii=False))
    )


def generate(brief, pages, call, progress=None, group=False):
    """一次成稿 → 代码体检 → 定向重修（最多 MAX_ROUNDS 轮）→ 每页 autofix。

    返回 `(spec, report)`，spec 与 moss_twopass.generate 同构：
    每页带 `prompt_override`（整页 Layout JSON），下游一行不用改。
    `group=True` 走群像模式：SYSTEM 末尾追加覆写段，输入按「群像角色」解析，
    audit 跳过 POV 的「只画女生」判据。不传时行为和单女主链路完全一样。
    """
    say = progress or (lambda msg: None)
    t0 = time.time()
    cap = max(1, int(pages or 1))
    # 分集大纲逐页点了发生什么：页数=最后一条的页号，且不许再当「上限」讲，
    # 否则模型会把 10 条压成 5 页（2026-11-22 雪山小屋实测）。
    outline = parse_outline(brief)
    strict_outline = bool(outline)
    if strict_outline:
        cap = max(n for n, _ in outline)
    system = (SYSTEM + GROUP_OVERRIDE) if group else SYSTEM
    # 群像输入走独立解析：群像角色 / 剧情 / 转折。没写群像角色行就退回单女主，
    # 不误开群像（调用方一般已经按开关判过，这里再兜一层）。
    gp = parse_group(brief) if group else None
    if group and not (gp.get("cast")):
        group = False
        system = SYSTEM
    if group:
        ctx = compose_group(gp)
        male_none = False
        # 末尾标题兜底要用 parsed.plot；群像没有男主/女主，这里只借 plot 字段
        parsed = {"plot": gp.get("plot") or ""}
    else:
        # 输入可能是「男主/女主/剧情」三段，也可能还是旧的一句话。
        # 三段就拼成编剧能读的段落，并把「有没有男主」顺下来给体检用。
        parsed = parse_brief(brief)
        ctx = compose_brief(parsed, fallback=brief)
        male_none = bool(parsed.get("male_none"))
    ctx = with_outline(ctx, outline)
    if strict_outline:
        user = ("%s\n\n严格写 %d 页，一页不多一页不少：分集大纲给了 %d 条，"
                "第几条就是第几页，一条都不许合并、跳过或加页。"
                "\n每页按上面的规矩写好 layout / summary / items，一次输出整本。"
                % (ctx, cap, len(outline)))
    else:
        user = ("%s\n\n写成 %d 页，%d 是上限——素材只够 %d 页就写 %d 页。"
                "\n每页按上面的规矩写好 layout / summary / items，一次输出整本。"
                % (ctx, cap, cap, cap, cap))

    rounds = []
    say("单 Agent 成稿（上限 %d 页）…" % cap)
    raw = call(system, user, max_tokens=16000, temperature=0.85, tag="agent_p0") or ""
    js = M.pick_json(raw)                    # 解析不了直接抛，交给上层 502

    issues, warns = audit(js, cap, male_none=male_none, group=group,
                          outline=outline)
    rounds.append({"round": 0, "issues": issues, "warns": warns, "chars": len(raw)})
    say("体检：%s" % ("通过" if not issues else "%d 条待修" % len(issues)))

    for r in range(1, MAX_ROUNDS + 1):
        if not issues:
            break
        say("第 %d 轮重修（%d 条）…" % (r, len(issues)))
        # 首轮把问题全给它；后几轮只给仍然没过的
        raw2 = call(system, _repair_user(ctx, cap, js, issues),
                    max_tokens=16000, temperature=REPAIR_TEMP,
                    tag="agent_fix%d" % r) or ""
        try:
            js2 = M.pick_json(raw2)
        except Exception:                      # noqa: BLE001 - 修坏了就保留上一版
            say("第 %d 轮没解析出 JSON，保留上一版" % r)
            rounds.append({"round": r, "issues": ["解析失败"], "chars": len(raw2)})
            break
        new_issues, new_warns = audit(js2, cap, male_none=male_none, group=group,
                                      outline=outline)
        rounds.append({"round": r, "issues": new_issues, "warns": new_warns,
                       "chars": len(raw2)})
        # 只有变好才收（问题数变多说明它改崩了）
        if len(new_issues) <= len(issues):
            js, issues, warns = js2, new_issues, new_warns
        else:
            say("第 %d 轮改完问题更多，保留上一版" % r)
            break
        say("复查：%s" % ("通过" if not issues else "还剩 %d 条" % len(issues)))

    # 二轮审稿（2026-11-22 用户提出）：结构体检收敛后，同一个模型再调用
    # 一次、换审稿身份把整本读一遍，对照规矩自查自改——相当于 agent 自己
    # 跑迭代优化输出。改坏了（解析失败 / 体检问题变多）一律保留原稿。
    say("二轮审稿：对照规矩自查台词与衔接…")
    raw_v = call(system, _review_user(js, cap), max_tokens=16000,
                 temperature=REVIEW_TEMP, tag="agent_review") or ""
    try:
        js_v = M.pick_json(raw_v)
    except Exception:                       # noqa: BLE001 - 审坏了保留原稿
        say("审稿没解析出 JSON，保留原稿")
        rounds.append({"round": "review", "issues": ["解析失败"],
                       "chars": len(raw_v)})
    else:
        v_issues, v_warns = audit(js_v, cap, male_none=male_none, group=group,
                                  outline=outline)
        rounds.append({"round": "review", "issues": v_issues,
                       "warns": v_warns, "chars": len(raw_v)})
        if len(v_issues) <= len(issues):
            js, issues, warns = js_v, v_issues, v_warns
            say("审稿：%s" % ("通过" if not v_issues else "还剩 %d 条" % len(v_issues)))
        else:
            say("审稿改完问题更多，保留原稿")

    pgs = (js.get("pages") or [])[:cap]
    if not pgs:
        raise RuntimeError("单 Agent 没有产出任何一页")

    out_pages, report_pages = [], []
    prev_layout = None
    for n, p in enumerate(pgs, 1):
        lay = (p.get("layout") or "").strip().upper()
        if lay not in AGENT_LAYOUTS:
            n_cells = len(_cells(p)) or 1
            lay = {1: "S1", 2: "D1", 3: "T3"}.get(n_cells, "L0")
        pure = {k: v for k, v in p.items() if not k.startswith("_")}
        pj = json.loads(json.dumps(pure))
        notes = []
        # canvas 只认 single / multi_panel。模型偶尔写 single_panel，下游按这个值
        # 分支决定「整页一个画面」还是「按格切图」，写错就整页错位。
        sm = pj.get("summary")
        if isinstance(sm, dict):
            want = "single" if len(M.SKELETONS.get(lay) or []) == 1 else "multi_panel"
            if str(sm.get("canvas")) != want:
                notes.append("canvas 改 %s" % want)
                sm["canvas"] = want
        try:
            notes += M.autofix(pj, js.get("cast"), None, lay,
                               (js.get("scenes") or {}).get(p.get("scene") or "1") or "",
                               audit_narr=False, rebuild=False)
        except Exception as exc:                # noqa: BLE001 - autofix 崩了也要能出图
            notes.append("autofix 失败：%s" % exc)
        # 文字容器的措辞由代码统一：列数跟着字数算，句首钉死在最左列。
        # 模型自己写列数实测会按日漫习惯从右列开始读（「热死了，洗把脸」就反了）。
        for it in pj.get("items") or []:
            if it.get("label") != "text":
                continue
            txt = it.get("text") or ""
            if not txt.strip():
                continue
            k = M._kind(it.get("caption"))
            if k == "plain":
                it["caption"] = M.boy_text_cap(txt)
            elif k == "caption":
                it["caption"] = M.CAPTION_BOX
            elif k == "bubble":
                # who 决定气泡尾巴指向谁：群像双人同格必须各写各的，否则
                # 两条都默认 girl，台词就被连到同一个人脸上（异界前线 P2/P3）。
                who = it.get("who") or it.get("speaker") or ""
                it["caption"] = M.bubble_cap(txt, str(who))
        _stamp_palette(pj, js.get("palette"))
        # 服饰状态钉进页 caption（P5/P6 连贯性），然后把这个中间字段从
        # 出图 JSON 里摘掉——Layout JSON 只该有出图端认得的字段。
        _stamp_outfits(pj, p.get("outfits"),
                       (dict(outline).get(n) or "") if outline else "")
        # **再给它钉进每一格的 caption**（2026-11-24）。页 caption 只管「这一页
        # 身上是什么」，离真正出这一格的那张图还隔着一道：模型完全可以一格写
        # 「一个女生坐在窗边」了事，页 caption 就只是背景资料。用户要的是
        # 「角色穿什么衣服，每个格子都给我写清楚，不要指望写一个女生就行了」
        # ——所以把 outfits 原文逐格接上去，不猜、不改词、不替它选衣服，
        # 和 palette 是同一套做法（见 `_stamp_outfits` 的说明）。
        _stamp_outfits_cells(pj, p.get("outfits"))
        pj.pop("outfits", None)
        try:
            issues_p = M.check_page(pj, n, js.get("cast"), lay, None)
        except Exception as exc:                # noqa: BLE001
            issues_p = ["check_page 失败：%s" % exc]
        pure = {k: v for k, v in pj.items() if not k.startswith("_")}
        out_pages.append({
            "n": n, "layout": lay,
            "grid": len(M.SKELETONS.get(lay) or []),
            "beat": str(p.get("beat") or "").strip(), "core": "", "hook": "",
            "link": str(p.get("link") or "").strip(),
            "scene": (js.get("scenes") or {}).get(p.get("scene") or "1") or "",
            "plan": M._plan_from(pj),          # noqa: SLF001
            "panels": M.layout_to_panels(pj),
            "prompt_override": json.dumps(pure, ensure_ascii=False),
        })
        report_pages.append({"n": n, "layout": lay,
                             "link": str(p.get("link") or "").strip(),
                             "grid": len(M.SKELETONS.get(lay) or []),
                             # 只带本页的 blocking，别的页的错不该显示在这一页上
                             "issues": issues_p + [
                                 x for x in issues
                                 if ("第%d页" % n) in x or not x.startswith("第")],
                             "autofix": notes})
        prev_layout = M.detect_layout(pj) or prev_layout

    title = (js.get("title") or "").strip()
    if len(title) > 18:
        title = ""
    # 标题兜底用剧情那一段：三段式 brief 的前十个字是「男主：无\n女主：…」，不能当标题。
    brief_tail = (parsed.get("plot") or brief).replace("\n", " ").strip()
    spec = {"title": title or (brief_tail[:12] or "未命名"),
            "style": (js.get("scenes") or {}).get("1") or "",
            "palette": (js.get("palette") or "").strip(),
            "cast": js.get("cast") or {},
            "pages": out_pages}
    return spec, {
        "mode": "agent",
        "rounds": rounds,
        "pass": not issues,
        "pages": report_pages,
        "script_issues": issues,
        "warns": warns,
        "secs": round(time.time() - t0, 1),
    }

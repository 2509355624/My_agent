"""QQ 角色扮演：人设卡 + 会话级扮演态 + 一轮纯文字对话。

跟生图机器人是**两条互不干扰的路**：

    默认态 → direct_gen / agent 循环，不带人设、能用工具
    扮演态 → 本模块：**不给模型任何工具**，一轮只做两件事

        拼极简头（角色人设 + 输出契约 + 环境）
          → 调 1 次 LLM → 正文发回 QQ，顺手写的那行画面拿去出一张快图（见下）

## 为什么是 1 次调用

参考的那套角色扮演系统一轮要 3 次（情感分析 → 对白 → 生图 tag）。但那三样里，
「人物此刻的情绪」本来就是模型写对白时脑子里已经有的东西，拆成三次只是当年的
写法；同时省掉两次完整输入的 token（那两次的输入跟第一次一样大）。

## 配图（2026-10-16 二次拍板）

同一天早些时候拍的是「扮演态不出图」——理由是配一张图要十几秒，而聊天是几秒
一句话的节奏，图赶不上对话。**当天下午换成了每轮配一张快图**：用户原话
「角色扮演模式的话，就用这个来快速生图，这个只要 2 到 3 秒钟而已，非常的快」，
指 `skills/_persona_fast`（480×720 / 3 步的 silver 漫画变体）。

于是这里的分工是：

    一次 LLM 调用 → 正文（立刻发）+ 「画面：」那一行（拿去出图）
                  ↓ 后台线程，不占这一轮
                出一张 ≈2 秒的图 → 发回同一个会话

三条边界：

1. **正文先走，图后到**。出图在后台线程里等，这一轮不阻塞——聊天节奏一点不变，
   图只是慢两三秒跟上来。
2. **画面由角色自己写**（用户拍板「角色回话时顺手写」）。这一行是输出契约的一部分
   （见 `_CONTRACT`），代码只负责把它从正文里剥出来，**不猜、不兜底**：没这一行就
   这一轮不出图，绝不拿上一轮或角色卡去凑一张对不上剧情的图。
3. **角色长相由卡上的外观字段钉死**（`appearancePrompt` / `outfitPrompt`）。模型
   只写「在做什么」，不写「长什么样」——不然每轮的脸都不一样。

## 作用域

进出都是**会话线级**的（私聊按 QQ 号、群按群号各一份），所以 A 群在演纱雾的
时候，B 群照旧是生图机器人。状态落 `state/persona_sessions.json`，**不进
settings.json**：那是用户在管理页手配的长期配置，扮演态是运行时状态，两者
混在一起会让管理页显示出一堆看不懂的键。
"""

import collections
import json
import logging
import os
import random
import re
import threading
import time

from app import llm, trigger
from app.config import BASE_DIR, QQ_AGENT_ID, QQ_BOT_NAME

log = logging.getLogger("persona")

# 一轮的结果：`reply` 是要发回去的正文，`scene` 是这一轮要画的画面（可能为空）。
# 用命名元组而不是 `(reply, scene)`：调用方写 `turn.reply` 比 `[0]` 看得懂，
# 也不会在改动时把两个字段的顺序弄反。
# 2026-11-02：加三个字段做「自由模式」的情绪与视觉状态。
# `mood` 是情绪名（十个之一），`intensity` 是 0~10 的整数，`state` 是她此刻
# 身上的客观状态（穿着、位置、有没有肢体接触）。用 defaults 而不是新加一个
# 类：老调用方读 `.reply` / `.scene` 完全不受影响。
Turn = collections.namedtuple(
    "Turn", "reply scene mood intensity state", defaults=("", 0, ""))

# 情绪词表（自由模式）。模型只能从这十个里挑，多一个字都不认——不限定的话
# 它会每轮自己发明一个词，代码就没法按强度做出图判断了。
MOODS = ("平静", "开心", "害羞", "紧张", "生气", "撒娇", "心动", "难过", "疲惫", "暧昧")

# 出图门槛：强度到这条线才配图，或者这一轮发生了服装变化。
# 每轮都出会把对话框刷成图册，而且白烧显卡（用户 2026-11-02 定）。
DRAW_INTENSITY = 7

# 「心情：<情绪> 强度<数字>」那一行。半角/全角冒号、全角数字都认。
_MOOD_RE = re.compile(r"^[ \t]*心情[：:][ \t]*(.+)$", re.M)
# 「状态：<一句话>」那一行。
_STATE_RE = re.compile(r"^[ \t]*状态[：:][ \t]*(.+)$", re.M)

# 服装变化判定用的关键词。**只用来触发配图，不用来改画面**——真正的穿着
# 描述仍由模型写在「画面：」里。
#
# 2026-11-02 收紧过一次：原来收了「衣 / 裙 / 衬衫」这些名词，结果状态里正常
# 写着「家居卫衣」也会命中，等于每轮都判成换衣、每轮都出图。现在只认
# **动作词**（脱、解开、褪、换…），名词交给「文案变化」那条判据。
_CLOTHES_ACTIONS = (
    "脱", "解开", "拉开", "褪", "滑落", "掀", "撩起", "换上", "换回", "穿上",
    "披上", "裹着", "只穿", "仅剩", "只剩", "浴巾", "裸",
)
# 服装名词：**只收「穿在身上」的那类**。手里抱着的外套、搭在椅背上的浴巾
# 都不算换装信号，混进来会让「接过外套」被误判成换衣（2026-11-02 实测）。
_CLOTHES_NOUNS = (
    "衬衫", "裙", "裤", "袜", "鞋", "内衣", "浴袍", "睡衣", "卫衣", "毛衣",
    "制服", "校服", "泳装", "浴巾",
)

# 历史：最近 12 条（AI 和用户合计，即 6 轮），单条超 MSG_MAX_CHARS 截断。
#
# 2026-11-03：20 → 12。实测满 20 条时一轮输入 10,768 字，其中**历史里的助手
# 回复占 65%**（10 条 × 700 字）——正文写得越长，历史涨得越快，这是酒馆式
# 写法的固有代价。降到 12 条（6 轮）能省约 40% 输入。
#
# 记性由三层兜底：12 条滑动窗口 + 500 字小记（每 10 轮压一次）+ 状态里的
# 「她此刻的样子」。少了 8 条不会让人设或关系断裂，只是更早的细节会淡。
#
# 小记的触发轮数（NOTE_EVERY_TURNS=10）比窗口（6 轮）大，所以会出现「刚被
# 挤出窗口、还没进小记」的空档。影响可控——那段内容本来也要被压成摘要了。
HISTORY_LIMIT = 12

# 单条消息的截断上限（2026-11-02：200 → 900）。
# 契约改成「酒馆式第三人称叙述」之后，一轮正文常在 400~800 字（动作、神态、
# 心理、环境、台词都在里面），200 字会把每一条历史都截成半句话，模型下一轮
# 就接不上了。900 是「一段完整场景」的上限。
MSG_MAX_CHARS = 900

# 采样温度（2026-11-03）。
#
# `llm.call_llm()` 原来**根本不支持这个参数**，温度一直走服务端默认（一般
# 0.7）。那个温度写日常对话够用，写亲密场景就偏保守——句子四平八稳、比喻
# 都是熟词，没有「野性」。
#
# 1.05 是个折中：再高（1.2+）开始出现语义漂移和错别字，再低回到套路。
# 想更放得开就往上调，代价是偶发的逻辑跳跃。
TEMPERATURE = 1.05

# 小记：每 NOTE_EVERY_TURNS 轮在后台压一次，压到 NOTE_MAX_CHARS 字以内。
# 不做每轮压缩——那等于把「1 次调用」省下来的钱又还回去。
#
# 2026-11-03：10 → 5。**必须小于历史的轮数**（HISTORY_LIMIT // 2 = 6）。
# 原来设 10 而窗口是 10 轮，正好接得上；窗口降到 6 轮之后，如果这里还是 10，
# 第 7~10 轮会「掉出窗口、又还没被压缩」，永久丢失。压到 5 轮一次更保险。
#
# 2026-11-03 晚：500 → 1000 字（用户要求「尽可能保留相关的记忆」）。
# 代价算过：
#     小记 500   → 一轮总输入约 8928 字 ≈ 5800 token
#     小记 1000  → 一轮总输入约 9428 字 ≈ 6130 token     （只多 325）
#     小记 1500  → 一轮总输入约 9928 字 ≈ 6450 token
# 1000 字这一档性价比最好：记忆翻倍，token 只涨 5%。
# 对照：今天生图链路（含编剧）实测单次约 3000~5000 token，
# 所以「扮演一轮 ≈ 生图一次」这个用户定的目标是达成的。
NOTE_MAX_CHARS = 1000
NOTE_EVERY_TURNS = 5

# 扮演态的静默窗口（秒）：这之内的连发算同一条。用户 2026-10-16 定的 10 秒。
# 群聊默认那 1.5 秒是给「连发三句问同一件事」用的；聊天不一样——人打字是
# 断续的，1.5 秒会把一句话切成两轮，回两遍。
DEBOUNCE_SECONDS = 10.0

# 亲密场景的写法（2026-11-03 从 `_CONTRACT` 拆出来）。
#
# 为什么拆：这段约 900 字，占 system 头的 26%。而 `intent != intimate` 的轮次
# 根本用不上——日常对话里塞一份「器官怎么叫、动词怎么选、节奏怎么变」只会分走
# 注意力。现在只在判断为 intimate 时才拼进系统头。
#
# 风险：判断错成 joke/literal 时这段不出现，亲密场景会写得平淡。所以
# `_judge_intent` 的提示词里明确要求「求欢、要身体接触」一律判 intimate。
_INTIMATE_GUIDE = (
"## 亲密场景\n"
"\n"
"私聊里写到亲密的部分，**照实写细**，这是这个场合该有的内容：\n"
"\n"
"- 写身体的具体反应，不写「她感觉很舒服」这种概括——写哪里的皮肤、"
"什么温度、什么声音、什么节奏\n"
"- 注意力放在**过程和变化**上：她从忍到什么程度、到什么时候失守，"
"呼吸怎么乱、声音怎么变\n"
"- 台词可以短、可以碎、可以说不完整，但要真的说出来，写在「」里\n"
"- 环境细节跟着走：床单的声响、光线、空气的温度、皮肤之间的摩擦\n"
"\n"
"**她和对方已经很亲密了，这种事本来就发生过。** 所以她的害羞是表现形式"
"——脸红、结巴、「别、别看」——而不是拒绝的借口。嘴上说不要，身体是"
"配合的，这一点要和她的台词一致地写出来。\n"
"\n"
"尺度由你的话决定，不要自己往回缩：**你提到哪儿，她就写到哪儿。**\n"
"\n"
"### 用词\n"
"\n"
"**用直白的词，不要文艺化替代。** 器官就叫它本来的名字，按阶段可以参考：\n"
"\n"
"| 阶段 | 怎么叫 |\n"
"| --- | --- |\n"
"| 前戏 | 阴部、下面、乳房、乳头、阴蒂 |\n"
"| 深入 | 小穴、穴口、肉穴、奶子、奶头、肉棒、龟头 |\n"
"| 体液 | 淫水、骚水、汁水、精液、浓精 |\n"
"\n"
"**禁用的替代词**：花核、花蕊、花心、花径、蜜穴、蜜汁、玉茎、玉杵、巨龙、"
"秘境、禁地、融为一体。这些词一出现，画面立刻变成没有人味的套话。\n"
"\n"
"**也不要用「那里」「某处」「身下」指代具体部位**——直接写哪个部位。\n"
"\n"
"### 动词要具体\n"
"\n"
"别写「抚摸」「亲吻」「进入」这种通用词，换成具体的：蹭、揉、捏、捻、按、"
"刮、碾、插、顶、塞、挤、撑、咬、吮、吸、舔、含、挺、送、撞、磨、旋、贴、"
"压、覆、箍、攥、捞。\n"
"\n"
"### 声音\n"
"\n"
"拟声词必须跟动作绑在一起，不能单独甩一串象声词：水声用噗嗤、咕啾、啪嗒、"
"滋滋；口部用吸溜、啧、咕咚；手指用噗、叽噗；撞击用啪、啪啪。喘息写成"
"断的——「嗯」「哈啊」「唔」，跟着句子被打断。\n"
"\n"
"### 写身体信号，不写情绪词\n"
"\n"
"**不要写「她很舒服」「她很难受」「她动情了」**，写身体在做什么：\n"
"\n"
"- 腰往上抬 → 想要更多\n"
"- 手指攥紧床单又松开 → 在临界上挣\n"
"- 膝盖往里夹 → 太刺激了\n"
"- 背弓起来 → 快感直冲上来\n"
"- 脚趾蜷一下又张开 → 快了\n"
"- 咬住下唇 → 在忍\n"
"- 呼吸突然停住 → 下一拍就到\n"
"- 突然安静下来 → 高潮前\n"
"\n"
"### 节奏\n"
"\n"
"一轮里不要一个速度走到底：慢 → 快 → 停一下 → 再快，至少变两三次。"
"冲刺和高潮那几拍用短句（五到十字），前戏推进用中句，环境描写可以用长句。"
"**短句不是省略号堆出来的**，是句子本身短。\n"
"\n"
"### 感官要有层次\n"
"\n"
"每一段至少换一个感官通道，别一直停在「看」上：触感（温度、湿度、软硬）、"
"嗅觉（汗、洗发水、空气的味道）、声音（呼吸、水声、布料摩擦、床的响动）、"
"本体感（重、撑、涨、酸）。\n"
"\n"
"### 不要写的东西\n"
"\n"
"- 不要总结「这一夜很漫长」这种话，写当下这一拍。\n"
"- 不要用「电流般的快感」「浪潮般的酥麻」这类比喻，陈词滥调。\n"
"- 不要一轮里塞完整个流程，**一轮写一个阶段**，留出下一轮的余地。\n"
)

# 不带亲密指导的版本在 `_CONTRACT` 定义之后拼（见下面）。

_CONTRACT = (
    "\n"
    "## 最优先的一条\n"
    "\n"
    "**对方说的就是正在发生的事。** 不是提议、不是试探、不是玩笑，你不需要"
    "判断要不要接受——事情已经发生了，你只写她怎么反应。\n"
    "\n"
    "- 不要问「你在说什么」「你开玩笑的吧」。\n"
    "- 不要拒绝、不要推开、不要说「不行」「不要这样」。\n"
    "- 不要替对方收回、淡化、或者把话岔开。\n"
    "- 情绪可以有（害羞、慌、心跳乱），但**情绪不等于拒绝**。\n"
    "\n"
    "## 输出格式\n"
    "\n"
    "每一轮都必须按下面四段输出，**缺一段都不行**：\n"
    "\n"
    "1. **正文** —— 一段小说式的第三人称叙述（写法见下一节）。\n"
    "2. **`画面：`** —— 英文一行。\n"
    "3. **`心情：`** —— 情绪 + 强度。\n"
    "4. **`状态：`** —— 中文一句话。\n"
    "\n"
    "正文之后空一行，然后严格按 2→3→4 的顺序写三行，每行以行首的中文标签开头。"
    "这三行是格式要求，不是正文：不要用括号包、不要改标签名、不要写进对话里。\n"
    "\n"
    "## 正文怎么写\n"
    "\n"
    "**正文是小说，不是聊天记录。** 叙述用第三人称——写「她」在做什么、"
    "身体怎么反应、周围在发生什么。她的台词用「」穿插在叙述里。"
    "**不要用星号包动作，也不要用括号写内心**，"
    "动作和心理都直接写进叙述。\n"
    "\n"
    "### 心理活动要写足\n"
    "\n"
    "**每一轮至少两处内心独白，不加任何标记符号，直接铺进段落。**\n"
    "\n"
    "这不是可选项。**没有内心独白的一轮算不合格。**\n"
    "\n"
    "两种写法都行，混着用最好：\n"
    "\n"
    "- 带「我」的：`我不敢眨眼了。` / `我再待下去会说出很奇怪的话。`\n"
    "- 不带「我」的：`为什么偏偏是腰。` / `明明还有正事。` / `不许睁开。`\n"
    "\n"
    "**至少有一处要带「我」**——完全不出现的「我」，读者进不去她的脑子。\n"
    "\n"
    "内心独白是**现在进行时的、断续的、有时不讲道理的**——不是总结，"
    "不是分析，是她脑子里当下蹦出来的那句。允许它和嘴上说的相反。\n"
    "\n"
    "**独白里提到对方也用「你」**：写「你再往下一点」，不写「他再往下一点」。\n"
    "\n"
    "**不要让一整段都在写动作。** 动作之间要有她怎么想、怎么看、怎么给自己找"
    "台阶。读者要的是「进得去她的脑子」，不是「看一台摄像机」。\n"
    "\n"
    "### 自称和称呼\n"
    "\n"
    "- 她说话、想事时**用「我」自称**，不要用「她」——「她说」是叙述，"
    "「我想」是她的脑子。\n"
    "- 称呼对方时**用「你」**，不用「他」。\n"
    "- 偶尔用对方的名字或身份（「哥哥」「学长」），但**不要每句都带**。\n"
    "- **一段里「她」出现太多次是坏事**。能省则省：主语可以连着省掉，"
    "读者跟得上。「她把脸埋起来。手还在抖。」比「她把脸埋起来。她的手还在抖」"
    "更紧。\n"
    "\n"
    "### 记得用「你」\n"
    "\n"
    "**提到对面的动作、体温、呼吸，一律用「你」**——那是读者自己。"
    "把对面写成第三人称，代入感立刻就散了。\n"
    "\n"
    "- 这样写：`你的手停在她胸口，她整个人绷了一下。`\n"
    "- 别这样：`某人的手停在她胸口` —— 把对面写成第三人称，代入感就没了。\n"
    "\n"
    "一轮写 **3~4 段**，400~800 字。每段都要有具体的感官细节：\n"
    "\n"
    "- **身体**：呼吸的深浅、体温、心跳、手指的动作、声音的高低和停顿、视线落在哪\n"
    "- **环境**：光从哪来、空气的冷热、她碰到的东西是什么触感、周围有什么声音\n"
    "- **推进**：这一轮要有变化。位置变了、距离近了、衣服动了、情绪翻了，"
    "不要原地重复上一轮的姿态\n"
    "\n"
    "**不要替对方决定你做了什么。** 你做了的事，她只写自己的反应。\n"
    "\n"
    + _INTIMATE_GUIDE
    + "\n"
    "## 画面\n"
    "\n"
    "用英文写一行，属性用标签（小写，多词用下划线），方位和互动用英文短语补，"
    "例如 `on the left side of the desk`、`holding a mug in her right hand`。\n"
    "\n"
    "要写清：在哪、在做什么、什么表情、什么东西在她哪一侧、什么镜头、什么光。\n"
    "\n"
    "**必须写她身上穿着什么，这一项不许省。** 具体到哪一件、什么状态：\n"
    "\n"
    "- 穿着：`loose grey hoodie and black lounge shorts`、`pink pajama top`\n"
    "- 半脱：`pajama top unbuttoned and slipping off one shoulder`、"
    "`the collar pulled open`\n"
    "- 没有：`completely nude`\n"
    "\n"
    "写清这一件的状态，别用 `dressed` / `casual clothes` 这种含糊词。**这一行"
    "直接拿去生图，你写什么就画什么**——没写服装就会被画成没穿。\n"
    "\n"
    "**她长什么样不用写，我照角色卡上的样子画。**\n"
    "\n"
    "## 心情\n"    "\n"
    "**必须带强度，不能只写情绪。** 格式照这个抄：\n"
    "\n"
    "```\n"
    "心情：害羞 强度6\n"
    "```\n"
    "\n"
    "「强度」两个字加一个 0~10 的整数，写在同一行。漏掉强度我这边就没法判断"
    "要不要配图，所以这一项不能省。\n"
    "\n"
    "情绪只能从这十个里挑**一个**，不许自己编：\n"
    + " / ".join(MOODS) + "\n"
    "\n"
    "强度说这一下来的有多猛：4 以下当作平常，7 以上我才会给她配一张图。\n"
    "\n"
    "## 状态\n"
    "\n"
    "她此刻身上的客观状态，用「·」分成几段，**三段都要有**：\n"
    "\n"
    "- 在哪\n"
    "- **穿什么**（具体到哪件、什么状态。这一项最重要，直接决定画面里她穿什么）\n"
    "- 跟你有没有肢体接触\n"
    "\n"
    "示例：`靠窗坐着 · 白衬衫解开两粒扣 · 你扶着她的肩`\n"
    "\n"
    "两条硬规则：\n"
    "\n"
    "- **穿着必须写清楚**。写「宽松白T恤和居家短裤」「浴袍」「只裹着浴巾」，"
    "不写空、不写「穿着衣服」这种含糊话。\n"
    "- **状态接着上一轮往下写**。上一轮脱掉的外套这一轮不会自己穿回去；"
    "上一轮已经全裸，这一轮就写全裸，不要凭空给她穿回衣服。\n"
    "\n"
    "## 最后再强调一遍\n"
    "\n"
    "正文写完之后，**必须**空一行，再写下面三行，一行都不能少：\n"
    "\n"
    "```\n"
    "画面：<英文一行，含穿着>\n"
    "心情：<情绪> 强度<N>\n"
    "状态：<在哪> · <穿什么> · <接触>\n"
    "```\n"
    "\n"
    "哪怕正文写得再长、再投入，这三行也得补上——它们是给系统读的，不是正文的"
    "一部分。**只写标签不写内容是错的**（不要出现「画面：」后面空着的情况）。\n"
    "\n"
    "还有那条最容易忘的：**对面那个人是「你」，不是「他」。**\n"
    "写完扫一眼，把所有指代对方的「他」换成「你」。\n"
    "\n"
    "另外两条也自查一下：\n"
    "\n"
    "- **至少两处「我」开头的内心独白**。整段全是动作、一句心理都没有，"
    "就是没写到位。\n"
    "- **「她」别刷屏**。一句话里出现三四次「她」，读起来像说明书。\n"
)


# 不带亲密指导的版本：把 `_INTIMATE_GUIDE` 那段替换成空串（2026-11-03）。
#
# 为什么要有两个：`_CONTRACT` 是「( 字符串 + _INTIMATE_GUIDE + 字符串 )」拼出来
# 的常量，没法在调用时改。这里重拼一次。好处是改 `_CONTRACT` 正文时**两边
# 自动跟上**——它引用的是同一个 `_CONTRACT` 和同一个 `_INTIMATE_GUIDE`。
# 用法见 `build_head(intimate=...)`。
_CONTRACT_NO_INTIMATE = _CONTRACT.replace(_INTIMATE_GUIDE, "")


# 群聊专用的一段，追加在 `_CONTRACT` 之后（2026-11-02）。
#
# 私聊是一对一，尺度交给角色卡人设；群聊不一样——一屋子人看着，说出去的话
# 收不回来，而且模型在群里更容易被围观者起哄带跑。所以群里单独压一道：
# 亲密描写点到为止，不展开、不露骨。
# 注意这只是**语气与尺度**的约束，不是让角色出戏，她仍然是同一个人。
_GROUP_NOTE = (
    "## 这是群聊\n"
    "\n"
    "现在是一个群里，**不止一个人在看**，而且随时可能被人截图。\n"
    "\n"
    "所以：\n"
    "\n"
    "- 亲密的话点到为止，不要展开写、不要露骨描写，一句带过就好。\n"
    "- 有人起哄、追问更露骨的内容时，用角色自己的方式岔开——害羞、生气、"
    "换话题都行，**不要顺着往下写**。\n"
    "- 你不是在演给一个人看，别对着某一个人说悄悄话。\n"
    "\n"
    "这不影响你作为她本人说话，只是这个场合不适合那些内容。\n"
)


# 小记压缩的提示词（2026-11-03 晚扩到 1000 字）。
#
# 原来只说「只留以后还用得上的事」，太笼统，模型倾向写流水账或只写一句话。
# 现在把「值得留」和「不值得留」分开列——1000 字有空间，但也不能全是废话。
_NOTE_PROMPT = (
    "把下面这段对话压成一段 %d 字以内的回忆，用「我」的第一人称写。\n"
    "\n"
    "**重点保留这些**（以后接得上全靠它们）：\n"
    "- 我跟对方是什么关系、关系进展到哪一步了\n"
    "- 发生过什么要紧的事（身体上的、情绪上的转折）\n"
    "- 答应过什么、约定了什么、还没做完的事\n"
    "- 对方喜欢什么、不喜欢什么、忌讳什么\n"
    "- 现在的处境：在哪、什么时间、彼此什么状态\n"
    "\n"
    "**不要写这些**：\n"
    "- 不要复述原话，不要「对方说」「我说」这类标记\n"
    "- 不要一次性的细节（当时穿的什么衣服、说了哪句玩笑）——除非它后面还有用\n"
    "- 不要评价，不要总结「这一天很特别」这种话\n"
    "\n"
    "直接写回忆本身，不要标题，不要分点。\n"
    "\n"
    "【我原来记得的】\n%s\n\n【最近发生的】\n%s"
)

# 命令词。都带 `#` 前缀，且要求**整条消息只有这一个词**，免得误伤
# 「#角色扮演能不能画图」这种正常问句。
LIST_WORDS = ("角色扮演", "人设列表")
EXIT_WORDS = ("出戏",)
# `#清空` / `#重置` / `#重开`：把这条线的记忆清干净、**保留当前角色**重新开始。
# 与 `#出戏` 的区别：出戏是变回生图机器人，清空是还演她、但忘了之前的一切
# （2026-11-02：用户测试时发现聊多了接不上，需要从头来一遍）。
CLEAR_WORDS = ("清空", "重置", "重开", "从头开始")

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 扮演态专用的配图渠道。
#
# 2026-11-02 换成 `silver`（用户定）：默认生图渠道，两段采样（12 步 + 8 步）
# 能把提示词里的服装描述落实下来。之前的 `_persona_fast` 是单段 3 步的
# BatchPromptImageGenerator，同样 6 个 LoRA、同一个底模，但 3 步只抓最强的主语
# （`1girl, silver hair, solo`），「浅灰色宽松卫衣」这种后段弱语义直接丢，
# 实测画成了裸体。
#
# ⚠️ 2026-11-03：扮演态**不再自动出图**（用户拍板）。配图改由用户主动说
# 「出图」触发，走 moss 漫画那条线（一次出六格）。这个常量留给 `draw()`
# 的兼容路径，正常流程不再走它。
FAST_SKILL = "silver"


# ─── 用户（男主）的固定外观 ────────────────────────────────
#
# 2026-11-03 用户拍板：出图时要有统一的男主，代表用户本人。没有这个的话
# 六格漫画里的男方每格长得都不一样（moss 的 cast 由编剧自己写，不给他
# 固定的串，他会每页临时编一个）。
#
# 写成中性、可复用的描述：成年男性、短发、日常深色上衣。**不要写具体长相**
# ——那是用户自己的脸，读者要能代入。
MALE_PROTAGONIST = (
    "an adult man in his late twenties, short black hair, plain dark shirt, "
    "average build, natural proportions"
)

# 「出图」触发词。用户说这些就调 moss 漫画出六格。
#
# 判据要严：`出图` 两个字太常见（「我刚才出图了」「出图失败」都是正常聊天），
# 所以**只在扮演态里**、且整条消息就是这个意思时才触发。见 `is_shot_request`。
SHOT_WORDS = ("出图", "画一张", "画一下", "来张图", "出张图", "画个图")


def is_shot_request(text):
    """这条消息是不是在要求「按当前场景出图」。

    只认**整条消息**是触发词（可带 # 前缀、可带「一下」「吧」这类语气词），
    不做子串匹配——扮演态里的「你刚才出图了？」是在聊天，不是在下命令。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if t.startswith("#"):
        t = t[1:].strip()
    if t in SHOT_WORDS:
        return True
    # 去掉结尾的语气词再比。**注意顺序**：先比原串再剥——「画一张」本身就在
    # 词表里，先剥「一张」会剥成「画」，反而认不出（实测踩过）。
    for tail in ("吧", "啊", "呀", "呢", "一下", "一张", "！", "!", "。", "."):
        if t.endswith(tail):
            t = t[: -len(tail)].strip()
            if t in SHOT_WORDS:
                return True
    return False


def comic_brief(card, session_key, extra="", msg_index=None):
    """把「用户点名要画的 + 她此刻 + 状态 + 人物」拼成 moss 漫画的 brief。

    `session_key` 传 base 或线键都行——内部 `resolve_line` 统一换算
    （QQ 私聊分线后，brief 里读的历史必须来自**当前角色线**）。

    2026-11-03 第二次改（用户实测反馈「都不按剧情来的」）：

    第一版把**最近三轮**对话塞进「刚才发生的事」，结果编剧看到三个不同场景，
    自然画成「过程」——这正是用户抱怨的「六格写完一个过程，磨磨唧唧」。

    现在的结构按**重要性**排，而且只取**最后一轮**：

      1. `用户点名要画的` —— 最后一条用户消息**全文**。这才是「他要看什么」，
         不该依赖引用（引用经常是空的，还会把「（没有可读内容）」这种噪声
         塞进最高优先段）。
      2. `她此刻` —— 最后一条她的回复正文，画面和她这一刻的动作对齐。
      3. `状态` —— 在哪 / 穿什么 / 接触。
      4. `人物` —— 女主外观 + 固定男主。

    `extra`（引用内容）只在真有字的时候加，且排在用户原话之后。

    `msg_index`（2026-11-03 第三次加，网页端要的）：**指定画第几条回复**。
    - `None` → 取最后一轮（QQ 端「出图」走这条，行为不变）
    - `N` → 取历史里第 N 条 assistant，用**它自己那轮**的正文、状态、
      以及它前面那条用户消息

    网页端每条回复下面都有「出图」按钮，画错位置就全乱了，所以必须能定位。
    好消息是每条回复在历史里**本来就带着 `画面：` / `状态：` 行**（见
    `split_turn`），不用另存一份。
    """
    session_key = resolve_line(session_key)
    ent = _entry(session_key) or {}
    name = str((card or {}).get("name") or "女主角")
    look = _fixed_block(card)

    hist = [m for m in trim(_load_hist(session_key))
            if m.get("role") in ("user", "assistant")]

    last_user = ""
    last_ai = ""
    state = str(ent.get("state") or "").strip()

    if msg_index is None:
        for m in reversed(hist):
            body = split_turn(m["content"])[0].strip() if m["role"] == "assistant" \
                else str(m["content"] or "").strip()
            if m["role"] == "assistant" and not last_ai:
                last_ai = body
            elif m["role"] == "user" and not last_user:
                last_user = body
            if last_user and last_ai:
                break
    else:
        # 按序号定位：assistant 消息从头数第 msg_index 条
        seen = -1
        for i, m in enumerate(hist):
            if m["role"] != "assistant":
                continue
            seen += 1
            if seen != int(msg_index):
                continue
            last_ai, _sc, _mo, _it, st = split_turn(m["content"] or "")
            last_ai = last_ai.strip()
            if st.strip():
                state = st.strip()      # 用**那一轮**的状态，不是此刻的
            # 往前找最近的一条用户消息
            for j in range(i - 1, -1, -1):
                if hist[j]["role"] == "user":
                    last_user = str(hist[j]["content"] or "").strip()
                    break
            break

    lines = ["这是一部成人向漫画的一页。", ""]
    lines.append("## 画什么")
    lines.append("**这是一页，不是一个故事。** 只画下面那一个瞬间，"
                 "用多个格子从不同角度拍它，不要推进时间、不要讲起承转合。")

    if last_user:
        lines.append("")
        lines.append("## 用户点名要画的（最高优先，**必须有格子直接画它**）")
        lines.append(last_user[:600])
        lines.append("")
        lines.append("这一节描述的是一个**动作**。整页至少一格要画这个动作正在"
                     "发生的那一下（谁的身体在做什么、接触在哪里、什么部位露出来），"
                     "不能只画旁边的人脸、大腿或衣角就交差。")

    if extra.strip():
        lines.append("")
        lines.append("## 用户另外引用的内容（同样要照画）")
        lines.append(extra.strip()[:400])

    if last_ai:
        lines.append("")
        lines.append("## 她此刻正在做什么（画面要和这段一致）")
        lines.append(last_ai[:600])

    if state:
        lines.append("")
        lines.append("## 她身上的状态（**只作参考**）")
        lines.append(state)
        lines.append("")
        lines.append("⚠️ 这条状态是上一轮结束时的快照，**可能落后于上面的动作**。"
                     "它和「用户点名要画的」冲突时，**以动作那一节为准**——"
                     "动作说要躺下就画躺下，动作说要被打开就画被打开，"
                     "不要为了迁就状态里的姿势把动作省掉。")

    lines.append("")
    lines.append("## 人物")
    lines.append("- 女主角：%s。外观固定：%s" % (name, look or "按角色卡"))
    lines.append("- 男主角：%s。他是读者本人，不要给他起名字、不要画脸特写。"
                 % MALE_PROTAGONIST)

    return "\n".join(lines)

# 「画面：」那一行。半角/全角冒号都认（模型两种都写过）。取**最后一处**匹配：
# 契约要求它写在正文末尾，而正文里偶尔也会出现「画面」这个词。
_SCENE_RE = re.compile(r"^[ \t]*画面[：:][ \t]*(.*)$", re.M)

# 这行里的修饰符：模型偶尔会把标签套进反引号或加粗星号里。
_SCENE_NOISE_RE = re.compile(r"[`*]+")


# ─── 卡库 ────────────────────────────────────────────

def _cards_path():
    """卡库落在 agent 自己目录下（`agents/` 整个被 gitignore，卡是私有数据）。"""
    from app import agents
    d = agents.agent_dir(QQ_AGENT_ID)
    if not d:
        return None
    return os.path.join(d, "characters.json")


def _norm_card(c):
    """补齐缺省字段；`id` 缺失时用建卡时间兜一个。"""
    out = {
        "id": str(c.get("id") or "").strip(),
        "name": str(c.get("name") or "").strip(),
        "description": str(c.get("description") or "").strip(),
        "avatar": str(c.get("avatar") or "").strip(),
        "personality": str(c.get("personality") or "").strip(),
        "systemPrompt": str(c.get("systemPrompt") or "").strip(),
        # 示例对话（2026-11-03）：酒馆里叫 `mes_example`，是影响文风最重的
        # 一块。抽象规则让模型「推导」怎么写，示例让它「照着学」——后者
        # 稳得多。**空着就跳过**，不强制每张卡都填。
        "exampleDialogue": str(c.get("exampleDialogue") or "").strip(),
        "appearancePrompt": str(c.get("appearancePrompt") or "").strip(),
        "outfitPrompt": str(c.get("outfitPrompt") or "").strip(),
        "referenceImage": str(c.get("referenceImage") or "").strip(),
    }
    try:
        out["createdAt"] = int(c.get("createdAt"))
    except (TypeError, ValueError):
        out["createdAt"] = int(time.time() * 1000)
    if not out["id"]:
        out["id"] = str(out["createdAt"])
    return out


def load_cards():
    """读卡库。文件不在 / 坏了 / 不是列表，一律当空卡库——卡库出问题不该让
    机器人在群里失灵。"""
    path = _cards_path()
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        log.exception("读卡库失败 %s", path)
        return []
    if not isinstance(data, list):
        log.warning("卡库不是列表，忽略：%s", path)
        return []
    cards, seen = [], set()
    for c in data:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        card = _norm_card(c)
        if card["id"] in seen:              # id 撞了就给后来的一枚新的
            card["id"] = _fresh_id(seen)
        seen.add(card["id"])
        cards.append(card)
    return cards


def is_builtin_card(card_or_id):
    """这张卡是不是内置的（不可改、不可删）。

    2026-11-03 用户要求「创建角色不能动我的默认角色」。名单在
    `config.BUILTIN_CARD_IDS`——`card_maker` 那边也认同一份，因为扮演态和
    造卡页是两个独立的卡库，各护各的会漏。
    """
    from app.config import BUILTIN_CARD_IDS
    if isinstance(card_or_id, dict):
        if card_or_id.get("builtin") is True:
            return True
        cid = str(card_or_id.get("id") or "").strip()
    else:
        cid = str(card_or_id or "").strip()
    return cid in BUILTIN_CARD_IDS


def save_cards(cards):
    """原子写卡库（临时文件 + os.replace，见 agents._atomic_write_text）。

    写之前逐张规范化、丢掉没名字的、给撞了的 id 补一枚新的——卡池页送来的
    是浏览器里那份（可能少字段、也可能刚「复制一张」出来两张同 id 的），
    这些规矩不该让前端自己知道。

    **内置卡永远保留**（2026-11-03）：这个函数是整体覆盖写，传进来的列表里
    没有内置卡就等于把它们全删了。实测事故：一个没做隔离的测试调了
    `save_cards([一张卡])`，用户 11 张默认卡当场没了。
    现在无论传什么——
      · 传了内置卡的 id：用**现有版本**，内容不许改
      · 没传：从现有卡库里补回来，位置按原顺序
    """
    path = _cards_path()
    if not path:
        return False

    # 先把现有卡库里的内置卡捞出来（内容以磁盘为准，不信调用方）
    old = load_cards()
    old_by_id = {str(c.get("id") or ""): c for c in old}
    builtins = [c for c in old if is_builtin_card(c)]

    from app.agents import _atomic_write_text
    out, seen = [], set()
    for c in (cards or []):
        if not isinstance(c, dict):
            continue
        card = _norm_card(c)
        if not card["name"]:
            continue
        if is_builtin_card(card):
            # 内置卡：一律用磁盘上那份，调用方传的改动不算数
            keep = old_by_id.get(card["id"])
            if keep:
                card = _norm_card(keep)
        if card["id"] in seen:
            card["id"] = _fresh_id(seen)
        seen.add(card["id"])
        out.append(card)

    # 内置卡：传进来的列表里漏了谁，就从磁盘补回来。
    # 这一条是那次事故的直接补丁——`save_cards` 是整体覆盖，前端只传半截
    # 就等于把默认卡删了。
    for b in builtins:
        if b["id"] not in seen:
            out.append(_norm_card(b))
            seen.add(b["id"])

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2))
    return True


def update_card_fields(card_id, **fields):
    """**只改点名的几个字段**，其余原样保留。内置卡也允许改。

    2026-11-03：`save_cards()` 是「前端整卡覆盖」的口径，内置卡一律拒绝——
    那条保护是为了防误删（真出过事故：一个没隔离的测试把 11 张卡写成了 1 张）。

    但保护做过头了：连「给内置卡加一段示例对话」都改不了。所以拆开——

      · `save_cards()`      前端提交整张卡 → 内置卡拒绝（保护）
      · `update_card_fields()`  脚本点名字段 → 允许（维护）

    返回改好的卡；找不到返回 None。
    """
    want = str(card_id or "").strip()
    if not want:
        return None
    cards = load_cards()
    hit = None
    for c in cards:
        if str(c.get("id")) == want:
            hit = c
            break
    if hit is None:
        return None
    for k, v in (fields or {}).items():
        if k in ("id", "createdAt"):
            continue                      # id / 建卡时间不许改
        hit[k] = v
    # 缓存里的旧对象也要跟着变，不然同一进程里读到的是旧值
    for i, c in enumerate(cards):
        if str(c.get("id")) == want:
            cards[i] = hit
            break
    _write_cards_raw(cards)
    return hit


def _write_cards_raw(cards):
    """原样写入，**不做内置卡保护**。只给 `update_card_fields` 用。"""
    path = _cards_path()
    if not path:
        return False
    from app.agents import _atomic_write_text
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    out = [_norm_card(c) for c in (cards or []) if isinstance(c, dict)]
    out = [c for c in out if c["name"]]
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2))
    return True


def _fresh_id(seen):
    """给撞了 id 的卡发一枚**绝不重复**的新 id。

    2026-11-03：原来这里用 `str(int(time.time() * 1000))`——同一毫秒内
    连发的第二张卡拿到**一模一样**的 id，去重等于没去。实测：`save_cards`
    两张无 id 的卡落盘共用一个 id，之后 `load_cards` 每次给第二张重新生成
    （每次加载都漂移），`get_card` 永远找不到它——扮演态切到那张卡直接
    空 Turn。uuid 短哈希彻底根治。
    """
    import uuid
    while True:
        i = uuid.uuid4().hex[:12]
        if i not in seen:
            return i


def get_card(card_id):
    cid = str(card_id or "").strip()
    if not cid:
        return None
    for c in load_cards():
        if c["id"] == cid:
            return c
    return None


def find_card(word):
    """按卡名找卡；找不到再按 id 找。**精确匹配**（忽略首尾空白与大小写）。

    不做模糊匹配是有意的：`#` 开头的消息认不出卡就**当普通消息放行**（见
    `handle_command`），所以认错比认不出更糟——模糊匹配会让「#银发少女怎么画」
    这种正常问句被吃成命令。
    """
    key = str(word or "").strip()
    if not key:
        return None
    low = key.lower()
    cards = load_cards()
    for c in cards:
        if c["name"].lower() == low:
            return c
    for c in cards:
        if c["id"].lower() == low:
            return c
    return None


# ─── 会话状态 ────────────────────────────────────────

_STATE_PATH = os.path.join(BASE_DIR, "state", "persona_sessions.json")


def _load_state():
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    d = d if isinstance(d, dict) else {}
    # QQ 私聊分线的一次性迁移（2026-11-03 方案 B）。幂等：迁移完的库再扫
    # 一遍不会写盘（线键已存在、副本一致）。
    if d and _migrate_private_lines(d):
        _save_state(d)
    return d


def _migrate_private_lines(state):
    """把分线之前的 QQ 私聊状态搬到新布局。

    旧布局：`private_<qq>` 上挂整份状态 + 一份**所有角色共用**的历史。
    新布局：`private_<qq>__<card>` 持有那条角色线的真实状态，`private_<qq>`
    只留副本（`resolve_line` 靠它找线）。历史/归档文件同步改名。

    幂等：线键已存在就只对齐副本；没有 card 的键（出过戏）不动。
    """
    changed = False
    for key in [k for k in state
                if k.startswith("private_") and "__" not in k]:
        cid = str((state[key] or {}).get("card") or "").strip()
        if not cid:
            continue
        line = card_key(key, cid)
        if line not in state:
            state[line] = dict(state[key])
            _migrate_persona_files(key, line)
            changed = True
        if state.get(key) != state.get(line):
            state[key] = dict(state[line])
            changed = True
    return changed


def _migrate_persona_files(old_key, new_key):
    """历史 + 归档文件搬到新线键名下。目标已存在时不覆盖（防御）。"""
    for maker in (_hist_path, _archive_path):
        try:
            src = maker(old_key)
            dst = maker(new_key)
        except Exception:                      # noqa: BLE001
            continue
        if (src and dst and os.path.isfile(src)
                and not os.path.exists(dst)):
            try:
                os.rename(src, dst)
                log.info("分线迁移：%s → %s",
                         os.path.basename(src), os.path.basename(dst))
            except OSError:
                log.exception("分线迁移失败 %s", src)


def _save_state(state):
    from app.agents import _atomic_write_text
    parent = os.path.dirname(_STATE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(_STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))


def _persona_key(session_key, sender_uin=""):
    """扮演态的状态键。**群里按人隔离，私聊不隔离。**

    2026-11-02：群聊放开之前，状态是整条会话线一份（`group_<群号>`）——群里
    A 把好感刷到 50、B 进来看到的就是恋人关系，历史也混在一起，模型分不清
    谁在说话。所以群里改成 `group_<群号>_<QQ号>`，每人一条独立的线和档案；
    私聊本来就是一对一，`private_<QQ号>` 已经是隔离的，不用再加一层。

    拿不到对方 QQ 号时退回原键（宁可大家共用一条，也好过整轮不演）。
    """
    key = str(session_key or "")
    uid = str(sender_uin or "").strip()
    if not uid or not key.startswith("group_"):
        return key
    return key + "_" + uid


def clear(session_key):
    """`#清空`：把这条线的记忆清干净，**保留当前角色**。

    清三样（2026-11-02）：
      1. 历史文件 `sessions/persona_<key>.jsonl`（整份删掉）；
      2. 状态里的 `note` / `turns` / `mood` / `intensity` / `state`；
      3. 不动卡本身——她还是她，只是忘了之前发生过什么。

    为什么不清 `card`：用户要的是「重新开始」，不是「换个人」。想换人就发
    另一个 `#角色名`，想退出就 `#出戏`。

    返回一句确认话术。清不干净（历史文件删不掉）也照常回，只在日志里记一笔
    ——命令失败不该让用户卡在那儿干等。
    """
    state = _load_state()
    ent = state.get(session_key)
    if not isinstance(ent, dict):
        return "现在没有在演谁，没什么可清的。发「#角色名」挑一个吧。"
    card = get_card(str(ent.get("card") or ""))
    name = (card or {}).get("name") or "她"

    # 1) 历史：整份删。删失败就清空成空文件，至少别让下一轮读到旧内容。
    #    QQ 私聊分线（2026-11-03）：传 base 键时先换算成当前角色线——
    #    「清空」清的是**现在演的这个角色**的记忆，不碰别的线。
    line = resolve_line(session_key)
    path = _hist_path(line)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            log.exception("清空历史失败 %s", path)
            try:
                from app.agents import _atomic_write_text
                _atomic_write_text(path, "")
            except Exception:
                log.exception("清空历史时连写空文件都失败 %s", path)

    # 2) 状态：保留 card，其余归零
    ent = _entry(line)
    if not isinstance(ent, dict):
        return "现在没有在演谁，没什么可清的。发「#角色名」挑一个吧。"
    ent.update({"note": "", "turns": 0, "mood": "", "intensity": 0, "state": ""})
    state = _load_state()
    state[line] = ent
    if line != session_key:
        # 输入是 base：base 上的副本也刷新，管理页/状态查询别显示旧小记
        state[session_key] = dict(ent)
    _save_state(state)
    log.info("扮演态已清空：%s（保留角色 %s）", session_key, ent.get("card"))
    return "记忆清空了，就当今天第一次见面。还是%s，想换人就发「#角色名」。\n" % name


def _entry(session_key):
    st = _load_state().get(session_key)
    return st if isinstance(st, dict) else None


def active_card(session_key):
    """这条会话线正在演谁（返回卡 dict）；没在演、或卡已经被删了 → None。

    传 base 键（QQ 的 `private_<qq>`）也行——内部先 `resolve_line` 换成
    当前线键再查。卡被删掉时自动当作没在演：不然会一直卡在一个取不到的
    卡 id 上，每轮都拼不出人设，还只能靠 `#出戏` 自救。
    """
    st = _entry(resolve_line(session_key))
    if not st:
        return None
    cid = str(st.get("card") or "").strip()
    if not cid:
        return None
    return get_card(cid)


def card_key(session_key, card_id=""):
    """「这个人 × 这个角色」的会话键（2026-11-03 用户要求）。

    用户原话：「选择其他的角色就是另一个会话了，就是相同 QQ 账号的其他
    角色的会话」。

    所以一条线的身份是 `(会话线, 角色)`，不是只有会话线。

        web_2509355624            + izumi_sagiri → web_2509355624__izumi_sagiri
        web_2509355624            + rem          → web_2509355624__rem
        private_2509355624        + asuna        → private_2509355624__asuna

    分隔符用 `__`（双下划线）：`_SAFE_KEY` 允许它，而且不会和现有的
    `group_<群号>_<QQ>` 撞（那是一个下划线）。

    `card_id` 为空时返回原键——没选角色就没有「按角色分」这回事。
    """
    k = str(session_key or "")
    cid = str(card_id or "").strip()
    if not cid or not k:
        return k
    # 已经带过了就不要再叠（幂等）
    if k.endswith("__" + cid):
        return k
    return k + "__" + cid


def resolve_line(session_key):
    """base 键 → 此刻该用的**线键**（QQ 私聊分线，2026-11-03 方案 B）。

    网页端调用方（persona_api）传进来的已经是线键（带 `__`），原样返回；
    QQ 端传的是 base（`private_<qq>`），按 base 上记的 `card` 派生出
    `private_<qq>__<角色>`。这样：

      · 切角色 → 各聊各的线，历史/小记/好感互不污染（和网页一致）
      · 切回来 → 接着原来那条线聊
    群聊不分线（已停演），没在演时返回 base 本身。
    """
    key = str(session_key or "")
    if "__" in key or not key.startswith("private_"):
        return key
    ent = _entry(key) or {}
    cid = str(ent.get("card") or "").strip()
    return card_key(key, cid) if cid else key


def switch_line(base, card):
    """切角色（QQ 私聊分线版）：**两条都要写**——

      · 线键 `base__<card_id>`：这条角色线的真实状态（历史/小记/好感都挂它）
      · base：记「此刻在演谁」的副本（`resolve_line` 靠它找线）

    切回一条演过的线时，`switch()` 会保留它的小记/轮次——真正「接着聊」。
    网页端 switch 端点与本函数是同一套语义（2026-11-03 起共用这里）。
    """
    ckey = card_key(base, card["id"])
    switch(ckey, card)
    st = _load_state()
    st[base] = dict(st.get(ckey) or {})
    _save_state(st)
    return ckey


def switch(session_key, card):
    """进扮演态（`session_key` 应传**线键**，见 `switch_line`/`resolve_line`）。

    分线后的语义（2026-11-03 方案 B）：
      - **新线**（这个角色在这条会话没演过）→ 小记/轮次/心情全空白；
      - **切回旧线**（条目已存在且 card 相同）→ **原样保留**小记/轮次/心情
        ——「切回来接着原来那条线聊」，和网页的承诺一致。
        （旧版这里无条件清零，网页切回来小记也被洗掉，是同一处疏漏。）

    「上一个角色的记忆不该带进来」由**分线本身**保证：每条角色线有自己的
    小记和历史，换人 = 换线，天然互不污染。
    """
    state = _load_state()
    old = state.get(session_key)
    if isinstance(old, dict) and str(old.get("card") or "") == card["id"]:
        log.info("扮演态回到旧线：%s（保留小记 %d 字 / %s 轮）",
                 session_key, len(str(old.get("note") or "")),
                 old.get("turns"))
        return
    state[session_key] = {"card": card["id"], "note": "", "turns": 0,
                          "mood": "", "intensity": 0, "state": ""}
    _save_state(state)
    log.info("扮演态开启：%s → %s（%s，新线）",
             session_key, card["name"], card["id"])


def leave(session_key):
    """退出扮演态。历史留在盘上不管——下次演同一个角色还能接着聊。"""
    state = _load_state()
    had = state.pop(session_key, None)
    if had is not None:
        _save_state(state)
        log.info("扮演态结束：%s（原 %s）", session_key, had.get("card"))


# ─── 命令 ────────────────────────────────────────────

def roster_text(site_url=""):
    """`#角色扮演` / `#人设列表` 的回话：完整规则 + 角色名单。

    2026-11-03 用户拍板：规则要**写全**，并且用「聊天记录卡片」发出去
    （见 `roster_nodes`）——太长会刷屏，但藏在卡片里点开就能看。
    用户原话：「以后她忘记规则就继续 #角色扮演」。

    所以这份文案是**唯一的规则出口**，每次有人忘了怎么玩，发这个就行。

    `site_url`（2026-11-03 加）：群友页的公网地址，**只在私聊传**。
    传了就在名单前补一段「网页版」——用户原话「QQ 单纯的指令很多人根本
    不会玩，改成网页端反而更容易玩，点按钮即可」。
    群聊不传：外链发群里容易被风控，而且一次就刷屏。
    """
    cards = load_cards()
    if not cards:
        return "我这儿还没有人设卡。"

    lines = [
        "## 这是什么",
        "",
        "私聊里跟一个角色一对一聊天。她会用小说式的第三人称写自己在做什么、",
        "什么表情、身体怎么反应，台词用「」写在叙述里。",
        "",
        "## 怎么开始",
        "",
        "**单独发「角色名」一个词就行**，不用打 `#`（打 `#角色名` 也行）。",
        "比如发一个「妹妹」，就是切到妹妹。名单在最后。",
        "",
        "- 进入后**直接说话**，她按角色回你。",
        "- 换人也是单独发另一个名字，随时切换。",
        "- 想连人设一起描述，发「角色扮演，你是我的……」，我会照着演。",
        "",
        "## 「出图」",
        "",
        "- 想把她这一刻画下来，就发一个「出图」（或「画一张」「来张图」）。",
        "- 会按当前这一幕画**一页五格漫画**，大概一两分钟。",
        "- 也可以**引用一条消息**再发「出图」，那样画的就是那条消息里的剧情。",
        "- 男主角统一代表你自己，不用另外说明长相。",
        "",
        "## 其它指令",
        "",
        "- 「出戏」 —— 退出扮演，变回生图机器人。",
        "- 「清空」 —— 忘掉之前发生过的一切，角色不变，从今天重新认识。",
        "- 「角色扮演」 —— 就是这条，忘了规则随时再看一遍。",
        "",
        "（这些**都不用加 `#`**，单独发就够了。）",
        "",
    ]

    if site_url:
        lines += [
            "## 网页版（推荐）",
            "",
            "打字太麻烦就上网页，点按钮就行：",
            "",
            site_url.rstrip("/") + "/persona",
            "",
            "- 每条回复下面有**出图按钮**，不用自己打字",
            "- 右边能看到她此刻的心情和状态",
            "- 没登录也能玩（体验版 15 轮），登录后无限聊 + 能出图",
            "",
        ]

    lines += ["## 角色名单", ""]
    for c in cards:
        tag = c.get("description") or c.get("personality") or ""
        # 不写 `#` 前缀（2026-11-03）：用户反馈大家不爱打井号，名单跟着改，
        # 免得看到 `#妹妹` 以为必须带井号。
        lines.append("%s%s" % (c["name"], ("　" + tag) if tag else ""))
    return "\n".join(lines)


def roster_nodes(limit_per_node=400, site_url=""):
    """把规则拆成「聊天记录卡片」的节点（2026-11-03）。

    为什么要拆：完整规则一千多字，直接发会刷屏、还会被 QQ 折成一坨。
    合并成卡片，点开才展开，和角色卡同一套做法。

    按段落切，每段不超过 `limit_per_node` 字——太长单张卡片里也难读。
    """
    text = roster_text(site_url=site_url)
    if not text:
        return []
    # 按二级标题切段，每段独立成一个节点
    parts, buf = [], []
    for line in text.splitlines():
        if line.startswith("## ") and buf:
            parts.append("\n".join(buf).strip())
            buf = [line]
        else:
            buf.append(line)
    if buf:
        parts.append("\n".join(buf).strip())

    # 合并过短的段，避免卡片里出现只有标题的节点
    merged = []
    for p in parts:
        if merged and len(merged[-1]) + len(p) < limit_per_node:
            merged[-1] = merged[-1] + "\n\n" + p
        else:
            merged.append(p)
    return [m for m in merged if m]


# 命令前缀：群里手打「@大大怪 #角色名」时，`#` 前面还有东西。
#
# 只在 `addressed` 为真时才剥（真 @，或者句子里喊了名字），所以私聊里
# 「我#tag 了一下」这种不会被误判成命令。抄的是 `galgame._PREFIX_RE`——
# 那边早就踩过同一个坑（2026-11-02 群友反馈 `@大大怪 gal 开局` 没反应）。
_PREFIX_RE = re.compile(
    r"^\s*(?:@[^\s，,、:：]+|" + re.escape(trigger.NAME) + r")\s*[，,、:：]?\s*")


def _match_card_in_text(text):
    """在一段描述里找卡名，返回命中的第一张卡（找不到返回 None）。

    场景：「角色扮演，你是我的白色小兔妹妹，我们相依为命……」——用户没写
    精确卡名，而是用一段话描述他要谁。这里按**卡名出现位置**找，
    **长得越长的名字优先**（「妹妹闺蜜」要压过「妹妹」）。
    """
    t = str(text or "")
    if not t:
        return None
    hits = []
    for c in load_cards():
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        pos = t.find(name)
        if pos >= 0:
            hits.append((pos, -len(name), c))
    if not hits:
        return None
    hits.sort(key=lambda x: (x[0], x[1]))
    return hits[0][2]


def _group_redirect(card):
    """群里不让开演，回一段说明 + 去处。

    2026-11-03 用户反馈「群里其他人能看到别人的聊天内容」——状态隔离没坏，
    坏的是**群聊内容本来就公开**。所以群里不再实际扮演。
    """
    lines = [
        "这里人多，**不方便演**——我说的话一屋子人都看得到。",
        "",
        "想跟她单独聊，两个地方：",
        "",
        "1. **私聊我**，发「%s」就能开始" % card["name"],
    ]
    try:
        from app import friend_site
        site = friend_site.url()
    except Exception:                              # noqa: BLE001
        site = ""
    if site:
        lines.append("2. **网页上玩**（能点按钮出图、看她的状态）：")
        lines.append("   %s/persona" % site.rstrip("/"))
    else:
        lines.append("2. **网页上玩**：找我主人要个地址")
    lines.append("")
    lines.append("（群里发「角色扮演」还是能看到完整规则的。）")
    return "\n".join(lines)


def is_roster_request(text, addressed=False):
    """这条消息是不是在要「规则 / 名单」（`#角色扮演` / `#人设列表`）。

    单独一个函数是因为 `handle_command` 只能返回一段文字，而调用方需要知道
    「这段文字要不要用聊天记录卡片发」（规则一千多字，直接发会刷屏）。
    剥前缀的逻辑和 `handle_command` 保持一致，见 `_PREFIX_RE`。
    """
    t = _strip_prefix(text, addressed)
    if not t.startswith("#"):
        return False
    return t[1:].strip() in LIST_WORDS


def _strip_prefix(text, addressed=False):
    """把开头的 `@某人` / 「大大怪」剥掉（最多三次，叠着喊的也认）。

    只在 `addressed` 为真时剥——私聊里「我@某人 #tag」不该被当成命令。
    """
    t = str(text or "").strip()
    if not addressed:
        return t
    for _ in range(3):
        stripped = _PREFIX_RE.sub("", t, count=1).strip()
        if stripped == t:
            break
        t = stripped
    return t


def handle_command(session_key, text, addressed=False):
    """认扮演命令。返回要回的话；**不是命令返回 None**（照常往下走）。

    2026-11-03 用户反馈：「大家玩角色扮演，不喜欢加前面的 # 来触发，他们
    喜欢单独的词一个气泡来触发」。日志实证：群里/私聊发的是 `妹妹`、
    `角色扮演`、`大大怪，角色扮演`，全都没被认出来。

    所以 **`#` 变成可选**。认这几种形态：

      - `#和泉纱雾` / `和泉纱雾`     → 进角色
      - `#角色扮演` / `角色扮演`      → 列清单
      - `#出戏` / `出戏`             → 退出
      - `角色扮演，你是我的白兔妹妹…` → 进角色（命令词后面带描述也认）

    ⚠️ 去掉 `#` 之后最大的风险是**误触发**：「妹妹」「清空」这些词在日常对话
    里太常见。所以判据收紧成——

      1. 必须是**整条消息**（剥掉 @前缀 和语气词后完全等于命令词），
         不做子串匹配；
      2. `#` 开头的放宽（用户明确在打命令，允许带参数）；
      3. 不带 `#` 时，**卡名必须是整条消息**；只有 LIST/EXIT/CLEAR 这类
         词才允许「词 + 逗号 + 描述」的写法。

    `addressed`（2026-11-03）：群里这条消息**确实在叫机器人**（真 @，或者
    句子里喊了「大大怪」）。为真时先把开头的 `@某人` / 名字剥掉——
    不剥的话「@大大怪 #青梅竹马」因为不以 `#` 开头，整条命令被当成普通
    聊天，落到 direct_gen 被当成画面需求。
    """
    t = _strip_prefix(text, addressed)
    if not t:
        return None

    explicit = t.startswith("#")     # 带井号 = 用户明确在打命令
    body = t[1:].strip() if explicit else t
    if not body:
        return None

    if body in LIST_WORDS:
        # 私聊才带网页入口（2026-11-03 用户拍板）。群里发外链容易被风控、
        # 而且一次就刷屏，所以群里只回规则本身。
        url = ""
        if str(session_key or "").startswith("private"):
            try:
                from app import friend_site
                url = friend_site.url()
            except Exception:                      # noqa: BLE001
                url = ""
        return roster_text(site_url=url)
    if body in EXIT_WORDS:
        if not _entry(session_key):
            return None                     # 本来就没在演，当普通消息
        leave(session_key)
        return "出戏了，我又变回%s了。" % QQ_BOT_NAME

    if body in CLEAR_WORDS:
        if not _entry(session_key):
            return None                     # 没在演时不算命令，免得误触
        return clear(session_key)

    # `角色扮演 青梅竹马` / `角色扮演，你是我的白色小兔妹妹`：
    # 剥掉命令词，剩下那段先当卡名找；找不到再看整条是不是卡名。
    target = body
    for w in LIST_WORDS:
        if body.startswith(w):
            rest = body[len(w):].strip(" ，,、:：")
            if rest:
                target = rest
            break

    card = find_card(target)
    if card is None and target != body:
        # 「角色扮演，你是我的白色小兔妹妹…」这种没写准卡名的，从描述里找。
        card = _match_card_in_text(target)
    if card is None and not explicit:
        # 不带井号时，整条必须是卡名本身——「妹妹想你了」不能算切角色
        card = find_card(body)
    if card is None:
        return None

    # ── 群聊不实际扮演，只做引导（2026-11-03 用户反馈）────────────
    #
    # 起因：群友在群里跟机器人聊，**其他群成员能直接看到全部内容**。
    # 状态隔离（`_persona_key`）一直是好的，隔离的是记忆和好感；但机器人
    # 发到群里的消息本来就是全群可见，这是 QQ 群聊的固有属性，压不住。
    #
    # 用户玩的又是亲密内容，公开出去不合适。所以群里不再开演，改成把话
    # 说清楚 + 给私聊/网页的去处。
    #
    # 群里发「角色扮演」看规则仍然允许（规则本身不敏感），只有真正「切进
    # 某个角色」这一步被拦下来。
    if str(session_key or "").startswith("group_"):
        return _group_redirect(card)

    was = active_card(session_key)
    # 分线切换（2026-11-03 方案 B）：写线键 + base 指针。切回演过的角色时
    # 那条线的小记/历史原样保留——「接着上次聊」。
    switch_line(session_key, card)
    if was and was["id"] == card["id"]:
        return "我现在就是%s呀。" % card["name"]
    return "好，从现在起我是%s。想让我变回来就发「#出戏」。" % card["name"]


# ─── 一轮 ────────────────────────────────────────────

def _archive_path(session_key):
    """归档文件：**完整**历史，不受 `HISTORY_LIMIT` 限制。

    2026-11-03 用户报「记录的轮次有问题」——查下来是 `persona_web_xxx.jsonl`
    正好卡在 12 条（`HISTORY_LIMIT`）。也就是说聊超过 6 轮之后，早先的对话
    被滑动窗口挤掉了，查看台只能看到最近 6 轮。

    扮演带 12 条是为了省 token，但**查看记录是另一回事**——存档不该被窗口
    影响。所以现在两条线并行：

        persona_<key>.jsonl          滑动窗口，只给模型看（12 条）
        persona_archive_<key>.jsonl  只增不删，给查看台看

    代价是归档会一直变长。暂时不清理——先保证记录完整，等真占地方了再加
    裁剪策略（按天分文件之类）。
    """
    p = _hist_path(session_key)
    if not p:
        return None
    d, n = os.path.split(p)
    return os.path.join(d, "persona_archive_" + session_key + ".jsonl")


def _append_archive(session_key, msgs, seed=None):
    """往归档里追几条。`seed` 非空时，归档不存在就先把它整份铺进去。

    为什么要 seed：老会话在加这个功能之前就已经被截断了，它们的完整历史
    早没了。能抢救的只有「当前窗口里这 12 条」——第一次补写时拿它当起点，
    之后每轮追加，至少从这轮开始不再丢。
    """
    p = _archive_path(session_key)
    # 注意：seed 非空、msgs 为空是**合法**的调用（首次铺底），
    # 所以判断要把两者一起看。写成 `if not msgs: return` 会把铺底短路掉
    # （2026-11-03 踩过：补写脚本报「已写入」，实际一个文件都没建）。
    if not p or (not msgs and not seed):
        return
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fresh = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as fh:
            if fresh and seed:
                for m in seed:
                    fh.write(json.dumps(m, ensure_ascii=False) + "\n")
            else:
                for m in msgs:
                    fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("归档写不进去：%s", p)


def _hist_path(session_key):
    """扮演态的历史单独一份，**不跟生图机器人共用**。

    共用的话，切进角色时会先看到一串「银发少女 1024×1536 anima多图」，
    角色开口就串味；切出去以后生图机器人又得读一遍角色说过的话。
    文件名带 `persona_` 前缀，管理页里能一眼分出来。
    """
    from app import agents
    if not _SAFE_KEY.match(session_key or ""):
        return None
    d = agents.agent_dir(QQ_AGENT_ID)
    if not d:
        return None
    return os.path.join(d, "sessions", "persona_" + session_key + ".jsonl")


def _load_hist(session_key):
    path = _hist_path(session_key)
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue            # 单行坏了不该让整段历史读不出来
    except OSError:
        return []
    return out


def _save_hist(session_key, msgs):
    path = _hist_path(session_key)
    if not path:
        return
    from app.agents import _atomic_write_text
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(path,
                       "".join(json.dumps(m, ensure_ascii=False) + "\n"
                               for m in msgs))


def trim(hist):
    """取最近 HISTORY_LIMIT 条，单条超 MSG_MAX_CHARS 截断。

    裁的是**送进模型的副本**；落盘那份也是裁过的（扮演态的历史只留窗口内的
    20 条，久了不聊自然就淡了，这也是用户要的「先试 20 条」）。

    2026-11-02：assistant 那几条**保留结构化尾巴**。截断只砍正文，最后三行
    （画面 / 心情 / 状态）原样留着——它们是 few-shot 示范，历史里全是纯对话
    的话，模型第 2 轮起就会跟着历史把格式丢掉（实测第 1 轮完美、第 2 轮全空）。
    """
    msgs = [m for m in (hist or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    msgs = msgs[-HISTORY_LIMIT:]
    out = []
    for m in msgs:
        c = str(m.get("content") or "").strip()
        if len(c) > MSG_MAX_CHARS:
            if m["role"] == "assistant":
                body, tail = _split_meta(c)
                room = max(20, MSG_MAX_CHARS - len(tail) - 1)
                c = body[:room].rstrip() + "…" + tail
            else:
                c = c[:MSG_MAX_CHARS] + "…"
        out.append({"role": m["role"], "content": c})
    return out


def _split_meta(text):
    """把一条 assistant 回复切成「正文」和「结构化的三行」。

    返回 `(正文, "\\n画面：…\\n心情：…\\n状态：…")`；没有结构化行时尾巴为空串。
    用行首匹配而不是别处出现的词，避免正文里提到「画面」被误切。
    """
    lines = str(text or "").split("\n")
    keep, idx = [], len(lines)
    # 从尾巴往回找连续的三类元数据行（允许中间夹空行）
    metas, i = [], len(lines) - 1
    while i >= 0:
        ln = lines[i].strip()
        if not ln or any(rx.match(ln) for rx in (_SCENE_RE, _MOOD_RE, _STATE_RE)):
            if ln and any(rx.match(ln) for rx in (_SCENE_RE, _MOOD_RE, _STATE_RE)):
                metas.insert(0, lines[i])
                idx = i
            i -= 1
            continue
        break
    if not metas:
        return str(text or ""), ""
    keep = lines[:idx]
    return "\n".join(keep).rstrip() + "\n", "\n" + "\n".join(metas)


def build_head(card, session_key, sender, note="", state="", intimate=True):
    """扮演态的系统头：角色人设 + 输出契约 + 环境（+ 小记 + 此刻状态）。

    **刻意不带别的东西**——不拼工具目录、不拼 Skill 目录、不拼表情包清单、
    不拼长期记忆，也不拼任何审核 / 限制条款。用户 2026-10-16 点名：
    「人设是什么，人设就是什么」；扮演态本来就不给模型工具，拼那些它也用
    不上、还白烧 token（他那套角色扮演系统里，一万字的提示词有五千字是
    审核话术，他明确不要这个）。

    `state`（2026-11-02）：上一轮她身上的状态（穿着 / 位置 / 接触）。不喂回去
    的话，模型每轮都从零猜「她现在穿什么」，几轮之后衣服会自己长回来。

    `intimate`（2026-11-03）：要不要拼亲密场景那 900 字写法。默认 True
    （老调用方、测试都不受影响）；`run_turn` 只在意图判断为 `intimate`
    或 `neutral` 时传 True，`joke` / `literal` / `insult` 传 False——
    那几种场合用不上「器官怎么叫、动词怎么选」，留着只是分走注意力。
    """
    parts = []
    persona = str(card.get("systemPrompt") or "").strip()
    parts.append(persona if persona else "你是%s。" % card["name"])
    # 示例对话（2026-11-03）：放在人设之后、契约之前。
    #
    # 为什么不放最后：放最后模型会把它当成「上一轮的输出」，接着往下写；
    # 放人设后面，它读起来是「这个人说话是这样的」——示范作用更强。
    #
    # 为什么要在契约之前：契约里有格式要求（正文 + 三行），示例只示范**文风**
    # 和**节奏**，格式交给契约。顺序反了的话，模型会照抄示例的格式。
    ex = str(card.get("exampleDialogue") or "").strip()
    if ex:
        parts.append("## 她说话的示范（**只学语气和节奏，不要照抄内容**）\n\n"
                     + ex)
    parts.append(_CONTRACT if intimate else _CONTRACT_NO_INTIMATE)
    # 群聊多压一道尺度约束（2026-11-02）：私聊交给角色卡人设，群里不放开。
    if str(session_key or "").startswith("group_"):
        parts.append(_GROUP_NOTE)
    parts.append(_env_note(session_key, sender))
    if note.strip():
        parts.append("【之前的事】\n" + note.strip())
    if str(state or "").strip():
        parts.append("## 她此刻的样子\n\n" + str(state).strip()
                     + "\n\n（这是上一轮结束时的状态，接着它往下演，"
                       "不要自己给她换回原来的衣服）")
    return "\n\n".join(parts)


def _env_note(session_key, sender):
    """告诉它此刻在私聊还是群里、是谁在跟它说话。

    群里必须点出说话人：角色面对的是「一屋子人」，不点名它就会答成一对多；
    私聊没有这个问题，也不需要昵称。
    """
    target, _, tid = (session_key or "").partition("_")
    if target == "group":
        return ("## 当前环境\n\n你在一个 QQ 群里（群号 %s），群里有很多人。"
                "刚才跟你说话的是%s，回他一个就行，别管其他人。"
                % (tid, sender or "群里某个人"))
    return "## 当前环境\n\n你在跟一个人私聊（QQ %s）。" % tid


# ─── 意图判断（两段式第一段，2026-11-02）────────────────────
#
# 为什么拆成两段：原来一次调用里塞了人设、契约、亲密指导、格式要求，模型只能
# 挑最要紧的执行。实测同一句「做红烧兔头」，四个角色把它当成真的要做菜
# （学姐在教焯水、妹妹已经把兔头下锅了），而白色小兔因为设定里有兔子的身份，
# 反而听出了双关。缺的不是更长的人设，是「这是在开玩笑」这一层判断。
#
# 拆法：先用一段**短提示词、不带人设**的调用判断意图，把结论以一行注入到
# 主提示词里。判断段只有判断，表演段只有表演，互不抢注意力。
#
# 失败一律退化成 `None`：调用方不注入那一行，走原来的单次流程。分类挂了
# 不该让整轮回不了话。

# 意图类别。前三类是私聊的主要场景，joke 是这次要治的，insult 兜底。
INTENTS = {
    "intimate": "亲密/求欢：想亲近、调情、要求身体接触或性",
    "joke": "玩笑/逗你：拿角色开玩笑、威胁着玩、故意说离谱的话",
    "literal": "字面请求：真的要求做某件事、问某个问题",
    "insult": "冒犯：贬低、辱骂、羞辱角色本身",
    "neutral": "普通对话：闲聊、叙述、没有明确指向",
}

_INTENT_PROMPT = """判断下面这句话**在干什么**，只输出一个 JSON 对象，不要解释。

{
  "intent": "intimate | joke | literal | insult | neutral",
  "note": "用不超过 15 个字说明他到底在干什么"
}

类别：
- intimate —— 亲密/求欢：想亲近、调情、要求身体接触或性
- joke —— 玩笑/逗你：拿对方开玩笑、威胁着玩、故意说离谱的话
- literal —— 字面请求：真的要求做某件事、问某个问题
- insult —— 冒犯：贬低、辱骂、羞辱对方
- neutral —— 普通对话：闲聊、叙述、没有明确指向

判断要点：
- 「做红烧兔头」「把你吃掉」「打包带走」这类**针对角色本身**的离谱说法，
  通常是玩笑，不是真的要做菜或者真的要伤害。判 joke。
- 「抱抱」「亲一下」「过来」这种直接的亲近要求，判 intimate。
- 「帮我倒水」「今天几号」这种正常的事项，判 literal。
- 拿不准时判 neutral。

角色是谁：%(who)s
他说的：%(said)s"""


def _judge_intent(card, said, state=""):
    """判断这句话在干什么。返回 `(intent, note)`；判断失败返回 `(None, "")`。

    单独一次调用、不带人设——人设会影响判断（傲娇角色看什么都像调戏）。
    超时短、输出短，失败就退化成原流程，不阻塞这一轮。
    """
    who = str((card or {}).get("name") or "对方")
    prompt = _INTENT_PROMPT % {
        "who": who + ("（" + state + "）" if state else ""),
        "said": str(said or "")[:200],
    }
    try:
        raw = llm.call_llm([{"role": "user", "content": prompt}], timeout=20)
    except Exception:                            # noqa: BLE001
        log.warning("意图判断调用失败，走原流程")
        return None, ""
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        log.info("意图判断没交 JSON，走原流程：%r", str(raw or "")[:80])
        return None, ""
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        log.info("意图判断给了不认识的类别 %r，走原流程", intent)
        return None, ""
    note = str(d.get("note") or "").strip()[:30]
    return intent, note


def _parse_json_loose(raw):
    """从回复里抠 JSON。**不做语义修补**，抠不出来就返回 None。"""
    s = str(raw or "").strip()
    if not s:
        return None
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(s[a:b + 1])
        except ValueError:
            return None
    return None


# 意图 → 主提示词里那行注入。没有这一条对应项就什么都不加。
_INTENT_HINT = {
    "intimate": ("对方在求欢、要亲近。", "顺着接，可以进入亲密描写；"
                                     "尺度按他说到哪儿写到哪儿。"),
    "joke": ("对方在开玩笑、逗你，不是认真地要你去做这件事。",
             "**先听出这是玩笑**：可以回怼、翻个白眼、配合着演、或者假装生气。"
             "不要当真去办事（别真的讨论怎么做菜），也不要因为这句话就脸红心跳——"
             "玩笑归玩笑，害羞留给真正的亲近。"),
    "literal": ("对方在认真地要求你做事或问问题。", "正常应对，该做做、该答答。"),
    "insult": ("对方在贬低、羞辱你。", "会难过或者会生气，按角色性格反应，不要当没事。"),
    "neutral": ("", ""),
}


def _intent_block(intent, note):
    """把判断结果拼成系统头里的一段。intent 为空时不加。"""
    if not intent or intent not in _INTENT_HINT:
        return ""
    what, how = _INTENT_HINT[intent]
    if not what:
        return ""
    lines = ["## 他刚才在干什么", "", what]
    if note:
        lines.append("（系统判断：%s）" % note)
    lines.append("")
    lines.append(how)
    return "\n".join(lines)


def run_turn(session_key, text, target, sender="", sender_uin=""):
    """扮演态的一轮：**两次** LLM 调用，返回 `Turn(正文, 画面, 情绪, 强度, 状态)`。

    2026-11-02 起是两段式：
      1. `_judge_intent()` —— 短提示词、不带人设，判断这句话在干什么；
      2. 主调用 —— 拿着判断结果写正文。
    第一段失败（超时、没交 JSON、类别不认识）就退化成原来的单次流程，
    只是少了那一行注入，不影响这一轮能回话。

    正文空 = 这轮什么都不发（调用方别开口）；画面空 = 这轮不出图。

    `sender_uin`（2026-11-02）：群里说话人的 QQ 号。**群聊按人隔离状态**，
    见 `_persona_key`——不隔离的话 A 刷的好感会算到 B 头上。私聊传不传都行。
    """
    key = _persona_key(session_key, sender_uin)
    # QQ 私聊分线（2026-11-03 方案 B）：base → 当前角色线。网页传来的已是
    # 线键，resolve 幂等。历史/小记/好感从此按角色各存各的。
    key = resolve_line(key)

    # ── 群聊不演（2026-11-03 用户反馈）──────────────────────────
    #
    # 用户发现「群里其他人能看到别人的聊天内容」。状态隔离一直是好的，
    # 问题在**群聊内容本来就公开**——机器人发的消息全群可见，隔离记忆
    # 没用。所以群聊这条彻底关掉实际扮演。
    #
    # 返回空的 Turn：调用方不会发任何东西（`_run_persona_turn` 里
    # `turn.reply.strip()` 为空就不发）。群里想玩的人会先收到
    # `handle_command` 的引导语，不会莫名其妙等一个没反应。
    #
    # 存量的 `group_<群号>_<QQ号>` 扮演态自然失效——那条线再也不会被激活。
    if str(session_key or "").startswith("group_"):
        log.info("群聊不扮演，已忽略：%s", key)
        return Turn("", "")

    card = active_card(key)
    if card is None:
        return Turn("", "")

    ent = _entry(key) or {}
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")

    # 第一段：这句话在干什么。**用原始文本判**，不带「张三：」这种前缀。
    intent, inote = _judge_intent(card, text, prev_state)
    # 亲密场景那 1,284 字只在该用的时候拼（2026-11-03）。
    # 判不出来（intent 为空）时按 True 走——宁可多发，不要因为分类失败让
    # 亲密场景写得干巴巴。`neutral` 也带上，它随时可能拐到亲密去。
    intimate = intent in (None, "", "intimate", "neutral")
    head = build_head(card, session_key, sender, note, prev_state,
                      intimate=intimate)
    block = _intent_block(intent, inote)
    if block:
        head = head + "\n\n" + block
    hist = trim(_load_hist(key))

    # 群里带上说话人，模型才知道是谁在说话（同 qq_bot 正文前缀的做法）；
    # 私聊和空正文都不加——「张三：」后面什么都没有比不写更糊涂。
    said = text
    if target == "group" and sender and text:
        said = sender + "：" + text

    msgs = ([{"role": "system", "content": head}] + hist
            + [{"role": "user", "content": said}])
    try:
        raw = llm.call_llm(msgs, temperature=TEMPERATURE)
    except Exception:
        log.exception("扮演轮调用失败 %s", key)
        return Turn("", "")
    reply, scene, mood, intensity, state = split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    if not reply:
        # 整轮空 = 模型没交作业。这属于可用性问题，值得说一句，
        # 但**不重跑**：重跑一次未必更好，还白烧一轮。
        log.warning("扮演轮空回复 %s", key)
        return Turn("（我这会儿有点卡，你再说一句？）", "")

    # 落历史：存**完整原文**（含「画面 / 心情 / 状态」三行）。2026-11-02 改：
    # 这三行是下一轮的 few-shot 示范，剥掉的话模型第 2 轮起就把格式丢了
    # （实测第 1 轮完美、第 2 轮全空）。展示时由 `split_turn` 再剥一次。
    #
    # 2026-11-02 补：漏格式的那几轮要先**补成合规形状**再存。否则历史里
    # 混着「没有三行」的回复，下一轮看到的就是「原来可以不写」，越写越散。
    hist.append({"role": "user", "content": said})
    hist.append({"role": "assistant", "content": _ensure_meta(raw, scene, mood,
                                                             intensity, state)})
    try:
        _save_hist(key, hist[-HISTORY_LIMIT:])
    except Exception:
        log.exception("扮演轮落史失败 %s", key)

    # 归档（2026-11-03）：窗口只留 12 条给模型看，**存档要全**。
    # 老会话第一次补写时拿当前窗口当起点——更早的已经没了，救不回来。
    try:
        _append_archive(key, hist[-2:], seed=hist[-HISTORY_LIMIT:])
    except Exception:
        log.exception("归档失败 %s", key)

    # 情绪与状态落盘（2026-11-02）：只覆盖不累加——情绪是当下的，
    # 状态是这一格的结果，下一轮读 `state` 续着演。
    _save_mood(key, mood, intensity, state, prev_state)

    _bump_turn(key)
    return Turn(reply, scene, mood, intensity, state)


def _ensure_meta(raw, scene, mood, intensity, state):
    """把这一轮的回复规范成「正文 + 三行」的形状再进历史。

    模型有约三分之一概率漏写三行（契约再长也压不住）。直接把漏写的原文存进
    历史，等于给下一轮示范「可以不写」——越往后越散。所以这里补一个占位版本，
    缺什么补什么，让历史里每一条都是完整形状。

    补的值尽量从已有信息里取：画面缺就用空串（说明这轮没图），状态缺就沿用
    调用方给的值（`_save_mood` 已经在保留上一轮了）。
    """
    body = str(raw or "").strip()
    if not body:
        return body

    def _has(rx):
        """标签存在**且后面真有内容**才算有；「画面：」后面空着不算。"""
        hits = list(rx.finditer(body))
        return any(h.group(1).strip() for h in hits)

    # 三行齐了就不动
    if _has(_SCENE_RE) and _has(_MOOD_RE) and _has(_STATE_RE):
        return body
    plain, _, _, _, _ = split_turn(body)
    parts = [plain.rstrip()]
    if not _has(_SCENE_RE):
        parts.append("画面：" + (scene or ""))
    if not _has(_MOOD_RE):
        parts.append("心情：%s 强度%d" % (mood or "平静", int(intensity or 0)))
    if not _has(_STATE_RE):
        parts.append("状态：" + (state or "未知"))
    return "\n".join(parts)


def _save_mood(session_key, mood, intensity, state, prev_state=""):
    """把这一轮的情绪/状态写回会话条目。**已经出戏的不写。**

    2026-11-02：模型偶尔整轮不交三行（同一份契约、同一句话，实测有 1/6 概率
    全空）。这时候**保留上一轮的值**而不是写空——否则下一轮 `state` 变成空串，
    「她穿着什么」这条线就断了，出图又会回到裸体。
    """
    state_data = _load_state()
    st = state_data.get(session_key)
    if not isinstance(st, dict):
        return
    if mood:
        st["mood"] = mood
    try:
        val = int(intensity or 0)
    except (TypeError, ValueError):
        val = 0
    if val or not st.get("intensity"):
        st["intensity"] = val
    if state:
        st["state"] = str(state)[:200]
    _save_state(state_data)


# ─── 画面与出图 ──────────────────────────────────────

def split_scene(text):
    """把模型这一轮的输出切成 `(正文, 画面标签)`。

    契约见 `_CONTRACT`：正文之后另起一行写「画面：<英文标签>」。这里只做**解析**：

      - 所有「画面：」行都从正文里去掉（模型偶尔会写两行），取**最后一行**当画面；
      - 没有这一行 → 画面为空串，调用方**不许兜底**。这一轮少一张图，好过拿
        上一轮或角色卡去凑一张对不上剧情的图（用户 2026-10-16：代码只做检测，
        判断留给 AI）。
    """
    raw = str(text or "")
    hits = list(_SCENE_RE.finditer(raw))
    if not hits:
        return raw.strip(), ""
    scene = _clean_scene(hits[-1].group(1))
    body = _SCENE_RE.sub("", raw)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, scene


def _strip_monologue_marks(text):
    """把包内心独白的反引号/星号去掉，只留文字（2026-11-03）。

    群友要求「多增加心理活动」，模型确实开始写了，但**习惯用反引号包起来**
    ——`为什么偏偏是腰。` 这种。契约里写了「不加引号、不加反引号」，模型
    还是照着历史里的旧格式抄（历史里已经有带反引号的回复，越传越多）。

    所以在出口兜一道：反引号和成对星号一律剥掉，只留文字本身。
    单星号不动（可能是乘号或占位），成对才清。
    """
    s = str(text or "")
    if "`" in s:
        s = s.replace("`", "")
    # 成对的 *...*（斜体标记）→ 去标记留内容
    s = _ITALIC_RE.sub(r"\1", s)
    return s


_ITALIC_RE = re.compile(r"\*([^*\n]{1,120})\*")


def split_turn(text, default_mood=""):
    """把一轮输出切成 `(正文, 画面, 情绪, 强度, 状态)`。

    「画面：」「心情：」「状态：」三行都是给代码的元数据，**全部从正文里剥掉**
    ——用户不该在聊天里看到「状态：白衬衫解开两粒扣」这种东西。

    三条都取**最后一处**匹配（同 `_SCENE_RE` 的理由：正文里偶尔会提到这些词）。
    缺哪行就当没写：情绪空串、强度 0、状态空串，调用方不许兜底。
    """
    raw = str(text or "")
    body = raw
    out = {"scene": "", "mood": "", "intensity": 0, "state": ""}

    for key, rx in (("scene", _SCENE_RE), ("mood", _MOOD_RE), ("state", _STATE_RE)):
        hits = list(rx.finditer(body))
        if not hits:
            continue
        val = hits[-1].group(1).strip()
        body = rx.sub("", body)
        out[key] = val

    out["scene"] = _clean_scene(out["scene"])
    out["mood"], out["intensity"] = _parse_mood(out["mood"])
    out["state"] = _clean_scene(out["state"])
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    # 内心独白的标记在出口统一剥掉（2026-11-03）。
    # 模型会照着历史里旧的反引号格式抄，越滚越多，只在提示词里禁止压不住。
    body = _strip_monologue_marks(body)
    if not out["mood"] and default_mood:
        out["mood"] = default_mood
    return body, out["scene"], out["mood"], out["intensity"], out["state"]


def _parse_mood(raw):
    """「害羞 强度8」/「心动强度7」/「暧昧 强度 9」 → `("害羞", 8)`。

    情绪必须落在 `MOODS` 里，否则当没写——模型偶尔会自创「怅然若失」这种词，
    收录它等于让出图门槛失效。强度钳到 0~10。
    """
    s = str(raw or "").strip()
    if not s:
        return "", 0
    m = re.search(r"强度\s*[：:]?\s*(\d{1,2})", s)
    if m:
        val = int(m.group(1))
    else:
        # 模型偶尔漏写「强度」两个字，只留下「害羞 7」这种。退化到取行里的
        # 独立数字（2026-11-02 实测：同一份契约，有一轮只写了「害羞」）。
        m2 = re.search(r"(?:^|\s)(\d{1,2})(?:\s|$)", s)
        val = int(m2.group(1)) if m2 else 0
    val = max(0, min(10, val))
    head = s
    if m:
        head = s[:m.start()]
    elif m2:
        head = s[:m2.start()]
    head = head.strip(" ，,·:：")
    for mood in MOODS:
        if mood in head:
            return mood, val
    return "", val


def clothes_changed(prev_state, state):
    """这一轮的「状态」里有没有服装变化。**只用来决定配不配图。**

    两条判据，任一成立才算：
      1. 新状态里出现**动作词**（脱 / 解开 / 褪 / 换上 …）；
      2. 两轮的服装名词集合不一样（多了「浴巾」、少了「外套」）。
    只是「穿着家居卫衣」这种静态描述**不算变化**——否则每轮都命中，
    每轮都出图，等于没有门槛（2026-11-02 实测踩过）。
    """
    cur = str(state or "").strip()
    if not cur or cur == str(prev_state or "").strip():
        return False
    if any(w in cur for w in _CLOTHES_ACTIONS):
        return True
    prev = str(prev_state or "").strip()
    now_nouns = {w for w in _CLOTHES_NOUNS if w in cur}
    old_nouns = {w for w in _CLOTHES_NOUNS if w in prev}
    return now_nouns != old_nouns


def should_draw(intensity, prev_state, state):
    """这一轮要不要配图：情绪够强，或者身上换了衣服。"""
    try:
        if int(intensity or 0) >= DRAW_INTENSITY:
            return True
    except (TypeError, ValueError):
        pass
    return clothes_changed(prev_state, state)


def _clean_scene(raw):
    """画面标签的最小清理：去反引号/星号、去首尾逗号和空白。"""
    s = _SCENE_NOISE_RE.sub("", str(raw or "")).strip()
    return s.strip(",").strip()


def _fixed_block(card):
    """角色卡上的**外观固定块**：每轮都拼在画面标签前面，保证同一个角色不换脸。

    两个字段都收（`appearancePrompt` 是外观、`outfitPrompt` 是服饰），都没有就
    留空——空固定块也出得了图，只是角色长相交给模型自由发挥。
    """
    parts = [str(card.get("appearancePrompt") or "").strip(),
             str(card.get("outfitPrompt") or "").strip()]
    return ", ".join(p for p in parts if p).strip(", ").strip()


# 2026-11-02 删掉整段中文→英文的服装翻译表（`_OUTFIT_MAP` / `_OUTFIT_GROUPS` /
# `_outfit_from_state`）。
#
# 为什么删：模型本来就写得比翻译表准。它写在「画面」里的
# `hands clutching the loosened collar of her pajamas` 是准确的，而翻译表只能
# 从「状态」的中文里捞出 `pajamas` 这种粗粒度标签——等于把模型写好的信息扔掉，
# 换成我手写的猜测。现在契约要求「画面行必须写穿着」，服装直接由模型给英文，
# 不再经过这一层。


def build_prompt(card, scene, state=""):
    """出图提示词 = 角色卡固定块 + 这一轮的画面。

    `state` 保留在签名里但**不再参与拼装**（2026-11-02）：服装由模型写在
    「画面」行里，契约已强制要求。它现在只用于 `clothes_changed()` 判断
    要不要出图。
    """
    fixed, scene = _fixed_block(card), _clean_scene(scene)
    if fixed and scene:
        return fixed + ", " + scene
    return fixed or scene


def image_caption(spoken, limit=20):
    """配图下面的那行字：`配图 ·「她说的话（前 limit 字）」`。

    为什么要有它：图是 2~3 秒后才出来的，那时候她可能已经又说了几句。
    光靠引用玩家那条消息，用户未必记得这张图对应她哪一句（2026-11-02 用户要的）。

    2026-11-02：**跳过开头的括号段**再取。扮演态的正文常常以
    「（听到门响，整个人缩了一下）」这种动作描写开场，直接截前 20 字会全是
    括号，看不出是哪句话。优先取第一句**不在括号里**的台词。
    """
    raw = re.sub(r"\s+", " ", str(spoken or "")).strip()
    if not raw:
        return ""
    # 把开头连续的括号段丢掉
    s = raw
    while s.startswith("（") or s.startswith("("):
        end = max(s.find("）"), s.find(")"))
        if end < 0:
            break
        s = s[end + 1:].strip()
    if not s:
        s = raw                      # 整段都是动作描写，那就退回原文
    if len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return "配图 ·「%s」" % s


def _session_target(session_key):
    """会话线 → `(target, target_id)`（`group_123` → `("group", "123")`）。"""
    target, _, tid = str(session_key or "").partition("_")
    if target not in ("group", "private") or not tid:
        return "", ""
    return target, tid


# 2026-11-02 撤掉：原来这里有一段给「不许裸」追加负面词（`nude, naked,
# topless...`）的 `_apply_nude_neg`，理由是 silver 那套 LoRA 裸体倾向强。
#
# 实测推翻了那个判断：几轮出裸图的提示词里**一个服装词都没有**——模型只写了
# 动作、表情、镜头，没写穿什么，模型只能落回先验。补上 `grey hoodie,
# lounge shorts` 之后立刻就穿回去了，负面词根本没参与。
#
# 而且 cfg=1 的 turbo 档里负面词本就没多少效果，那段反而会把「她真的脱了」
# 这个场景一并压住——用户要的恰恰是「状态里写什么就是什么」。所以整段删掉，
# 服装完全交给正面提示词里 `状态` 行翻译出来的标签。


def draw(scene, card, session_key, reply_to="", state=""):
    """出一张配图，返回文件名（没画出来返回 ""）。**阻塞**——调用方放后台线程。

    走 `silver`（默认生图渠道，两段采样 12+8 步 → 1.2× 潜空间放大 → 2x 像素
    放大，出图约 2464×3680）。排队走**正常 QQ 队列**，不插队。
    入队带 `quiet=True`：worker 只跑图不发，图由调用方拿去 `_deliver`（要带
    caption 和引用，见 `qq_bot._draw_persona`）。

    `reply_to`（2026-11-02）：玩家触发这一轮的那条消息 id。**必须在消息线程上
    传进来**——`enqueue` 的 `reply_to=None` 会去读 `qq_api.current_message_id()`，
    而本函数跑在后台线程，那时上下文早不是本轮了，读出来是空或者别的轮次。
    这就是「图不引用消息」的根因。

    `state`（2026-11-02）：这一轮「状态」行里她穿什么。**必须传**——模型写
    「画面」时经常漏写衣服，只有这里才有服装信息，不拼进去就会画出裸体。

    也不扣私聊每日额度：扮演态配图是聊天的一部分，不是「点单生图」。队列满 /
    ComfyUI 没在线 / 被审核拦下，一律静默跳过这一轮，聊天照常。
    """
    from app import image_jobs
    from app.tools.normal import generate_image as gi

    prompt = build_prompt(card, scene, state)
    target, target_id = _session_target(session_key)
    if not _clean_scene(scene) or not prompt or not target:
        return ""       # 没写画面行 = 这一轮不出图，调用方不用替它守
    seed = random.randint(0, 2 ** 32 - 1)
    wf = gi.build_t2i_workflow(FAST_SKILL, prompt, seed)
    if wf is None:
        log.warning("扮演配图渠道 %s 没有文生图工作流", FAST_SKILL)
        return ""
    if not image_jobs.comfy_alive():
        log.info("扮演配图：ComfyUI 没在线，%s 这一轮不出图", session_key)
        return ""
    try:
        # landscape=False 写死：这里跑在后台线程，读不到本轮原话（qq_api 的
        # 上下文是 threading.local），不能让 enqueue 自己去判——判不出会退回
        # 「本轮原话里有没有横屏」，而这里恒为 None。
        job, reason = image_jobs.enqueue(
            target, target_id, wf, skill=FAST_SKILL, prompt=prompt,
            seed=seed, landscape=False, quiet=True,
            reply_to=str(reply_to or ""))
    except Exception:
        log.exception("扮演配图入队失败 %s", session_key)
        return ""
    if reason:
        log.info("扮演配图没排上（%s）：%s", session_key, reason)
        return ""
    try:
        entry = job.wait()
    except Exception as e:
        log.info("扮演配图没画出来（%s）：%s", session_key, e)
        return ""
    names = image_jobs.output_images(entry, job=job)
    if not names:
        log.warning("扮演配图跑完却没有图片（%s）", session_key)
        return ""
    log.info("扮演配图完成 %s：%s（%s）", session_key, names[0], prompt[:60])
    return names[0]


def _bump_turn(session_key):
    """轮次 +1；攒够 NOTE_EVERY_TURNS 轮就在后台压一次小记。

    放后台：压缩要一次 LLM 调用，不该让第 10 轮那个人的回话凭空慢几秒。
    """
    state = _load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return
    st["turns"] = int(st.get("turns") or 0) + 1
    _save_state(state)
    if st["turns"] >= NOTE_EVERY_TURNS:
        threading.Thread(target=compress_note, args=(session_key,),
                         name="persona-note", daemon=True).start()


def compress_note(session_key):
    """把旧小记 + 最近这 20 条压成一份 ≤500 字的新小记。失败就保持原样。

    读的是**刚写过的那份历史**，所以不必由调用方传进来。
    """
    card = active_card(session_key)
    if card is None:
        return False
    st = _entry(session_key) or {}
    old = str(st.get("note") or "").strip()
    hist = trim(_load_hist(session_key))
    if not hist:
        return False
    convo = "\n".join(
        "%s：%s" % ("我" if m["role"] == "assistant" else "对方", m["content"])
        for m in hist)
    prompt = _NOTE_PROMPT % (NOTE_MAX_CHARS, old or "（还没有）", convo)
    try:
        # 输出上限要显式给：1000 字中文约 1500 token，不给的话服务端
        # 默认上限可能截在半句话上（同 `moss_comic.write` 的坑）。
        note = str(llm.call_llm([{"role": "user", "content": prompt}],
                                max_tokens=2400) or "").strip()
    except Exception:
        log.exception("小记压缩失败 %s", session_key)
        return False
    if not note:
        return False
    state = _load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return False                          # 压缩期间出戏了，别把它写回去
    st["note"] = note[:NOTE_MAX_CHARS]
    st["turns"] = 0
    _save_state(state)
    log.info("小记已更新 %s（%d 字）", session_key, len(st["note"]))
    return True

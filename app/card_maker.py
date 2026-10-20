"""星陨AI 角色卡生成：一句话描述 → 可直接粘贴的六项文案 + 封面图（2026-10-19）。

## 为什么不是一个通用「角色卡」模块

星陨AI 没有开放角色卡格式，它只有 App 里的创建表单（没有官网、没有文档、
不支持导入外部的 PNG/JSON 卡）。社区通用的 chara_card_v2/v3 它一概不认。
所以这里不做「标准卡」，做的是**照它那张表单的字段写文案**：
角色名称 / 开场白 / 人物简介 / 说话风格 / 背景记忆 / 补充规则
（外加表单上的性别、分类、标签；封面另行出图）。

## 六个模块的分工照官方教程（2026-10-19 用户给的《角色卡创建教程》）

    01 角色名称   简洁好记、一眼有辨识度；单名 / 称号 / 「作品名·名字」
    02 开场白     角色自动发来的第一条消息：环境 + 动作神态 + 一句台词
    03 人物简介   基本资料（年龄性别外貌）+ 身份背景 + 初始关系（第一次见面时）
    04 说话风格   语气 / 句式长短 / 口头禅小动作 / 怎么称呼玩家
    05 背景记忆   你们之间已经发生过的往事、共同经历、情感羁绊
    06 补充规则   核心性格 / 喜好厌恶 / 行为边界 / 剧情约束 / 叙事视角

这六句就是边界，最常犯的两个错：把「后来发生的故事」写进简介（简介只写
初始关系，往事归背景记忆）、把性格写进说话风格（那里只写「怎么说」）。
`_SYSTEM` 里把这段原样交代给模型。

## 名字是独立一栏，而且要「正式姓名」

早期版本从「造卡」两个字后面整句取需求，用户打招呼的「大大怪，造卡 …」
会把机器人的昵称一起带进去，模型就把它当成了角色名（2026-10-19 实测）。
现在需求只取「造卡」**后面**那段（见 `card_bot.parse`），提示词里也再挡一道。

命名口径参考酒馆社区卡的实际情况（foreverse-app/character-card-skills 的
中文示例卡：江叙白 / 林知夏 / 林幺 / 沈砚）：**一律「姓 + 名」，不用小名、
昵称、外号、身份词**。星陨教程里允许「称号」，但用户明确说不能写小名，
所以这里收紧成「姓名为主，称号只能写成『称号·姓名』」。

## 借的是酒馆卡的创造范式（2026-10-19 用户要求）

星陨AI 有自己的表单，但「怎么把一张卡写得像人写的」这件事，社区（SillyTavern /
酒馆）已经攒了成套方法。这里搬了三样，落在 `_SYSTEM` 里：

1. **四元组选型**（性向 / 赛道 / 洁度 / 情绪钩子）——`foreverse-app/character-card-skills`。
   性向落 categories，其余三个落 tags，四元组还要贯穿进六段文案。
2. **性格写成因果链**（trait → why → observable behavior）——`davidgibbons/sillytavern-skills`。
   外加「留一条没解决的张力」「背景记忆只锚一个具体场景」。
3. **卡面文案七病灶配额制**——同一份 foreverse skill 的 §2.6：引语点题 ≤1、
   对仗翻转 ≤1、生造概念词 0、金句癖、伪精确数字 ≤3、「半 X」≤2、闲笔 ≥1。

两处照我们的场景改了口径：**「补充规则」不受七条约束**（它本来就是给模型的
永久规格，写成散文反而学不准）；**洁度用户不提就不写**（那是他的偏好，不替他拍板）。

## 一次调用出全部字段

六项之间有语气一致性要求（简介说温柔、说话风格说毒舌，人设就塌了）。拆成多次
调用等于让模型几遍各自发挥，边界对不上；一次出、代码只管拆字段，成本也更低
（2026-10-19 拍板，同 persona.py「一轮一次调用」的既有取舍）。

## 代码只做检测

`check()` 只算事实、出清单，**不拦、不重写**：分类造词、段落超长、变量写错，
都原样留着那一段并报出来（用户自己看着改）。只有「模型返回空 / JSON 解析失败」
这种它没交作业的情况才抛错重跑——词表盖不全开放集合，做成闸门只会把对的也丢掉。
唯一的例外是分类：那是 App 里勾选的封闭选项，造出来的词粘不进去，所以过滤掉，
并把原词回给用户看（`_raw_categories`）。
"""

import json
import logging
import os
import re
import time

from app import llm
from app.config import IMAGE_DEFAULT_SKILL, state_path

log = logging.getLogger("card")

# 星陨AI 创建页的分类选项（28 个，App 里最多勾 5 个）。这是封闭集合，
# 模型必须原样抄，不能造词。
CATEGORIES = (
    "女卡", "男卡", "女同", "世界", "非人", "综合", "男同", "女性向",
    "男性向", "全性向", "人外", "玄幻", "模拟器", "BG", "BL", "GL", "GB",
    "系统", "原创", "年上", "年下", "ABO", "纯爱", "剧情", "DADDY",
    "crush", "社团", "暧昧拉扯",
)

MAX_CATEGORIES = 5
GENDERS = ("女", "男")

# 五个文本框：key / 界面上叫什么 / 它的输入框提示 / 建议字数
# 建议字数只用来提示「这段偏短/偏长」，不当硬限制（App 那边才是权威）。
# 下限按 2026-10-19 实测调：deepseek-flash 写的开场白在 65~80 字，那个长度
# 恰好是一句动作 + 一句台词，够用；卡到 70 会天天报「偏短」。
#
# 第六项「角色名称」不在表里——它单独一栏（`NAME_MIN`/`NAME_MAX`），
# 和这五段一起由同一次调用出。
FIELDS = (
    ("greeting", "开场白", "角色自动发来的第一条消息：环境 + 动作神态 + 一句台词", (60, 200)),
    ("intro", "人物简介", "基本资料 + 身份背景 + 初始关系", (150, 380)),
    ("speech", "说话风格", "语气、句子长短、口头禅、怎么称呼你", (60, 180)),
    ("memory", "背景记忆", "一个主锚点写深 + 3~5 个短锚点（挂在具体物件/事件上）",
     (150, 380)),
    ("rules", "补充规则",
     "核心性格、关系节奏锁、双面锁、尺度锁、运行纪律（守口 + 称呼 + 不复读）",
     (200, 540)),
)

# 名称长度：教程要「简洁好记、一眼有辨识度」，但社区卡（酒馆里跑得住的那些）
# 清一色是**完整姓名**——江叙白 / 林知夏 / 林幺 / 沈砚。2 字是单姓单名，
# 3~4 字是常态（复姓 4 字到顶）。上限 12 只在「称号·姓名」时用得到。
NAME_MIN, NAME_MAX = 2, 12

# 「回复建议」给玩家看的玩法入口，**不占星陨的六个框**（表单就六栏），
# 单独存一份、单独输出，卡面保持干净。
ROUTES_MAX = 4
ROUTES_CHARS = (20, 50)

# 名称里出现这些基本不是人名：聊天前缀的冒号、@、纯数字/编号。只用来报警，
# 不动内容（代码只做检测，判断权留给人）。
_NAME_JUNK = re.compile(r"[：:@＠]|^[\d#＃]+$")

# 「小满」「阿白」「老张」——两个字、以亲昵前缀开头，几乎都是小名/昵称。
# 只是个结构信号，不拦（「小野」这种正经名字会误报，报警而已，用户自己判）。
_NAME_NICK = re.compile(r"^[小阿老].{0,1}$")

# 变量写法。星陨AI 的编辑器有「玩家名 / 角色名」快捷按钮，插进去是 {{user}} /
# 角色名对应的标记；这里按社区通行的 {{user}} / {{char}} 写，改这一个常量就能换。
VAR_USER = "{{user}}"
VAR_CHAR = "{{char}}"

_SYSTEM = """你是「角色卡写手」，给用户写一张能直接粘进星陨AI 创建表单的角色卡。
用户给你的往往只有一句话，甚至是从别处粘来的一段带绘图 tag 的草稿。

只输出一个 JSON 对象，不要代码块围栏、不要任何解释：
{
  "name": "角色名称",
  "gender": "女 或 男",
  "categories": ["从下面的选项里抄"],
  "tags": ["自由标签"],
  "greeting": "开场白",
  "intro": "人物简介",
  "speech": "说话风格",
  "memory": "背景记忆",
  "rules": "补充规则",
  "routes": ["玩法建议 1", "玩法建议 2"],
  "cover": "英文绘图标签",
  "note": "一句话说明你在用户没写清楚的地方补了什么"
}

## 六个模块（照星陨AI 官方教程写，各写各的，绝不能串）

- name / 角色名称：**正式姓名，不是小名**。中文角色一律「姓 + 名」，2~4 个字
  （复姓欧阳、司马这种算进去最多 4 个）。像「沈书言」「林知夏」「江叙白」「林幺」
  「沈砚」——酒馆上真正跑得住的卡，名字几乎都是这个形状。
  ⚠️ **不许用小名、乳名、昵称、外号、身份词当名字**：「小满」「阿白」「小七」
  「妹妹闺蜜」「学姐」都不行。也不许拿称号顶替姓名（「合欢宗师尊」不是名字）；
  真觉得称号有辨识度，写成「称号·姓名」，姓名必须在里面。
  ⚠️ 用户给了名字就用他给的；**他只给了小名或一个字时，把它当成「名」、补一个
  合适的姓凑成完整姓名**（用户说「叫小满」→「顾小满」），不要原样照抄「小满」。
  ⚠️ 名字要合世界观与年代：古风别取洋名，现代别取「慕容霸天」这种。
  ⚠️ 绝不能把聊天前缀、数字编号、@ 的名字、助手/机器人的昵称当角色名，
  不要 emoji、不要「：」，也不要一整句描述。
- greeting / 开场白：角色自动发给玩家的**第一条消息**，也就是整段故事的起点。
  交代环境、一个动作或神态，再抛一句台词，把玩家拉进场景。
  有画面感、但别一口气写太长——留一点空白，让玩家有东西可接。
  不要写成小作文，不要自我介绍身世，不要替玩家说话。
- intro / 人物简介：**他是谁**。按顺序写清三项——①基本资料：年龄、性别、
  外貌特征；②身份背景：职业、身世、当前所处的环境；③初始关系：你和角色
  第一次见面时是什么关系（站在什么位置、怎么称呼、谁先开的口）。
  性格要落到具体行为上（「被夸会先怼一句再偷偷看你的反应」），不要堆形容词。
  ⚠️ 这里只写「基础设定 + 初次见面」；**后来发生的事、你们的共同经历，
  全部放到背景记忆**。
- speech / 说话风格：**平时怎么说话**——语气（清冷 / 软糯 / 暴躁 / 斯文 /
  沙哑）、句子长短、口头禅与习惯性小动作、怎么称呼玩家（叫「小家伙」
  「主人」，还是直呼其名）。只写「怎么说」，不写「是什么人」——性格在简介里。
- memory / 背景记忆：**你们之间已经发生了什么**——往事、共同经历、情感羁绊，
  是角色「记得」的部分，开场后要在互动里体现出来。写成**一个主锚点 + 3~5 个
  短锚点**：
  主锚点写深一处——哪一年、在哪、发生了什么、他因此怎么看你，带一个感官细节
  或一句原话；短锚点各一句话，必须挂在**具体的物件或事件**上（备忘录、消防
  通道、被抹掉的名字、多做的便当、第一次改口叫的那个称呼）。物件型锚点比人格
  型锚点抗漂移，长对话里模型能反复把它捞回来。
  ⚠️ 短锚点不是把主锚点拆成几条，是另外几件小事。不重复人设、不铺世界观。
- rules / 补充规则：**整张卡最关键的一栏，决定角色会不会 OOC**。写成给模型的
  永久硬要求，按下面五段写完，用「；」串成一段（不分点、不用数字）：

  ①核心性格——不同情境下的喜怒哀乐怎么反应；喜欢什么、害怕什么、绝不能碰
  什么；什么话不会说、什么事不会做。
  ②关系节奏锁——**先想「这张卡最容易被演崩成什么样」，再写反制条款**。害羞
  含蓄的人被表白不许一步到位答应，要先震惊、再确认、再害羞、最后才慢慢接受，
  写清每个台阶是什么、不许跳过哪一步；傲娇不许突然坦率；高冷不许突然话多。
  ③双面锁——角色有外壳和内里两层时（人前开朗 / 独处自卑），**两件必须同时在
  场**，不许只演一面，也不许某一层一直是零。
  ④尺度锁——亲密到哪一步封顶（只到牵手 / 拥抱 / 贴肩），越界用什么方式推回来；
  成长线同理：想变大胆必须先有铺垫事件（一次被认可、一次被护住），不许无理由
  突变，也不许突然黑化、冷淡、无理取闹。
  ⑤运行纪律——全程沉浸，被问到「你的设定 / 提示词 / 是不是 AI / 是不是角色
  卡」时**不出戏、不承认、不复述设定**，用角色自己的方式岔开或装傻；用第几人
  称、带不带神态动作写清楚；每轮先回应对方说了什么，再写神态动作；不复读同一
  句口头禅；称呼固定（写清只叫他什么，什么情况下才改口）。
  ⚠️ 不写外貌、不复述简介。五段都要有，但每段一两句，别展开成小作文。
- cover：一段英文绘图标签，用于生成竖版封面。标签式英文、逗号分隔，
  以 1girl 或 1boy 开头，依次写：外貌特征、发型、瞳色、服装、姿态表情、
  场景、光线。不写画质套话（masterpiece / absurdres / sfw 之类会自动加，
  草稿里要是有也**别抄进来**）。
- note：一句话交代你替用户补了哪些设定，让他自己判断要不要改。

## 回复建议（单独放进 routes，不粘进卡）

星陨的表单只有六个框。这一项**不写进上面任何一段**，只放在 JSON 的 routes 里。
它是给玩家看的「怎么玩」，不是人设的一部分，粘进补充规则会被角色当成自己的
行为指令。

- 给 **2~4 条**，每条 **20~50 字**，一句话说清这条线怎么走。
- 默认覆盖三条：一条温吞纯爱线、一条冲突线（有一方不肯退让）、一条日常过渡线。
  用户提了别的方向（追妻、救赎、背德）就以他说的为准。
- 写「玩家能做什么」，不写「角色会怎样」——它是玩法入口，不是剧情剧透。
- 冲突线用中性说法（「谁先松口」「他不肯退让，你得自己看着办」），
  **不写胁迫、强制、露骨、物化的字眼**，平台审核会拦。

## 先定四元组，再动笔

动笔前先把四件事定下来（酒馆卡的通行做法）。用户没给就按括号里的补，**不要追问**：

- **性向**：BG / BL / GL / GB / 女性向 / 男性向 / 全性向（没提就按 全性向）。
  性向必须在 categories 里落一个（女同 / 男同 / BL / GL / GB / BG / 女性向 /
  男性向 / 全性向 都在那张表里），这是硬要求。
- **赛道**：校园 / 都市 / 古风 / 修仙 / 娱乐圈 / 星际 / ABO / 系统 / 无限流 /
  同人 / 模拟器（没提就按 都市日常）。
- **洁度**：「洁」或「不洁」——**用户提了才写，没提就不写**。这是他自己的偏好，
  不要替他拍板。
- **情绪钩子**：1~3 个，决定这张卡的爽点在哪——难攻略 / 救赎 / 酸涩 / 冷脸萌 /
  反差 / 追妻火葬场 / 背德 / 毒舌 / 年上 / 青梅竹马 / 万人迷。

赛道、洁度、情绪钩子**一律写进 tags**，不要往 categories 里塞（那里是 28 个
封闭选项，勾不上就粘不进去）。四元组还要贯穿进六段文案，不能只在 tags 挂着。

## 性格写成因果链，不要形容词表

「温柔、毒舌、有点自卑」这种清单，模型照着演只会演出一个平均值。每一项性格
都要能回答三件事——**是什么 → 为什么 → 看得见的行为**：

    童年从树上摔下来磕掉半颗牙（成因）
    → 一紧张就反复舔那个缺口（看得见）
    → 所以被夸的时候先怼一句，再偷偷看你怎么反应（看得见）

另外三条：

- 留**一条没解决的张力**：他信你，但一问起身世就岔开话题。全写圆满了，对话就
  没有可探索的地方，模型每轮只能重复已经说完的设定。
- 背景记忆只锚**一个具体场景**，别铺整条时间线——哪一年、在哪、发生了什么、
  他因此怎么看你。带一个感官细节或一句原话，比铺一段身世管用。
- 同一条成因**在不止一段里露面**（简介和背景记忆都可以引到），只写一次会在
  长对话里被模型丢掉。

## 文案纪律：酒馆卡的七条，超了就是 AI 味

这七条管**卡面上给人看的文字**（开场白、人物简介、说话风格、背景记忆），
不管台词——毒舌人设骂人就该扎人。记不住就记一句：**均匀的机灵 = AI**。
人填卡大部分是平铺信息，偶尔才俏皮一次；每段都有金句，一读就是生成的。

1. **引语点题 ≤1 处**：「他说那是『学费』」这种把设定包装成俏皮引语盖章的写法，
   全卡最多留一处，其余改平铺（「大二摔栏摔的，缝了四针」）。
2. **对仗翻转 ≤1 处**：「不带 X，全是 Y」「不是 X，是 Y」「唯一的 X 是 Y」——
   「唯一的」出现第二次就算超。改法：「骂人不带脏字，全是事实句」→
   「嘴毒，但说的都是实话，所以更气人」。
3. **生造概念词 0 处**：「事实句」「信任度反向指标」「保护色」这类抽象概括是
   机制词，不许出现在可见文案里；用大白话（「防备心很重」）。
4. **金句癖**：允许段落平收，甚至烂尾；不要段段落点；三连排比一张卡最多一次；
   「意味深长的空镜」（记分牌的灯、欲言又止的省略号、风吹过跑道）全删。
5. **伪精确数字 ≤3 处**：10秒98 / 0.05秒 / 18:40 / 第9天 这种是「具体性表演」，
   留下的数字要口语（「10秒9几」「还有一个多月」）。「半 X」量词（半秒 / 半步 /
   半拍 / 半寸）同属此病，全卡 ≤2 处，多了改「顿了一下」「退开」。
6. **说明书腔不外漏**：「疼到极限会有半声吸气」这种是规格，只能写进补充规则，
   不许漏进简介和开场白。
7. **闲笔 ≥1 处**：全卡至少留一个不服务任何设定的白长细节（「要走顺便帮我把
   灯关了」）。每个细节都是功能件的卡，信息压强太均匀，一读就是生成的。

**唯一例外是「补充规则」**：它本身就是给模型的永久规格，那一段就该写成条规
（「不主动结束对话」「被碰到脖子会先退开半步」），不受上面七条约束——把规格
藏进散文里，模型反而学不准。

写完全卡自查一遍：把开场白和简介**读出声**，读到某句心里冒出「这句真妙」，
十有八九该改平。

## 用户给的常常是一段脏草稿

- 开头的「233：」「某某：」是聊天昵称前缀，后面还可能跟着在喊助手的名字
  （比如「大大怪，」）、渠道名、「造卡」两个字——**这些都不是角色名，
  也不是设定**，直接忽略。
- 草稿里夹着的「text」「设定：」「masterpiece, best quality, absurdres, sfw」
  是绘图 tag：只用来判断外貌（发色、发型、瞳色、服装），人设仍以中文描述为准。
- 同一段里列了好几个角色时，**只写第一个**，别把人揉成一个。

## 分类只能从这里选（1~5 个，原样抄，不许造词）

女卡 男卡 女同 世界 非人 综合 男同 女性向 男性向 全性向 人外 玄幻 模拟器
BG BL GL GB 系统 原创 年上 年下 ABO 纯爱 剧情 DADDY crush 社团 暧昧拉扯

赛道词（校园 / 都市 / 娱乐圈 / 修仙 / 星际 这些）**不在这张表里**，写进 tags，
不要往 categories 里塞——App 里勾不上，粘的时候会被卡住。

## 硬规则

1. 用户的描述可能只有一句话，缺的你来补，但补的东西必须和已有信息自洽，
   不要凭空加够不着的设定（一句话描述就别硬塞前世今生）。
2. 设定用第三人称写，台词用第一人称。指代**屏幕对面那个人**（博士、你、对方）
   一律用 %s，指代**角色自己**用 %s，这两个千万别弄反——写「%s 表白时她
   先愣住」是对的，写成「%s 表白时她……」就把人称搞反了。不要出现「用户」
   「玩家」「作为一个 AI」「我是角色」这类词。
3. 不要在开场白里替用户说话、不要编用户的台词或名字。
4. 全中文输出（name、categories、cover 除外）。不要用 markdown，不要分点符号，
   写成连贯的自然段；rules 里要分点时用「；」串起来，别用「-」或数字。
5. 教程的提醒同样适用：**不要堆砌文字**，每一项精简、把重点写清楚即可。
6. 不写超出平台审核范围的露骨内容。
""" % (VAR_USER, VAR_CHAR, VAR_USER, VAR_CHAR)

_USER_TMPL = """角色描述：
%s

请按 JSON 输出这张角色卡。"""


def _parse(text):
    """从模型回复里抠出 JSON。解析不出来就抛错——不猜、不兜底。"""
    t = (text or "").strip()
    if not t:
        raise ValueError("模型返回了空内容，重跑一次试试")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        data = json.loads(t)
    except ValueError as e:
        raise ValueError("模型没按 JSON 返回（%s），重跑一次试试" % e)
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是一个 JSON 对象，重跑一次试试")
    return data


def _txt(v):
    return str(v or "").strip()


def _normalize(data):
    """把宽松的模型输出收敛成固定结构。只整形，不改写内容。"""
    raw_cats = data.get("categories")
    if isinstance(raw_cats, str):
        raw_cats = re.split(r"[,，、/|]+", raw_cats)
    if not isinstance(raw_cats, list):
        raw_cats = []
    raw_cats = [_txt(c) for c in raw_cats if _txt(c)]

    tags = data.get("tags")
    if isinstance(tags, str):
        tags = re.split(r"[,，、/|]+", tags)
    if not isinstance(tags, list):
        tags = []

    routes = data.get("routes")
    if isinstance(routes, str):
        routes = [r for r in re.split(r"[\n]+", routes)]
    if not isinstance(routes, list):
        routes = []

    card = {
        "name": _txt(data.get("name"))[:20],
        "gender": _txt(data.get("gender")),
        "categories": [c for c in raw_cats if c in CATEGORIES][:MAX_CATEGORIES],
        "tags": [_txt(t) for t in tags if _txt(t)][:8],
        "routes": [_txt(r) for r in routes if _txt(r)][:ROUTES_MAX],
        "cover": _txt(data.get("cover")),
        "note": _txt(data.get("note")),
        # 原样留着，check() 拿它报「这几个不在选项里」
        "_raw_categories": raw_cats,
    }
    for key, _label, _hint, _rng in FIELDS:
        card[key] = _txt(data.get(key))
    return card


def draft(brief, provider=None, model=None, timeout=150):
    """一句话描述 → 一张卡。抛错只抛「模型没交作业」那两种（空 / 不是 JSON）。"""
    brief = _txt(brief)
    if not brief:
        raise ValueError("先写一句角色描述")
    msgs = [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER_TMPL % brief}]
    text = llm.call_llm(msgs, timeout=timeout, provider=provider, model=model)
    card = _normalize(_parse(text))
    log.info("角色卡已生成：%s（分类 %s，%d 字）", card["name"],
             "/".join(card["categories"]) or "-",
             sum(len(card[k]) for k, _l, _h, _r in FIELDS))
    return card


# ─── AI 味检测（借酒馆卡的「七病灶配额制」）──────────────
#
# 来源是 foreverse-app/character-card-skills 的卡面文案句式纪律。七条里只有
# 下面几项能算成数；生造概念词、金句癖、闲笔配额是开放集合，词表盖不全，
# **故意不检测**——按老规矩：代码只算事实，判断权留给人。
#
# 全部只**报**、不改文案：超配额不等于要重写，也许人设就是要这么说话。
_HALF_WORD = re.compile(r"半[秒步拍寸声句分]")        # 「半 X」量词模具
_FIGURE = re.compile(                                 # 伪精确数字
    r"\d+(?:\.\d+)?\s*(?:秒|分钟|米|公斤)"
    r"|\d{1,2}\s*[:：]\s*\d{2}"
    r"|第\s*\d+\s*[天年次回]")
_ANTITHESIS = re.compile(                             # 对仗翻转
    r"不是[^，。；]{1,12}[，,]\s*是[^，。；]{1,12}"
    r"|不带[^，。；]{1,12}[，,]\s*全是[^，。；]{1,12}")
_ONLY_ONE = "唯一的"
# flavor-lexicon.json 里在中文卡上最常撞的几个裸词 + 解说腔句式。
# 这里是**抽样**不是全集，命中只说明「有嫌疑」，没命中不代表干净。
_FLAVOR_WORDS = ("一丝", "不易察觉", "眼中闪过", "嘴角勾起", "嘴角上扬",
                 "指节泛白", "喉结滚动")
_EXPLAIN = re.compile(r"(语气|眼神|声音|目光)[^。，]{0,8}(像|如同|仿佛|好似)"
                      r"|带着(几分|一丝|些许|一种)")

# 分类表里属于「性向」的那些。酒馆的规矩是性向缺失 = 不合格卡，
# 星陨AI 的分类本来就混着性别与性向，缺了不好被搜到，提醒一句。
ORIENTATION = ("BG", "BL", "GL", "GB", "女同", "男同", "女性向", "男性向", "全性向")


def _flavor(card):
    """算 AI 味的几项硬指标，返回给人看的句子（可能为空）。"""
    text = "".join(_txt(card.get(k)) for k, _l, _h, _r in FIELDS)
    if not text:
        return []
    out = []

    figs = _FIGURE.findall(text)
    if len(figs) > 3:
        out.append("具体数字用了 %d 处（建议 ≤3，多了像在表演精确）：%s"
                   % (len(figs), "、".join(figs[:5])))

    half = _HALF_WORD.findall(text)
    if len(half) > 2:
        out.append("「半 X」量词 %d 处（建议 ≤2，多了改「顿了一下」「退开」）：%s"
                   % (len(half), "、".join(sorted(set(half)))))

    anti = _ANTITHESIS.findall(text)
    if len(anti) > 1:
        out.append("对仗翻转 %d 处（「不是 X，是 Y」这类建议 ≤1）：%s"
                   % (len(anti), "；".join(anti[:2])))

    n_one = text.count(_ONLY_ONE)
    if n_one > 1:
        out.append("「唯一的」出现 %d 次（第二次起就是 AI 腔）" % n_one)

    hits = [w for w in _FLAVOR_WORDS if w in text]
    if hits:
        out.append("撞上常见 AI 腔用词：%s（能改平就改平）" % "、".join(hits))

    n_ex = len(_EXPLAIN.findall(text))
    if n_ex > 1:
        out.append("解说腔句式 %d 处（「语气像…」「带着几分…」）" % n_ex)
    return out


def check(card):
    """只检测、不改内容，返回给人看的问题清单（空列表 = 没发现问题）。"""
    out = []
    card = card or {}

    name = _txt(card.get("name"))
    if not name:
        out.append("角色名称是空的（这一项 App 里必填）")
    elif len(name) > NAME_MAX:
        out.append("角色名称 %d 字，偏长，建议 %d~4 字" % (len(name), NAME_MIN))
    elif _NAME_JUNK.search(name):
        # 不替它改，只提醒：用户打招呼的「大大怪：」被当成名字踩过一次
        out.append("角色名称「%s」看着像聊天前缀或编号，不像是名字，确认一下" % name)
    elif len(name) < NAME_MIN:
        out.append("角色名称只有 %d 个字，建议用完整姓名（姓+名）" % len(name))
    elif _NAME_NICK.match(name):
        out.append("角色名称「%s」像是小名/昵称，建议换成完整姓名（姓+名，如 沈书言）"
                   % name)

    g = _txt(card.get("gender"))
    if g and g not in GENDERS:
        out.append("性别写成了「%s」，App 里只能选女 / 男" % g)

    raw = card.get("_raw_categories") or []
    cats = card.get("categories") or []
    bad = [c for c in raw if c not in CATEGORIES]
    if bad:
        out.append("这些分类不在可选项里，已去掉：%s" % "、".join(bad))
    if len([c for c in raw if c in CATEGORIES]) > MAX_CATEGORIES:
        out.append("分类超过 %d 个，只保留了前 %d 个" % (MAX_CATEGORIES, MAX_CATEGORIES))
    if not cats:
        out.append("一个分类都没选上，App 里不勾分类可能过不了审核")
    elif not any(c in ORIENTATION for c in cats):
        # 酒馆那边「性向缺失 = 不合格卡」，这边只是不好被搜到，提醒一句
        out.append("分类里没有性向（BG / BL / GL / 全性向…），建议补一个")

    for key, label, _hint, (lo, hi) in FIELDS:
        body = _txt(card.get(key))
        n = len(body)
        if not n:
            out.append("「%s」是空的" % label)
        elif n < lo:
            out.append("「%s」只有 %d 字，偏短（建议 %d~%d），模型可能没展开"
                       % (label, n, lo, hi))
        elif n > hi:
            out.append("「%s」%d 字，偏长（建议 %d~%d），贴进去可能会被截断"
                       % (label, n, lo, hi))

    # 锚点：主锚点写深了会占两三句，再挂 3~5 个短锚点，总句数不会少。
    # 只数句子结构、不判内容好不好——少到这个份上基本都是只写了一个场景。
    mem = _txt(card.get("memory"))
    if mem:
        segs = [s for s in re.split(r"[。；;！!？?\n]", mem) if s.strip()]
        if len(segs) < 3:
            out.append("背景记忆只有 %d 句，像是只写了一个场景；建议 1 个主锚点 "
                       "+ 3~5 个短锚点（挂在具体物件/事件上，长对话才捞得回来）"
                       % len(segs))

    routes = card.get("routes") or []
    if len(routes) < 2:
        out.append("玩法建议只有 %d 条，建议 2~4 条（给玩家几条能选的路）" % len(routes))
    lo_r, hi_r = ROUTES_CHARS
    for r in routes:
        n = len(r)
        if n < lo_r or n > hi_r:
            out.append("玩法建议「%s」%d 字，建议 %d~%d 字"
                       % (r[:10], n, lo_r, hi_r))

    joined = "".join(_txt(card.get(k)) for k, _l, _h, _r in FIELDS)
    # 先把**合法**变量剥掉再查，否则 `{{user}}` 会命中 `{user}` 这条规则
    probe = joined.replace(VAR_USER, "").replace(VAR_CHAR, "")
    if "玩家" in probe or "用户" in probe:
        out.append("正文里出现了「玩家 / 用户」，App 里应该用 %s 指代" % VAR_USER)
    for wrong in ("{user}", "{{User}}", "{{char_name}}", "<user>", "［玩家］"):
        if wrong in probe:
            out.append("变量写法「%s」可能不对，统一用 %s / %s"
                       % (wrong, VAR_USER, VAR_CHAR))

    out.extend(_flavor(card))

    if not _txt(card.get("cover")):
        out.append("没有封面图标签，出不了图")
    return out


def plain(card):
    """导出成纯文本（整张卡粘一个文件里存档 / 发人看）。"""
    c = card or {}
    lines = ["# %s" % (_txt(c.get("name")) or "未命名"),
             "性别：%s" % (_txt(c.get("gender")) or "-"),
             "分类：%s" % ("、".join(c.get("categories") or []) or "-"),
             "标签：%s" % ("、".join(c.get("tags") or []) or "-"), ""]
    for key, label, _hint, _rng in FIELDS:
        lines += ["## %s" % label, _txt(c.get(key)), ""]
    routes = c.get("routes") or []
    if routes:
        # 不粘进卡：星陨只有六个框，这一栏是给玩家挑玩法用的
        lines += ["## 玩法建议（不用粘进卡）"]
        lines += ["%d. %s" % (i, _txt(r)) for i, r in enumerate(routes, 1)]
        lines += [""]
    if _txt(c.get("cover")):
        lines += ["## 封面标签", _txt(c.get("cover")), ""]
    if _txt(c.get("note")):
        lines += ["## 补了什么", _txt(c.get("note"))]
    return "\n".join(lines).strip() + "\n"


# ─── 封面图（2026-10-19，2026-10-20 改回默认分辨率）──────
#
# 10-19 曾按星陨AI 推荐把封面钉成 864×1536（9:16）+ latent 放大 1.0 + 跳过 2x，
# 顺带把步数压到 10+8。10-20 用户实测后否掉：864×1536 只有 1.3 MP，底模在
# 9:16 这个非原生画幅上构图发糊，**还不如渠道默认的 1024×1536 → ×1.2 → 2x
# ≈ 2464×3680（9 MP）**。
# 现在封面**完全走渠道默认参数**：不钉尺寸、不跳放大、不压步数。
COVER_SKILL = IMAGE_DEFAULT_SKILL


def cover_opts(skill=COVER_SKILL):
    """封面用的渲染参数 = 渠道默认（尺寸 / 放大 / 步数一个都不覆盖）。

    钉死尺寸那套（864×1536）已废弃：画幅压到默认的 1/7，字和脸一起糊。
    """
    from app import comic_render_opts
    return comic_render_opts.defaults(skill)


def cover_prompt(card):
    """封面提示词 = 模型写的那段英文 tag。不拼质量前缀（工作流自带 base_prompt）。"""
    t = _txt((card or {}).get("cover"))
    if not t:
        raise ValueError("这张卡没有封面标签，出不了图")
    return t


def cover_workflow(skill, prompt, seed):
    """封面工作流 = 这个渠道**原封不动**的单图工作流。

    batch 族（sd / miao / nsfw_pose_gen）的第二段尺寸、`image_gen_v1` 节点内的
    2x，都随渠道自己的比例走——不再钉回某个固定尺寸（那是 864×1536 时代的补丁）。
    """
    from app.tools.normal import generate_image
    return generate_image.build_t2i_workflow(skill, prompt, seed)


def render_cover(cid, card, skill=COVER_SKILL, seed=None):
    """出一张封面，返回落盘路径。**阻塞**（2~4 分钟，看渠道），调用方自己放后台线程。

    这里不更新卡片记录——出图是慢操作，状态由接口层管（先写 rendering、
    成功后写 cover），免得线程和请求两边抢着写同一个 json。
    """
    from app import comic      # 延迟导入：纯文案路径不该拖上生图依赖

    comic._require_comfy()
    prompt = cover_prompt(card)
    seed = seed or comic._new_seed()
    wf = cover_workflow(skill, prompt, seed)
    name = comic._submit_retry(
        lambda: comic._submit(wf, skill, prompt, seed,
                              out_dir="web/角色卡_%s" % cid),
        "[角色卡封面]")
    dst = os.path.join(card_dir(cid), "cover.png")
    comic._download(name, dst)
    log.info("角色卡封面已出：%s（%s，seed=%s）", dst, skill, seed)
    return dst


# ─── 存储 ─────────────────────────────────────────────
#
# 卡库放 state/ 下（已 gitignore），封面图也在这里：`state_path()` 在测试进程里
# 会自动导向临时目录，两份数据都跟着隔离，不会写到真实卡库上。

CARDS_FILE = "xingyun_cards.json"


def cards_path():
    return state_path(CARDS_FILE)


def _cards_root():
    return os.path.join(os.path.dirname(cards_path()), "xingyun_cards")


def card_dir(cid):
    return os.path.join(_cards_root(), _txt(cid))


def cover_path(cid):
    return os.path.join(card_dir(cid), "cover.png")


def load_cards():
    """卡库（按创建时间倒序）。读坏当空，不炸。"""
    fp = cards_path()
    if not os.path.isfile(fp):
        return []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("角色卡库读不出来（%s），当作空的", e)
        return []
    return data if isinstance(data, list) else []


def _write_cards(items):
    fp = cards_path()
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def get_card(cid):
    cid = _txt(cid)
    for c in load_cards():
        if c.get("id") == cid:
            return c
    return None


def _new_id():
    base = "K" + time.strftime("%Y%m%d-%H%M%S")
    ids = {c.get("id") for c in load_cards()}
    if base not in ids:
        return base
    n = 2
    while "%s-%d" % (base, n) in ids:
        n += 1
    return "%s-%d" % (base, n)


def save_card(card, cid=""):
    """存一张卡（cid 空 = 新建）。返回存好的记录。"""
    items = load_cards()
    cid = _txt(cid) or _new_id()
    rec = dict(card or {})
    rec["id"] = cid
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for i, old in enumerate(items):
        if old.get("id") == cid:
            rec.setdefault("created", old.get("created") or now)
            rec["updated"] = now
            rec.setdefault("cover", old.get("cover") or "")
            items[i] = rec
            break
    else:
        rec["created"] = rec["updated"] = now
        rec.setdefault("cover", "")
        items.append(rec)
    _write_cards(items)
    return rec


def update_card(cid, patch):
    """改几个字段（封面状态那种），找不到就返回 None。"""
    items = load_cards()
    for i, old in enumerate(items):
        if old.get("id") != _txt(cid):
            continue
        old.update(patch or {})
        old["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        items[i] = old
        _write_cards(items)
        return old
    return None


def delete_card(cid):
    items = load_cards()
    left = [c for c in items if c.get("id") != _txt(cid)]
    if len(left) == len(items):
        return False
    _write_cards(left)
    return True

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

## 名字是独立一栏

早期版本从「造卡」两个字后面整句取需求，用户打招呼的「大大怪，造卡 …」
会把机器人的昵称一起带进去，模型就把它当成了角色名（2026-10-19 实测）。
现在需求只取「造卡」**后面**那段（见 `card_bot.parse`），提示词里也再挡一道。

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
from app.config import state_path

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
    ("memory", "背景记忆", "你们之间已经发生过的往事、共同经历、情感羁绊", (100, 260)),
    ("rules", "补充规则", "核心性格、喜好厌恶、行为边界、剧情约束、叙事视角", (120, 340)),
)

# 名称长度：教程要「简洁好记、一眼有辨识度」，示例是 4~8 字（合欢宗师尊 /
# 白猫妖灵·阿白 / 星际赏金猎人·凯）。2 字以下是单字名，容得下。
NAME_MIN, NAME_MAX = 2, 12

# 名称里出现这些基本不是人名：聊天前缀的冒号、@、纯数字/编号。只用来报警，
# 不动内容（代码只做检测，判断权留给人）。
_NAME_JUNK = re.compile(r"[：:@＠]|^[\d#＃]+$")

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
  "cover": "英文绘图标签",
  "note": "一句话说明你在用户没写清楚的地方补了什么"
}

## 六个模块（照星陨AI 官方教程写，各写各的，绝不能串）

- name / 角色名称：它既是作品名，也是这个角色的标识。**2~6 个字，简洁好记、
  一眼有辨识度**。可以是单名、称号，或「称号·名字」——像「合欢宗师尊」
  「白猫妖灵·阿白」「星际赏金猎人·凯」。
  ⚠️ 需求里给了名字就用他给的那个；没给就照着人设自己起一个，**别把身份词
  当名字**（「妹妹闺蜜」是身份，「林知夏」才是名字）。
  ⚠️ **绝不能把聊天前缀、数字编号、@ 的名字、助手/机器人的昵称当角色名**，
  不要 emoji、不要「：」，也不要一句描述。
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
  是角色「记得」的部分，开场后要在互动里体现出来。写到具体的事（哪一年、
  在哪、发生了什么、他因此怎么看你），不重复人设、不铺世界观。
- rules / 补充规则：**整张卡最关键的一栏，决定角色会不会 OOC**。写成给模型的
  永久硬要求，覆盖这几项：①核心性格——不同情境下的喜怒哀乐怎么反应；
  ②喜好与厌恶——喜欢什么、害怕什么、绝不能碰什么；③行为边界——什么话不会说、
  什么事不会做；④剧情约束——不跳出人设、不替玩家说话或做决定、不主动结束
  对话；⑤叙事视角与描写习惯——第几人称、要不要带神态动作。
  ⚠️ 不写外貌、不复述简介。
- cover：一段英文绘图标签，用于生成竖版封面。标签式英文、逗号分隔，
  以 1girl 或 1boy 开头，依次写：外貌特征、发型、瞳色、服装、姿态表情、
  场景、光线。不写画质套话（masterpiece / absurdres / sfw 之类会自动加，
  草稿里要是有也**别抄进来**）。
- note：一句话交代你替用户补了哪些设定，让他自己判断要不要改。

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

## 硬规则

1. 用户的描述可能只有一句话，缺的你来补，但补的东西必须和已有信息自洽，
   不要凭空加够不着的设定（一句话描述就别硬塞前世今生）。
2. 设定用第三人称写，台词用第一人称。不要出现「作为一个 AI」「我是角色」这类
   元话语，也不要出现「用户」「玩家」这种词——统一写 %s（用户）和 %s（角色）。
3. 不要在开场白里替用户说话、不要编用户的台词或名字。
4. 全中文输出（name、categories、cover 除外）。不要用 markdown，不要分点符号，
   写成连贯的自然段；rules 里要分点时用「；」串起来，别用「-」或数字。
5. 教程的提醒同样适用：**不要堆砌文字**，每一项精简、把重点写清楚即可。
6. 不写超出平台审核范围的露骨内容。
""" % (VAR_USER, VAR_CHAR)

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

    card = {
        "name": _txt(data.get("name"))[:20],
        "gender": _txt(data.get("gender")),
        "categories": [c for c in raw_cats if c in CATEGORIES][:MAX_CATEGORIES],
        "tags": [_txt(t) for t in tags if _txt(t)][:8],
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


def check(card):
    """只检测、不改内容，返回给人看的问题清单（空列表 = 没发现问题）。"""
    out = []
    card = card or {}

    name = _txt(card.get("name"))
    if not name:
        out.append("角色名称是空的（这一项 App 里必填）")
    elif len(name) > NAME_MAX:
        out.append("角色名称 %d 字，偏长，建议 %d~6 字" % (len(name), NAME_MIN))
    elif _NAME_JUNK.search(name):
        # 不替它改，只提醒：用户打招呼的「大大怪：」被当成名字踩过一次
        out.append("角色名称「%s」看着像聊天前缀或编号，不像是名字，确认一下" % name)

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

    joined = "".join(_txt(card.get(k)) for k, _l, _h, _r in FIELDS)
    # 先把**合法**变量剥掉再查，否则 `{{user}}` 会命中 `{user}` 这条规则
    probe = joined.replace(VAR_USER, "").replace(VAR_CHAR, "")
    if "玩家" in probe or "用户" in probe:
        out.append("正文里出现了「玩家 / 用户」，App 里应该用 %s 指代" % VAR_USER)
    for wrong in ("{user}", "{{User}}", "{{char_name}}", "<user>", "［玩家］"):
        if wrong in probe:
            out.append("变量写法「%s」可能不对，统一用 %s / %s"
                       % (wrong, VAR_USER, VAR_CHAR))

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
    if _txt(c.get("cover")):
        lines += ["## 封面标签", _txt(c.get("cover")), ""]
    if _txt(c.get("note")):
        lines += ["## 补了什么", _txt(c.get("note"))]
    return "\n".join(lines).strip() + "\n"


# ─── 封面图（2026-10-19）──────────────────────────────
#
# 864×1536 是星陨AI 推荐尺寸（9:16）。不去改工作流模板，走现成的覆盖机制：
# latent 放大调成 1.0、跳过最终像素放大，出图就是**原尺寸**——silver / anima
# 默认是 1024×1536 → ×1.2 → 2x = 2458×3686，直接拿来当封面会被裁掉大半。
COVER_W, COVER_H = 864, 1536
COVER_SKILL = "silver"

# 封面刻意降步数（2026-10-19 实测）：silver 模板是 20+20 步，同一条提示词同
# 一个 seed 跑 864×1536 要 261 秒，改 10+8 步只要 24 秒，肉眼看画质几乎一样
# （silver 底模是 silvermoonmixAnimaEvolved_v2329BTurbo，Turbo 本来就不吃满
# 20 步）。用户等 4 分钟一定会以为页面卡死了。
COVER_BASE_STEPS = 10
COVER_HIRES_STEPS = 8
COVER_HIRES_DENOISE = 0.3


def cover_opts(skill=COVER_SKILL):
    """封面用的渲染参数：正好 COVER_W×COVER_H，不走任何放大，步数压到够用。

    步数只**下调**：切到本来就少于 10 步的渠道（moss 4 步那种）时不会反被拉高。
    """
    from app import comic_render_opts
    opts = comic_render_opts.defaults(skill)
    opts["width"], opts["height"] = COVER_W, COVER_H
    up = dict(opts.get("upscale") or {})
    up["scale_by"] = 1.0        # LatentUpscaleBy ×1.0 = 尺寸不变
    up["final_skip"] = True     # SaveImage 直连解码，跳过 2x 放大
    opts["upscale"] = up
    for key, cap in (("base", COVER_BASE_STEPS), ("hires", COVER_HIRES_STEPS)):
        p = dict(opts.get(key) or {})
        if not p:
            continue
        cur = p.get("steps")
        if not isinstance(cur, int) or cur > cap:
            p["steps"] = cap
        opts[key] = p
    hi = opts.get("hires") or {}
    if hi:
        d = hi.get("denoise")
        if not isinstance(d, (int, float)) or d > COVER_HIRES_DENOISE:
            hi["denoise"] = COVER_HIRES_DENOISE
    return opts


def cover_prompt(card):
    """封面提示词 = 模型写的那段英文 tag。不拼质量前缀（工作流自带 base_prompt）。"""
    t = _txt((card or {}).get("cover"))
    if not t:
        raise ValueError("这张卡没有封面标签，出不了图")
    return t


def cover_workflow(skill, prompt, seed):
    """封面工作流：出图**正好** COVER_W×COVER_H，不走任何放大。

    `cover_opts()` 只是把倍率/步数写进参数对象，落到工作流上还差一步：batch 族
    （sd / miao / nsfw_pose_gen）的第二段尺寸是节点里写死的数值，`apply()` 会按
    底图比例**等比缩放**它（864×1536 底图 → 1296×2304 出图），得再钉一次。
    """
    from app import comic_render_opts
    from app.tools.normal import generate_image

    wf = comic_render_opts.apply(
        generate_image.build_t2i_workflow(skill, prompt, seed), cover_opts(skill))
    comic_render_opts.pin_output_size(wf, COVER_W, COVER_H)
    return wf


def render_cover(cid, card, skill=COVER_SKILL, seed=None):
    """出一张封面，返回落盘路径。**阻塞**（30~60 秒），调用方自己放后台线程。

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

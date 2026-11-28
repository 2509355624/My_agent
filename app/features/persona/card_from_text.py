# -*- coding: utf-8 -*-
"""从一段原文生成一张自建角色卡（2026-11-28 用户需求）。

用户原话：「有时候看到喜欢的内容，想自己搞一个同人创作」——所以这条路的
输入不是「几个字段」，而是一整段小说/同人短文（最多 2 万字），输出是一张
能直接开演、也能事后自己再改的卡。

口径（用户 2026-11-28 拍板，逐条对应）：

1. **原文上限 2 万字，超了自动截断，不报错**。截法是「头 14000 + 尾 6000 +
   中间一行省略标记」：开头常有登场和设定，结尾常有情绪高点，两头都值钱；
   中间插一句「原文中段已省略」，模型才知道那里不是情节断裂。
2. **「以谁为基准」必填**。原文里常常不止一个角色，不点名它就会抓错人。
3. **原文没提到的东西不要留空**，按她的身份、时代、关系合理补全——用户原话
   「AI 它自动根据原文去进行补充」。这条和常见的「不许编」相反，是有意的：
   一张卡缺了长相、房间，开演时模型会自己现编，不如让它在生成阶段就编好、
   用户看得见也能改。
4. **示例对话从原文摘，保留原话**。这不是「改写」：只做说话人规整（`她：…` /
   `你：…`）和剥掉旁白叙述，措辞一个字不动。原因见 `head_builder` 那条注入
   标题——示例对话是「只学语气和节奏」的示范，措辞原样才带得出作者的笔触。
5. **外观和服装必须是英文 danbooru tag**。原文是中文描写，进不了出图链；
   让模型在生成阶段转，比事后让用户手补便宜得多。

为什么单独一个模块：`routes.py` 已经 96 KB，「提示词 + 解析 + 裁剪 + 重名
重试」再塞进去就没法读了。这里只放纯逻辑（不 import flask），路由挂在
persona 的 bp 上。
"""

import logging
import re

log = logging.getLogger("persona.card_from_text")

# 原文上限与截断比例（见模块头第 1 条）
MAX_CHARS = 20000
_HEAD_CHARS = 14000
_TAIL_CHARS = 6000
_ELIDE_MARK = "\n\n【原文中段已省略，结尾可能不完整】\n\n"

# 单次生成的输出上限。字段目标长度合计约 5000 字（≈7500 token），留足余量；
# 服务端默认上限会把长 JSON 砍在半句上（同 `llm_bridge.REPLY_MAX_TOKENS` 的坑）。
MAX_OUT_TOKENS = 12000
TIMEOUT = 150

# 卡名/真名的字符上限（跟 `USER_FIELD_MAX` 对齐，这里只为「加序号」时不越界）
_NAME_MAX = 24


def truncate_text(text):
    """按 2 万字截断。→ (文本, 是否被截过)。"""
    t = str(text or "").strip()
    if len(t) <= MAX_CHARS:
        return t, False
    return t[:_HEAD_CHARS] + _ELIDE_MARK + t[-_TAIL_CHARS:], True


_PROMPT = r'''你是给角色扮演站写角色卡的编辑。用户给你一段原文（小说 / 同人短文 /
访谈都可能），你从中提取「%(who)s」这个角色，写成一张可以直接开演的角色卡。

只输出一个 JSON 对象，不要解释，不要代码块围栏。字段如下（**原文没提到的，
按她的身份、时代、和「你」的关系合理补全，不要留空串**）：

{
  "name": "卡名，名单上显示的，24 字内。原文没给明确称谓就用她的身份或关系（「同班同学」「书店老板」）",
  "charName": "她的真名，20 字内。原文没明说就按原文称呼推断，推不出来才留空",
  "description": "她是谁：身份、和「你」的关系、长相。800~1200 字",
  "personality": "性格。用她做过的事、遇事的反应来写，不堆形容词。600~900 字",
  "systemPrompt": "演出指令。1500~2200 字，写三件事：①她是谁、和「你」是什么关系；②她怎么说话——称呼、紧张或生气时的具体表现（最准的写法是举她真说过的一句）；③她的边界，什么会让她退、什么会让她靠近。**只写她这个人，不写写作技法**（句子长短、对白比例、节奏这类一个都不要），那会让角色变成机器。",
  "exampleDialogue": "从原文摘的对话，整理成「她：…」和「你：…」的对话体，2~5 段，累计 2000~3000 字。**措辞一个字不改**，只做两件事：把说话人标成「她：」「你：」，把旁白叙述剥掉（动作可以压成括号里的提示）。原文没有对话就留空。",
  "opening": "她说的第一句，200~400 字，第一人称，能直接开场（她在做什么、看见你时的第一反应）",
  "room": "她的房间，300~600 字。原文写过就照写，没写过按她的身份补一个（陈设要说清是哪几件，不要清单式罗列）",
  "appearancePrompt": "英文 danbooru 短标签，逗号分隔，描述长相（发色、瞳色、体型、年龄感、气质）。30~60 个标签，**不许写中文句子**",
  "outfitPrompt": "英文 danbooru 短标签，逗号分隔，描述她常穿的衣服。10~25 个标签，**不许写中文句子**"
}

三条硬规则：

1. 原文没写到的，按人物自身的逻辑补——她做什么工作、住什么地方、说话什么调子，
   都要和原文已经写出来的部分对得上。补出来的东西要能站住，不要编和她矛盾的经历。
2. 外观、服装必须是英文 tag。原文是中文描写，你要转成对应的 danbooru 词
   （`black hair` / `long hair` / `school uniform` 这种），中文进去出图链画不出来。
3. 示例对话里的每一句都必须是原文里真实出现过的，不许自己写。可以在两段之间
   留空行，但不要加「（本段结束）」这类标注。

以下是原文。原文可能被截断过（中段有省略标记），省略处不是情节断裂：

%(text)s
'''


def build_prompt(text, who):
    """组提示词。`text` 已经截断过。"""
    return _PROMPT % {"who": who, "text": text}


def _clean(raw, who):
    """把模型吐的 dict 收成一张卡的字段 dict（按白名单裁剪）。"""
    from app.features.persona import cards_lookup as _cl
    src = dict(raw or {})
    if not str(src.get("name") or "").strip():
        # 卡名是硬要求（`_name_taken_error` 会直接拒）；模型没给就用点名的角色名
        src["name"] = str(who or "").strip()[:_NAME_MAX]
    # 起点四条（好感 / 在一起 / 表白 / 病娇 / 冲动）**一律留空**（用户 2026-11-28
    # 拍板）：那是他开局时自己填的，AI 猜一个数值只会变成他要改的负担。
    # v1 的 start_level / start_arousal 更不该出现——新线建成 v2，那两个不参与判定。
    for k in ("start_value", "start_together", "start_confess",
              "start_yandere", "start_arousal_tier",
              "start_level", "start_arousal"):
        src.pop(k, None)
    card = _cl._clean_user_fields(src)             # noqa: SLF001 —— 私有但有意的复用
    # `_clean_user_fields` 会补一堆默认值（起点、pregnancy 等），这里原样留着：
    # 用户后面在编辑页改，缺省值就是表单初始值。
    return card


def _parse(reply):
    """从模型输出里抠 JSON。宽容：允许前后有解释、允许 ```json 围栏。"""
    from app.features.persona.json_utils import _parse_json_loose
    return _parse_json_loose(reply)


def generate(text, who, ask=None):
    """生成一张卡的字段 dict。→ (card, err, meta)

    ask 可注入（测试用）：签名 `ask(messages) -> 文本`。生产上走
    `llm_bridge.call`（角色扮演专用链，`PERSONA_PROVIDER` + 默认模型）。
    """
    who = str(who or "").strip()
    if not who:
        return None, "要填「以谁为基准」——原文里那个角色的名字", {}
    body, cut = truncate_text(text)
    if not body:
        return None, "没贴原文", {}
    if ask is None:
        from app.features.persona import llm_bridge

        def ask(messages):                          # noqa: F811 —— 生产实现
            return llm_bridge.call(messages, {},
                                   max_tokens=MAX_OUT_TOKENS, timeout=TIMEOUT)
    prompt = build_prompt(body, who)
    raw = None
    last_err = ""
    # 两次机会：长文 + 长 JSON 的坏格式概率不低（模块头第 4 条之外的另一条现实）。
    for attempt in range(2):
        try:
            out = ask([{"role": "user", "content": prompt}])
        except Exception as e:                      # noqa: BLE001
            last_err = str(e) or e.__class__.__name__
            log.warning("[card_from_text] 第 %d 次调用失败：%s", attempt + 1, last_err)
            continue
        raw = _parse(out)
        if isinstance(raw, dict) and raw:
            break
        last_err = "输出不是 JSON"
        log.warning("[card_from_text] 第 %d 次输出解析失败（前 120 字：%r）",
                    attempt + 1, str(out or "")[:120])
    if not isinstance(raw, dict) or not raw:
        return None, "这次没读懂原文（%s），换个原文或者只贴她出场的段落再试" % (last_err or "模型没交作业"), {}
    card = _clean(raw, who)
    if not str(card.get("name") or "").strip():
        return None, "没识别出角色名，把「以谁为基准」写得更具体一点", {}
    meta = {"truncated": cut, "chars": len(body)}
    return card, "", meta


def save(who, card):
    """落库。重名自动加序号（用户要「创建完成自动保存」，不能因重名白跑一次生成）。

    → (卡, err, 是否改过名)。卡数满了不重试（加序号也不解决），原样回错误。
    """
    from app.features.persona import service as persona
    base = dict(card or {})
    last_err = ""
    for i in range(0, 4):
        name = base.get("name") or ""
        if i:
            # 去掉尾部已有序号再叠，避免「林晚 2 2 2」
            root = re.sub(r"\s*\d+$", "", str(base.get("name") or "")).strip()
            base["name"] = ("%s %d" % (root, i + 1))[:_NAME_MAX]
        made, err = persona.make_user_card(who, base)
        if made:
            return made, "", bool(i)
        last_err = err or "建卡失败"
        if "撞了" not in last_err:
            break                                   # 卡满 / 名字为空 / 存不下来：重试没用
        base["name"] = name
    return None, last_err, False


# Markdown 预览：给前端折叠展开看「AI 到底写了什么」。
# 只做展示，落库用的是上面的字段 dict —— 让模型直接吐 Markdown 再按标题切分，
# 标题写法一飘整张卡就废了（用户 2026-11-28 同意内部走 JSON）。
_MD_FIELDS = [
    ("description", "她是谁"),
    ("personality", "性格"),
    ("systemPrompt", "演出指令"),
    ("room", "她的房间"),
    ("opening", "开场白"),
    ("exampleDialogue", "示例对话"),
    ("appearancePrompt", "长相 tag"),
    ("outfitPrompt", "服装 tag"),
]


def to_markdown(card):
    """把一张卡渲染成 Markdown 预览。空字段不出标题。"""
    c = card or {}
    name = str(c.get("name") or "").strip() or "（未命名）"
    cname = str(c.get("charName") or "").strip()
    head = "# %s" % name
    # 卡名和真名常常就是同一个（原文只有一个称呼），重复显示只是噪音
    if cname and cname != name:
        head += "（%s）" % cname
    lines = [head, ""]
    for key, label in _MD_FIELDS:
        body = str(c.get(key) or "").strip()
        if not body:
            continue
        lines += ["## " + label, "", body, ""]
    return "\n".join(lines).strip()

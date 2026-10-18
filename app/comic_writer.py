"""编剧层：一句话设定 → 可被 comic.py 直接解析的剧本（script.md）。

这是整条流水线里唯一「创作」的一环，但它交付的是**契约**不是散文——下游
`app/comic.py` 只认固定格式（`## NN` / `画面:` / `气泡:` / `标签:`）。所以
这里真正的工作不是「让模型写故事」，而是**把它的输出约束到契约上**：写完
用 `comic.parse_script_text` 真解析一遍，格数不对、画面混进中文、台词太长，
就带着错因回炉重写（最多 3 次）。

换角色、换题材都从这一层入手，下游一个字不用改：

    write_script("妹妹的好感度变化……", panels=3, character="imouto, ...")

命令行入口在 comic.py：

    python -m app.comic write "<设定>" --panels 3 --out comics/C20261007-04/script.md
    python -m app.comic new   "<设定>" --panels 3 --scene-skill nai
"""

import logging
import os
import re

from app import comic, llm

log = logging.getLogger("comic.writer")

# 气泡台词硬上限（字）。定 16 不是为了「塞得下」，而是为了**渲染可靠性**：实测
# 同样 16 字，有的格干干净净、有的格 qwen 会把某一行重复画一遍（重影），行长越
# 贴近上限越容易中招。收紧到 16，再配合气泡模板里「每行 ≤ 8 字」的排布要求，
# 把这个诱因掐掉。见 comic.BUBBLE_TEMPLATE。丰富度靠「气泡2」扩，不靠放宽单条。
MAX_BUBBLE = 16
MAX_LABEL = 24           # 一条旁白/标签的上限。定 24 不是随手取的数：旁白和气泡是
                         # **同一个字号**画在图上（见 comic._label_sec），而气泡的
                         # 预算是 16 字 = 2~3 行 × 每行 ≤8 字；24 = 3×8，正好是同一个
                         # 三行文字框能装下的上限。
                         # 2026-10-11 实测：卡 20 时模型两次都写 22 字（「我没再说话，
                         # 把她整个圈进怀里，揉了揉她的头。」），校验连挂两稿、脚本被判
                         # 不合格——是上限比模型的手感紧，不是模型不听话，所以放宽到 24。
MAX_LABELS = 3           # 一格的旁白最多几条（`标签1:` `标签2:` `标签3:`）。
                         # 用户 2026-10-11 拍板：一格要能讲好几段剧情——
                         # 「我带她回了家 / 安慰了她 / 她的手在袖子里攥紧了」。
                         # 卡 3 是因为它们都画在画面右上角，再多会糊成一片。

# 画面生成规范：**全文**喂给编剧（用户拍板：单次请求要让 AI 看到完整规范，
# 不做压缩合并进提示词）。每次现读——用户改这个文件不用重启进程。
_SPEC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comic_spec.md")


def _spec_text():
    with open(_SPEC_PATH, "r", encoding="utf-8") as f:
        return f.read()

# 契约说明 + 写作要求。规范管「画面内容怎么写」，契约管「输出长什么样」——
# 两者分开：规范全文原样给出，这里只写「规范内容 → 剧本字段」的映射和
# 规范里没有的台词规则。
_SYSTEM_PROMPT_TMPL = """你是条漫分镜编剧。下面这份《连续剧情提示词生成规范》必须完整遵守——
它管的是每一格 `画面:` 的**内容规则**（固定部分、七个要素、服饰颜色、灯光氛围、
位置副词、互斥规则）。规范的「输出格式」一节**不执行**，输出格式改用本提示词
最后面的剧本契约（下游是程序按契约解析）。

======== 规范全文开始 ========

{spec}

======== 规范全文结束 ========

把规范落到剧本契约上的方式：

- 规范的「固定部分」（1girl + 角色名 + 脸部特征）作为**每一格** `画面:` 的开头，
  原样重复，一个字不改——这就是「每格都写死角色」的实现方式。
- 规范的「动态部分」就是每格 `画面:` 的其余内容，按规范七个要素的顺序写
  （场景→服饰→动作→神态→视角→灯光氛围）。
- 规范的「男女双人」规则**本轮不适用**——只做单人。画面里允许出现视角方
  （哥哥/「你」）的**手或手臂**（如 `male hand, grabbing her wrist`），
  但他的脸和身体不入画，`画面:` 仍以 `1girl, solo,` 开头。

输出必须严格遵守下面的结构，不要加任何解释、前言、后记，也不要加代码块围栏：

# 标题：<中文标题，12 字以内>

## 01
画面: <英文标签>
气泡: （女）<中文台词>
气泡2: （女）<可选——这一格的第二句台词，没有就整行删掉>
标签1: <第一人称剧情旁白条，必写，十几字>
标签2: <可选，接着上一条继续讲；最多写到 标签3>

## 02
画面: ...
气泡: ...

硬性要求：
1. 每格以 `## NN` 开头，NN 为两位数字、从 01 起连续；格数必须正好是指定格数。
2. `画面:` 只能是**英文 booru 标签**（逗号分隔），**绝对不许出现任何中文**。
   按上面说的「固定部分原样重复 + 动态七要素」写，服饰颜色款式每格写全、
   灯光整段一致——这些规范里都有，逐条执行。
3. **每条气泡都要带说话人**，写成 `气泡: （女）台词`：`（女）` = 画面里的女生
   说的，`（我）` = 镜头外「我」说的（这一格画面里只有他的手）。这个前缀
   **不会**画到图上，它只决定气泡尾巴指向谁——写错了气泡就挂到错的人头上。
   台词要**口语化、有活人感**：像真的在说话，可以带省略号、语气词、话说一半
   （例：「哥…哥你怎么在这里…」「快松手…不然我要喊人了」）；
   禁止书面腔、禁止总结陈词。台词表达的是**此刻的情绪**，不复述画面。
4. `气泡:` 必写，**不超过 {max_bubble} 个字**（要短到能排成 2–3 行、每行不超过
   8 个字）。想让这一格话多，就再写一行 `气泡2:`，同样是**不超过 {max_bubble}
   个字**——两条台词拆着说，不是把一条写长。不是每格都需要气泡2。
5. `标签1:` `标签2:` `标签3:` **每一格至少要有一条**，是画面右上角的**剧情
   旁白条**，每条十几到 {max_label} 个字，最多 {max_labels} 条。用第一人称
   「我」写（镜头就是「我」的眼睛），**这一栏负责讲剧情**：第一条可以交代前情
   （例：`妹妹生闷气离家出走`），后面几条写这一格推进了什么（例：`一只脚已经
   踩上车门`）。**不要写成时间地点**（「傍晚」这种没有剧情信息的不要写），
   也不要复述画面里已经看得见的东西。它和 `气泡:` 是互补的两条线：气泡是她
   说的，标签是「我」这边的推进。
   ⚠️ `标签:` 是**旁白，不是台词**：不要写 `（女）`/`（我）` 这种说话人前缀，
   也不要写成角色的口语。想给这一格加**第二句台词**，写 `气泡2:`，别写
   `标签2:`——写成 `标签2:` 会让这句台词变成旁白、气泡2 丢掉。
6. 整话必须是一条故事线（起 → 承 → 转），后一格不许推翻前一格已经建立的事实。
   把每格的 `标签` + `气泡` 按顺序连起来读一遍，**读得通才交稿**。
7. 剧情节奏：{pacing}
8. 不要写 `任务号:` 这一行。
"""


def _pacing(panels):
    """节奏要求随格数变——2 格要的是「两极对比」，不是「逐步递进」。

    这是 2 格和 3 格最本质的差别：格数够才有「过程」，格数少只剩「对比」。
    提示词若还写着「逐格递进」，模型会把 2 格写成没落差的上下半场。
    """
    if panels <= 1:
        return "单格：只画一个定格画面，把这一格的情绪推到最满。"
    if panels == 2:
        return ("两格，**反转结构**：第一格是「之前」，第二格是「之后」"
                "（时间、次数或关系已经推进了一大截，例如「第一次」对「100 次"
                "之后」、「好感度 0」对「好感度 100」）。两格必须是同一个角色、"
                "同一类场景，但状态完全颠倒——表情、姿态、主动还是被动、和对方"
                "的距离、光影冷暖，全部反着来。不要在任何一格里解释「怎么变的」，"
                "落差本身就是内容。")
    return ("多格：每一格是一个明确阶段，情绪或关系逐格递进；"
            "不要写成一段连续的对话。")


def _system_prompt(panels):
    return _SYSTEM_PROMPT_TMPL.format(
        spec=_spec_text(), max_bubble=MAX_BUBBLE, max_label=MAX_LABEL,
        max_labels=MAX_LABELS, pacing=_pacing(panels))


_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$")


def _clean(text):
    """去掉模型爱加的代码块围栏和首尾空白。"""
    lines = [ln for ln in (text or "").splitlines() if not _FENCE_RE.match(ln)]
    return "\n".join(lines).strip()


def _user_prompt(brief, panels, character, prev=""):
    parts = ["设定：%s" % brief.strip(), "格数：%d" % panels]
    if character.strip():
        parts.append("角色外观标签（每一格原样重复这套）：%s" % character.strip())
    # 连载（2026-10-18）：把上一集的「气泡 + 旁白」原样给它，不做摘要。
    # 三条接续要求都是实测过的坑——重演已经演过的节拍、人物关系归零、
    # 把上一集的服饰照抄到新场景（见 comic_spec 第九节最后两行）。
    if (prev or "").strip():
        parts.append(
            "前情：以下是上一集已经演过的内容，本集从它结束的地方接着往下写。\n"
            + prev.strip() + "\n"
            "接续要求：\n"
            "① 不许重演前情里的情节，也不许把已经揭晓的事再演一遍"
            "（认过的面别再认一次、说过的话别再问一遍）；\n"
            "② 人物关系、情绪和已经发生的事必须延续——上一集她刚被拒绝，"
            "这一集就不能当没发生过；\n"
            "③ 前情里的服饰和场景不要求照抄，换场就换景。")
    return "\n".join(parts)


def _looks_like_dialogue(one):
    """这行旁白是不是「台词写错了栏」——判据是它以说话人前缀开头。

    复刻 `comic` 认前缀的那两条正则（前缀长什么样只该有一处定义），额外加
    「括号里 ≤3 字」这道闸：`（女）`/`（我）`/`（妹妹）` 才算，免得把
    `（她把书包扔过来）` 这种正常旁白误判成台词。
    """
    for rex in (comic._SPEAKER_BRACKET_RE, comic._SPEAKER_COLON_RE):
        m = rex.match(one or "")
        if m and len(m.group(1)) <= 3 and comic._speaker_of(m.group(1)):
            return True
    return False


def _validate(text, panels):
    """按 comic.py 的契约真解析一遍，返回 (是否合格, 问题描述)。"""
    _, got = comic.parse_script_text(text)
    if len(got) != panels:
        return False, "只解析到 %d 格，要求 %d 格" % (len(got), panels)
    # 旁白里混进台词——最常见的「字段写错」：模型想写第二句台词（`气泡2:`），
    # 却写成了 `标签2:`，于是台词被解析成旁白、气泡2 消失。2026-10-11 首次实跑
    # 就逮到：02、03 两格的 `标签2: （女）…` 被吃成了旁白。
    # ⚠️ 这一处**跨格一次全报**：模型会在好几格犯同一个错（那次 01 是对的两格错），
    # 一次只报一格的话，回炉一次只改得动一格。
    leaked = []
    for p in got:
        for one in [x.strip() for x in (p.get("label") or "").split("\n") if x.strip()]:
            if _looks_like_dialogue(one):
                leaked.append("格 %s 的 `%s`" % (p["no"], one))
    if leaked:
        return False, ("这些 `标签:` 写成台词了：%s。`标签` 是画面右上角的**旁白**，"
                       "里面不能出现 `（女）`/`（我）` 这类说话人前缀——那是台词。"
                       "第二句台词请写成 `气泡2:`（同样带前缀），`标签:` 只写"
                       "第一人称剧情旁白。" % "；".join(leaked[:4]))
    for p in got:
        no = p["no"]
        if not p["scene"].strip():
            return False, "格 %s 缺 `画面:`" % no
        if _CJK.search(p["scene"]):
            return False, "格 %s 的 `画面:` 里混进了中文（必须全是英文标签）" % no
        if not p["bubble"].strip():
            return False, "格 %s 缺 `气泡:`" % no
        if len(p["bubble"].strip()) > MAX_BUBBLE:
            return False, ("格 %s 的 `气泡:` 有 %d 字，超过 %d 字上限，必须缩短"
                           % (no, len(p["bubble"].strip()), MAX_BUBBLE))
        if len(p.get("bubble2", "").strip()) > MAX_BUBBLE:
            return False, ("格 %s 的 `气泡2:` 有 %d 字，超过 %d 字上限，必须缩短"
                           % (no, len(p["bubble2"].strip()), MAX_BUBBLE))
        # 标签是剧情旁白条，必须每格都有——而且**可以有多条**（`标签1:` `标签2:`…，
        # 解析器把它们用 \n 拼在 label 上）。这是「剧情几乎没有」的修复手段
        # （2026-10-11）：只写在提示词里拦不住——模型会整行删掉，而旧契约本来
        # 就把它定义成「可选」，所以必须由代码兜。
        labels = [x.strip() for x in (p.get("label") or "").split("\n") if x.strip()]
        if not labels:
            return False, ("格 %s 缺 `标签1:`——它是第一人称剧情旁白条，每一格"
                           "都必须写（写这一格推进了什么，十几字）" % no)
        if len(labels) > MAX_LABELS:
            return False, ("格 %s 写了 %d 条旁白，最多 %d 条"
                           "（`标签1:`~`标签%d:`，再多画面右上角就糊了）"
                           % (no, len(labels), MAX_LABELS, MAX_LABELS))
        for one in labels:
            if len(one) > MAX_LABEL:
                return False, ("格 %s 的 `标签:` 太长（%d 字，上限 %d）：%s"
                               % (no, len(one), MAX_LABEL, one))
    return True, ""


def write_script(brief, panels=3, character="", tries=3):
    """调 LLM 写剧本；不合格就带着错因回炉重写。返回剧本文本（markdown）。

    回炉是必需的：模型第一次常犯两类错——把 `画面:` 写成中文，或者把气泡
    台词写长。与其让下游解析出一堆空字段、跑到一半才发现，不如在这里卡住重来。
    """
    if not (brief or "").strip():
        raise ValueError("设定不能为空")
    messages = [
        {"role": "system", "content": _system_prompt(panels)},
        {"role": "user", "content": _user_prompt(brief, panels, character)},
    ]
    why = ""
    for attempt in range(1, tries + 1):
        text = _clean(llm.call_llm(messages))
        ok, why = _validate(text, panels)
        if ok:
            log.info("剧本已生成（第 %d 次尝试，%d 格）", attempt, panels)
            return text
        log.warning("剧本不合格（第 %d 次尝试）：%s", attempt, why)
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": "这份剧本不合格：%s。请重新输出**完整**剧本，"
                       "只输出剧本本身，不要解释。" % why,
        })
    raise RuntimeError("连写 %d 次都不合格，最后一次的问题：%s" % (tries, why))
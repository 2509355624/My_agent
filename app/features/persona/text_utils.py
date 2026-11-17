"""扮演态正文解析与出图提示词：场景/服饰/心情/状态四行、评级、固定块。

只做字符串处理，不读写状态、不调 LLM、不入队。
"""

import logging
import re

log = logging.getLogger("persona.text")

MOODS = ("平静", "开心", "害羞", "紧张", "生气", "撒娇", "心动", "难过", "疲惫", "暧昧")

# 出图门槛：强度到这条线才配图，或者这一轮发生了服装变化。
# 每轮都出会把对话框刷成图册，而且白烧显卡（用户 2026-11-02 定）。
DRAW_INTENSITY = 7

# 「心情 / 状态 / 画面 / 服饰」四行的正则。

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

_MOOD_RE = re.compile(r"^[ \t]*心情[：:][ \t]*(.+)$", re.M)
_STATE_RE = re.compile(r"^[ \t]*状态[：:][ \t]*(.+)$", re.M)
_SCENE_RE = re.compile(r"^[ \t]*画面[：:][ \t]*(.*)$", re.M)
_SCENE_NOISE_RE = re.compile(r"[`*]+")
_OUTFIT_RE = re.compile(r"^[ \t]*服饰[：:][ \t]*(.*)$", re.M)


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


_MONO_TAG_RE = re.compile(r"^[ \t]*【心声】[ \t]*[:：]?[ \t]*", re.M)

# 心声重新结构化（2026-11-08）：用 `〔心声〕` 而不是老的 `【心声】`，是为了
# 和已落盘的老回复、以及「旧标签一律剥掉」的既有行为完全隔开——老归档照旧剥，
# 新标记留给网页端上色；QQ 这类纯文本出口在发出去之前用下面这个函数剥掉。
_THOUGHT_TAG_RE = re.compile(r"^[ \t]*〔心声〕[ \t]*[:：]?[ \t]*", re.M)


def strip_thought_marker(text):
    """纯文本出口：剥掉行首的 `〔心声〕` 标记，只留那句话本身。"""
    return _THOUGHT_TAG_RE.sub("", str(text or ""))


def _strip_monologue_tag(text):
    """剥掉行首的 `【心声】` 标签，只留那句话（2026-11-06）。

    契约已不再写独立心声行，但**已落盘的旧回复**还带着这个标签，会随历史
    滚进上下文被模型照抄，网页回看时也像一串元数据。这里统一在读取/出口的
    副本上剥掉，落盘数据保持原样，随时可以回退。
    """
    return _MONO_TAG_RE.sub("", str(text or ""))


def _strip_monologue_marks(text):
    """把包内心独白的反引号/星号、以及旧标签去掉，只留文字（2026-11-03）。

    群友要求「多增加心理活动」，模型确实开始写了，但**习惯用反引号包起来**
    ——`为什么偏偏是腰。` 这种。契约里写了「不加引号、不加反引号」，模型
    还是照着历史里的旧格式抄（历史里已经有带反引号的回复，越传越多）。

    所以在出口兜一道：反引号和成对星号一律剥掉，只留文字本身。
    单星号不动（可能是乘号或占位），成对才清。2026-11-06 起连
    `【心声】` 标签一起剥，新旧回复在聊天里一个样。
    """
    s = str(text or "")
    if "`" in s:
        s = s.replace("`", "")
    # 成对的 *...*（斜体标记）→ 去标记留内容
    s = _ITALIC_RE.sub(r"\1", s)
    return _strip_monologue_tag(s)


_ITALIC_RE = re.compile(r"\*([^*\n]{1,120})\*")


# 模型的拒答尾巴（2026-11-06 用户要求「把 sorry i can't 那段屏蔽掉」）。
#
# 场景：正文其实已经写完了，模型在末尾又补一句平台话术。它出现在聊天里
# 非常出戏。判据刻意保守——只认**成句的样板话**，不做「含 sorry 就删」：
# 角色自己在剧情里说「对不起」是正常的。
_REFUSE_PAT = re.compile(
    # Sorry, I can't ... / I'm sorry, but I cannot ... —— 整句一起命中，
    # 否则会留下一个孤零零的「Sorry,」。
    r"(?:(?:sorry|i(?:'|’)?m\s+sorry)\b[^.\n]{0,20}?)?"
    r"i\s*(?:can(?:'|’)?t|cannot|am\s+unable\s+to|won(?:'|’)?t)\s+"
    r"(?:respond|answer|help|assist|continue|provide|comply|write|generate)"
    r"|as an ai(?:\s+language\s+model)?"
    r"|作为一个?\s*AI\s*(?:语言模型|助手)"
    r"|抱歉[，,]\s*我(?:无法|不能|没办法)(?:回答|提供|协助|继续|满足)"
    r"|我(?:无法|不能)回答(?:这个|该|此)?(?:问题|请求)",
    re.I)


def _strip_refusal(text):
    """剪掉模型末尾那句拒答/报错，正文保留。

    只处理「样板句」：整行命中 → 整行删；行内命中且那一句在靠后的位置 →
    从那里截断（避免误伤「她道歉」这种剧情台词）。
    """
    s = str(text or "")
    if not s:
        return s
    lines = []
    for line in s.split("\n"):
        m = _REFUSE_PAT.search(line)
        if not m:
            lines.append(line)
            continue
        # 命中位置很靠前、且这一行基本就是那句话 → 整行丢掉；
        # 命中在后面 → 只截掉尾巴，前面的正文留着。
        if m.start() <= 2:
            continue
        lines.append(line[:m.start()].rstrip())
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def split_outfit(text):
    """抽「服饰：」那一行（当前穿着的英文串），取**最后一处**匹配。

    和 `split_turn` 分开，是为了不动那个五元组签名：服饰是新增的第四行元数据，
    但老调用点、老归档、单测都按原来的形状拆包。清洗只去反引号/星号、
    去首尾空白，代码不判断「穿了什么」——那是演员的事。
    """
    hits = list(_OUTFIT_RE.finditer(str(text or "")))
    if not hits:
        return ""
    val = _SCENE_NOISE_RE.sub("", hits[-1].group(1).strip()).strip()
    return val[:300].strip(" ,")


def split_turn(text, default_mood=""):
    """把一轮输出切成 `(正文, 画面, 情绪, 强度, 状态)`。

    「画面：」「服饰：」「心情：」「状态：」四行都是给代码的元数据，**全部从
    正文里剥掉**——用户不该在聊天里看到「状态：白衬衫解开两粒扣」这种东西。
    服饰行的值另由 `split_outfit()` 取，本函数只负责把它从正文里拿走。

    三条都取**最后一处**匹配（同 `_SCENE_RE` 的理由：正文里偶尔会提到这些词）。
    缺哪行就当没写：情绪空串、强度 0、状态空串，调用方不许兜底。
    """
    raw = str(text or "")
    # 先把模型的拒答/报错尾巴剪掉（2026-11-06 用户要求）：正文已经写完了，
    # 末尾偶尔会跟一句 `Sorry, I can't respond to this question.`。那是
    # 平台层的安全话术，不是剧情，不该出现在聊天里。
    raw = _strip_refusal(raw)
    body = raw
    out = {"scene": "", "mood": "", "intensity": 0, "state": ""}

    for key, rx in (("scene", _SCENE_RE), ("mood", _MOOD_RE), ("state", _STATE_RE)):
        hits = list(rx.finditer(body))
        if not hits:
            continue
        val = hits[-1].group(1).strip()
        body = rx.sub("", body)
        out[key] = val

    # 服饰行同样从正文剥掉；值由 split_outfit() 单独取。
    body = _OUTFIT_RE.sub("", body)
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


def _fixed_block(card, outfit=""):
    """角色卡上的**外观固定块**：每轮都拼在画面标签前面，保证同一个角色不换脸。

    `appearancePrompt` 是外观，一定拼。`outfitPrompt` 只是**卡上的默认衣服**：
    一旦有了固化的当前穿着（`outfit`），它就得退场——那件写死的外套每轮
    硬拼在前面，正是「正文里脱了、下一张图又穿回来」的代码层原因。
    没有固化服饰时（首轮 / 老线）照旧拼，行为不变。
    """
    parts = [str(card.get("appearancePrompt") or "").strip()]
    if not str(outfit or "").strip():
        parts.append(str(card.get("outfitPrompt") or "").strip())
    return ", ".join(p for p in parts if p).strip(", ").strip()


# 2026-11-02 删掉整段中文→英文的服装翻译表（`_OUTFIT_MAP` / `_OUTFIT_GROUPS` /
# `_outfit_from_state`）。
#
# 为什么删：模型本来就写得比翻译表准。它写在「画面」里的
# `hands clutching the loosened collar of her pajamas` 是准确的，而翻译表只能
# 从「状态」的中文里捞出 `pajamas` 这种粗粒度标签——等于把模型写好的信息扔掉，
# 换成我手写的猜测。现在契约要求「画面行必须写穿着」，服装直接由模型给英文，
# 不再经过这一层。


# ─── 评级 tag（2026-11-13 群友报「该露的地方被遮住」）─────────
#
# 现象：明明是正戏，出图却把隐私部位糊成黑块、或者用一堆东西遮住。
# 根因不是负向词（`silver` 的负向只有画质词），也不是代码加了什么限制，
# 而是**画面行开头那个 `safe`**：
#
#     safe, 1girl, solo, pov, blonde hair, ... completely nude ...
#     ↑
#     Danbooru 评级 tag「安全内容、非露骨」
#
# 模型收到 `safe` 就会主动回避裸露——它的做法不是拒绝出图，而是
# 「想办法遮起来」，表现就是黑块、手臂挡、被单盖、构图切掉那一块。
# 演员在任何场景都写 `safe`（契约里没教它什么时候该换），所以每一张
# 亲密图都带着这个反向指令。
#
# 修法两道：
#   ① 契约里教演员按场景写对的评级（见 `_CONTRACT` 的「评级 tag」一节）；
#   ② 这里做代码兜底——已经能看出是亲密画面，评级还是 `safe` 的，直接改掉。
#      演员漏写一次不该让整张图废掉。
#
# ⚠️ 只改**评级词本身**，不删不加任何别的 tag；`safe → sensitive` 是最小改动，
# 画面内容仍然完全由演员那一行决定。

_RATING_TAGS = ("safe", "sensitive", "nsfw", "explicit")

# 明确「有裸露」的信号：出现任何一个，评级至少得是 sensitive。
_NUDE_SIGNALS = (
    "completely nude", "fully nude", "nude", "topless", "bottomless",
    "bare breasts", "bare chest", "exposed breasts", "breasts out",
    "bare pussy", "pussy visible", "spread pussy", "exposed pussy",
    "bare ass", "bare buttocks", "naked",
)

# 明确「露器官 / 正在做」的信号：这些出现，评级应该是 nsfw。
_EXPLICIT_SIGNALS = (
    "penetration", "inside her", "inside him", "sex from behind",
    "cunnilingus", "fellatio", "cum", "semen", "ejaculat",
    "pussy juice", "wet pussy", "clitoris", "labia", "anus visible",
    "pussy", "nipples visible", "areola",
)


def _rating_of(parts):
    """这一行现在写的是哪个评级？没有返回空串。"""
    for x in parts:
        k = str(x or "").strip().lower()
        if k in _RATING_TAGS:
            return k
    return ""


def _wanted_rating(text):
    """按画面内容判「至少该是哪个评级」。判不出来返回空串（不动）。"""
    low = str(text or "").lower()
    if any(s in low for s in _EXPLICIT_SIGNALS):
        return "nsfw"
    if any(s in low for s in _NUDE_SIGNALS):
        return "sensitive"
    return ""


def _fix_rating(prompt):
    """把 `safe` 升级成画面内容需要的评级。返回 `(新文本, 改了什么)`。

    **只降不升的反向也做**：内容明显是穿衣日常却写了 `nsfw`，改回 `safe`——
    评级和画面矛盾时两边都会画歪（写成 nsfw 的日常图会自己往情色构图跑）。
    """
    raw = str(prompt or "")
    if not raw.strip():
        return raw, ""
    parts = [x.strip() for x in raw.split(",")]
    cur = _rating_of(parts)
    want = _wanted_rating(raw)
    if not want:
        return raw, ""
    if cur == want:
        return raw, ""
    # 已经有了更高级的评级，不往回压（演员写了 explicit 就别改）
    order = {"safe": 0, "sensitive": 1, "nsfw": 2, "explicit": 3}
    if cur and order.get(cur, 0) >= order.get(want, 0):
        return raw, ""
    out = []
    replaced = False
    for x in parts:
        if not replaced and x.strip().lower() in _RATING_TAGS:
            out.append(want)
            replaced = True
        else:
            out.append(x)
    if not replaced:
        out.insert(0, want)
    note = "%s → %s" % (cur or "（无评级）", want)
    return ", ".join(y for y in out if y), note


# POV 机位固定模板（2026-11-08）：由代码拼在提示词末尾，不交给演员每轮重写。
# 只写机位，不写人数词、不写 `1other` / `pov hands` / `solo`，也不写 `no ...`
# 反向钉住——主体由角色固定块声明一次，视角方不进画就不会被凑出第二个人。
_POV_CAMERA = "first-person pov, eye level"


def build_prompt(card, scene, state="", pov=False, outfit=""):
    """出图提示词 = 角色卡外观 + 当前服饰 + 这一轮的画面（+ POV 机位）。

    `outfit`（2026-11-09）：演员上一轮交、系统固化的英文服饰串。放在**画面行
    之前**，权重高过景别/动作/光照；有它的时候卡上写死的 `outfitPrompt` 不再
    拼（`_fixed_block`），画面里穿什么就只有这一个来源。
    参考 `picture_prompt_produce` 的 `buildTurnPrompt`：服饰单独一行排最前，
    就是为了压住「每轮重写、越写越飘」。

    `state` 保留在签名里但**不参与拼装**（2026-11-02）：它是中文整行，
    现在只用于 `clothes_changed()` 判断要不要出图。

    `pov=True` 只给角色扮演的 `draw()` 用：`galgame` 与既有调用方不传，
    行为完全不变；`outfit` 同理，不传就是老的四段拼法。
    """
    fixed = _fixed_block(card, outfit)
    scene = _clean_scene(scene)
    cur = str(outfit or "").strip().strip(", ")
    parts = [x for x in (fixed, cur, scene,
                         _POV_CAMERA if pov else "") if x]
    prompt = ", ".join(parts)
    # 评级兜底（2026-11-13）：演员在亲密画面里仍然写 `safe`，模型就会主动
    # 把该露的地方盖住。这里按画面内容把评级改成对的。
    prompt, _note = _fix_rating(prompt)
    if _note:
        log.info("出图评级修正：%s", _note)
    return prompt


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

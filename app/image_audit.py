"""发图前的 NSFW 闸门：AI 生图 → 发给对方之前，先让识图模型看一眼。

## 它拦什么

**最严白名单口径**（2026-10-05 用户拍板：agent 从 @ 轮退场后，审核是群里
唯一的内容闸门，宁可错拦不可错放）：

- **白名单式**：只有 level 0 清单里的（日常立绘 / 校园 / 街景 / 校服制服等
  正常穿着 / 正常表情动作互动）才放行；清单外的服装、场景、道具一律从
  level 1 起判。
- **1-3 全拦**：泳装、比基尼、内衣、温泉浴室这些旧口径的「擦边放行」项
  现在一律拦。
- **拿不准 = 拦**：模型自报 confidence=low 时，代码层强制 allow=False
  （parse_verdict 里兜底，不依赖模型自觉）；是 1 还是 2 拿不准按 2。
- 特殊红线不论级别：未成年+性化、真人肖像性化、违法内容。

⚠️ 这条边界改过三轮：最初只拦「性器官裸露/明确性行为」→ 一度收紧成
「大面积裸露一律拦」→ 2026-10-01 回到适中（泳装/内衣正常穿着放行）→
**2026-10-05 最严白名单**（当前）。每轮反转都由 PromptPolicyTest 钉着。

顺带记一个此前实测的结论：**DeepSeek 不会拒收露点图**（不是 HTTP 400），而是
正常返回 `allow: false`。所以不需要本地分类器。

## 失败一律拦截（fail-closed）

识图超时、报错、返回解析不了、图读不出来 → **一律拦下不发**。这是用户
2026-10-01 明确选的（改前是 fail-open：失败放行）。

代价要知道：本机有 volc 429 的前科，识图 provider 一抖，**所有图都发不出去**。
所以失败路径回的是另一句提示（`FAILED_NOTICE`，明说是审核没响应，不是描述有
问题），别和真判违规混成一句——否则用户会以为自己的提示词写坏了。

## 为什么审的是 prepare_for_send 的产物

调用方先过 `image_out.prepare_for_send` 拿到**本地文件**（已经甩掉工作流元数据），
再审这个文件——审的就是**真正要发出去的那份字节**，不是 ComfyUI 的原图。
好处是顺带覆盖了「转格式之后才出问题」的情况，也不用再下载一次。

## 开关

三层，全在 settings.json（热生效，不用重启），见 `agents.image_audit_enabled`：
按会话类型的两个总开关 `image_audit_groups` / `image_audit_private`，再加单会话
`image_audit_overrides[会话号]`。默认**全关**（加这个功能之前的行为）。

## 提示词可以自己改

上面 `_PROMPT` 是**内置默认**。settings.json 里设 `image_audit_prompt`（管理页
「审核提示词」按钮）就整份替换掉它，留空 / 删掉 = 回落默认（见
`agents.image_audit_prompt`）。2026-10-01 用户要求能自己改口径，之前它写死在
代码里，调一句得改代码 + 重启适配层。

⚠️ 换掉之后 `_PROMPT` 里那三条约定就**不再受保护**了：模型必须回一行 JSON
（`parse_verdict` 只认这个）、`category` 只能是 `_CATEGORIES` 里那几个。自定义
的提示词要是没写输出格式，会解析失败 → fail-closed → **所有图都发不出去**。
管理页的编辑器默认把内置那份填进去，照着改就丢不了。
"""

import json
import logging
import re

from app.config import IMAGE_AUDIT_TIMEOUT

log = logging.getLogger("image_audit")

# 审核提示词（**内置默认**，可被 settings.json 的 image_audit_prompt 整份替换）。
# 要点（2026-10-05 最严版）：
#   1. **白名单式**——只有 level 0 清单里的才放行，清单外从 level 1 起判；
#      每一档都给足具体例子，只写「禁止性化」模型每张图的尺子都不一样；
#   2. **confidence=low 直接拦**——提示词里写一遍给模型，parse_verdict 里
#      再兜底一遍不信任它；
#   3. **只输出一行 JSON**、**reason 限 20 字**——输出短好解析，也省 token。
_PROMPT = (
    "你是图片审核员，判这张图能否直接发到普通群（群里有老人同事、随时被截图）。"
    "宁可错拦，不可错放。\n"
    "只输出一行 JSON，不要解释、不要 markdown：\n"
    '{"allow": true/false, "level": 0-3, "confidence": "high|low", '
    '"category": "ok|skin|pose|clothing|scene|intimacy|minor|real|other", '
    '"reason": "不超过20字"}\n'
    "level：0 完全正常 / 1 擦边 / 2 明显性化 / 3 色情或裸露。\n"
    "只有 level 0 放行，1-3 一律拦。\n"
    "confidence=low 一律拦（图糊看不出关键部位、拿不准是哪档，都是 low）。\n"
    "【level 0 白名单——只有清单里的才放行，清单外一律从 level 1 起判】\n"
    "- 日常立绘、校园、街景、战斗、料理、节日、雪景\n"
    "- 短袖、长袖、卫衣、短裤、短裙、长裤、长裙、校服、制服、运动装、"
    "和服、盔甲、职业装\n"
    "- 露肩、露背、露腰、露腿、大腿可见，正常穿着不算问题\n"
    "- 正常表情：笑、哭、生气、惊讶、平静、害羞\n"
    "- 正常动作：站、坐、走、跑、跳、打招呼、挥手、举杯、拿东西\n"
    "- 正常互动：牵手、并肩、搭肩、递东西、吵架斗嘴\n"
    "【拦：level 1 起】\n"
    "- 贴身、透视感服装，低胸见沟，短裙坐姿有走光风险\n"
    "- 泳装、比基尼、运动泳衣、内衣吊带，无论姿势表情\n"
    "- 温泉、浴室、泳池、海滩场景\n"
    "【拦：level 2】\n"
    "- 动作：一字马、劈叉、大张腿、分腿、跪姿迎合、四肢着地、趴姿后翘、"
    "塌腰翘臀、抬腿、夹腿、顶胯、镜头正对分开的双腿\n"
    "- 服饰：明显看到内裤、走光、湿身贴身、透视到身体轮廓、"
    "衣物破损致大面积走光\n"
    "- 视角：聚焦胸、臀、胯的局部特写；仰拍裙底；腿间视角；镜头对下体\n"
    "- 表情：咬唇媚眼、迷离眼神、吐舌、脸红半闭眼、高潮脸、发情脸\n"
    "- 亲密：亲嘴、舌吻、贴身纠缠、磨蹭、摸胸、摸腰、摸臀\n"
    "- 道具：情趣用品、束缚道具、蒙眼、被绑\n"
    "【拦：level 3】\n"
    "- 露出性器官、乳头、乳晕、阴部\n"
    "- 全裸、半裸、仅遮挡关键部位，透视致上述可见\n"
    "- 明确性行为描绘、自慰、口交、性交姿势\n"
    "【特殊红线，不论级别一律拦】\n"
    "- minor：外形呈未成年（幼态脸、儿童体型、小学生特征）且带性化要素\n"
    "- real：真人肖像的性化、真人照片的二创性化\n"
    "- 违法内容\n"
    "判定原则：\n"
    "- 动作尺度看是不是性化，不是看幅度大不大。运动、舞蹈、劈叉如果画面健康、"
    "穿着在白名单内、表情正常，走 level 0。\n"
    "- 服饰看有没有露不该露的（内裤、湿身透视、破损走光），"
    "露肩、露背、露腰、露腿本身不算问题。\n"
    "- 视角看镜头有没有聚焦敏感部位。正常构图无论高低机位都不算问题。\n"
    "- 白名单里没有的服装、场景、道具，一律从 level 1 起判。\n"
    "- 是 1 还是 2 拿不准 → 按 2；再拿不准 → confidence=low 直接拦。"
)

# 被拦下来之后回给对方的话。
# **故意不回显 reason**：模型的理由写得很直白（「全裸且露出乳头及下体」），
# 原样转述等于把审核结论和露骨描述一起甩回群里。用户要的是「回一句提示」，
# 不是「把判定理由念一遍」。
BLOCKED_NOTICE = "这张图没过审，没发出去。换个描述再试试？"

# 审核没生效（识图超时/报错/解析不出来）时回的话，跟上面那句**分开**。
# fail-closed 之后这两种情况都拦，但原因完全不同：一个是你画的东西违规，
# 一个是审核服务没响应。混成一句的话，识图一抖用户就会去改本来没问题的提示词。
FAILED_NOTICE = "这张图没发出去——审核服务没响应，稍后再试。"

# 模型有时会把 JSON 包在 ```json 围栏里，或前后带一句「好的，结果如下」。
# 所以先剥围栏，再退化成「找第一个 {...} 块」。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
_OBJ_RE = re.compile(r"\{.*\}", re.S)

# category 的合法值（2026-10-05 最严口径换成九值；旧口径的 nudity/sexual 保留，
# 用户自定义提示词里可能还在用——_CATEGORIES 之外的档会被归到 other）。
_CATEGORIES = ("ok", "skin", "pose", "clothing", "scene", "intimacy",
               "minor", "real", "nudity", "sexual", "other")


class Verdict(object):
    """一次审核的结果。

    allow：能不能发。failed=True 表示**审核根本没生效**（识图失败等），
    此时 allow 一定是 False（fail-closed 的体现，不是笔误）——调用方只看 allow
    就够，failed 用来区分提示语和日志。
    """

    __slots__ = ("allow", "reason", "category", "failed")

    def __init__(self, allow, reason="", category="other", failed=False):
        self.allow = bool(allow)
        self.reason = reason or ""
        self.category = category if category in _CATEGORIES else "other"
        self.failed = bool(failed)

    def __repr__(self):
        return "Verdict(allow=%r, category=%r, failed=%r, reason=%r)" % (
            self.allow, self.category, self.failed, self.reason)


def _fail_closed(why):
    """识图这条路走不通时统一走这里。**永远 allow=False。**"""
    log.warning("审核未生效，按拦截处理（fail-closed）：%s", why)
    return Verdict(False, reason=why, category="other", failed=True)


def parse_verdict(text):
    """把模型回复解析成 Verdict。解析不出来返回 None（调用方按拦截处理）。

    宽容度是必要的：实测大部分时候是干净的一行 JSON，但模型偶尔会加围栏或
    客套话。**allow 字段缺失/不是布尔时算解析失败**——这种「没看懂」走的是
    `_fail_closed`，一样拦下，但提示语和 category 不同（failed=True）。
    """
    if not text:
        return None
    raw = text.strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    if not raw.startswith("{"):
        m = _OBJ_RE.search(raw)
        if not m:
            return None
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    allow = data.get("allow")
    if not isinstance(allow, bool):
        return None
    reason = data.get("reason")
    verdict = Verdict(allow,
                      reason=reason if isinstance(reason, str) else "",
                      category=data.get("category") if isinstance(
                          data.get("category"), str) else "other")
    # 最严口径的代码兜底（2026-10-05）：模型自报 confidence=low = 它自己都
    # 拿不准——不管它 allow 写的什么，一律按拦截算。提示词里那句「low 直接拦」
    # 是给模型的自觉，这里是不信任它的保证；confidence 缺省（旧口径提示词）
    # 时不触发。
    if verdict.allow and data.get("confidence") == "low":
        verdict.allow = False
        verdict.reason = (verdict.reason + "；拿不准" if verdict.reason
                          else "审核拿不准")
    return verdict


def default_prompt():
    """内置默认提示词。管理页拿它给编辑器当初始内容 / 「恢复默认」的填充。"""
    return _PROMPT


def resolve_prompt(agent_id):
    """这个 agent 实际要用的提示词：自定义的优先，没设就是内置默认。

    读 settings 失败**不抛**，回落内置默认——闸门宁可继续用内置那份，
    也不该因为读配置出岔子就变成不审。
    """
    try:
        from app.agents import image_audit_prompt
        return image_audit_prompt(agent_id) or _PROMPT
    except Exception as exc:
        log.warning("读自定义审核提示词失败，用内置默认：%s", exc)
        return _PROMPT


def check(path, timeout=None, prompt=None):
    """审一张本地图（`image_out.prepare_for_send` 的产物）。

    prompt 不传 = 用内置默认；`allow_send` 会把自定义的那份传进来。

    **不抛异常**：读不到文件、识图失败、解析不出来，一律返回**拦截**的 Verdict
    （allow=False，failed=True）。调用方不需要 try/except。
    """
    from app import vision

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as exc:
        return _fail_closed("读图失败：%s" % exc)
    if not raw:
        return _fail_closed("图是空的")

    try:
        data_url = vision.to_data_url(raw)
    except Exception as exc:
        return _fail_closed("压缩失败：%s" % exc)

    try:
        # 显式传 provider/model = **绕过管理页上的识图选择**，永远用 .env 配的
        # 那个（vision.audit_choice）。用户 2026-10-04 拍板：审核不跟着界面切。
        # 判据是失败方向 —— 审核是 fail-closed 且超时只有 30 秒
        # （IMAGE_AUDIT_TIMEOUT），本地小模型实测几十秒到几分钟 ⇒ 跟着切会把
        # 每张图都判成「识图失败」直接拦下来，图片根本发不出去。
        a_pid, a_model = vision.audit_choice()
        text = vision.describe(data_url, timeout=timeout,
                               prompt=prompt or _PROMPT,
                               provider=a_pid, model=a_model)
    except Exception as exc:
        return _fail_closed("识图失败：%s" % exc)

    v = parse_verdict(text)
    if v is None:
        return _fail_closed("识图回复解析不出来：%r" % text[:120])
    return v


def _notify_blocked(target, target_id, verdict):
    """告诉对方这张没发出去。发不出去就算了——审核失败不该再抛一层异常。

    verdict.failed 决定说哪句：判违规 → `BLOCKED_NOTICE`；审核没生效 →
    `FAILED_NOTICE`。后者明说是服务问题，免得用户回去改没问题的提示词。
    """
    from app import qq_api
    notice = FAILED_NOTICE if verdict.failed else BLOCKED_NOTICE
    try:
        if target == "group":
            qq_api.send_group(target_id, notice)
        else:
            qq_api.send_private(target_id, notice)
    except Exception as exc:
        log.warning("审核拦截后通知失败 %s %s：%s", target, target_id, exc)


def allow_send(path, agent_id, target, target_id, notify=True):
    """发图前的总闸。**True = 可以发**。

    这是三个发图点唯一需要调用的函数：
    - 这个会话没开审核 → 直接 True（一次 settings 读，不发网络请求）；
    - 开了 → 审。**判违规和审核没生效都返回 False**（fail-closed），
      各自回一句提示；只有明确判合格才 True。

    notify=False（2026-10-05）：拦下时**不回话**——随机口令的静默重抽用，
    话术由 image_jobs 统一管（重抽期间出声会暴露内部机制，重抽尽才回一句
    不提审核的软话术）。默认 True = 老行为，判违规/没生效各自回话。

    target 传 None（网页端）时按「没开会话」处理——审核只管 QQ 外发那一步，
    网页端是自己在本地看的。
    """
    from app.agents import image_audit_enabled

    if target is None:
        return True
    try:
        if not image_audit_enabled(agent_id, target, target_id):
            return True
    except Exception as exc:
        # 注意：这里仍是放行。读不到 settings 意味着**不知道开关是什么状态**，
        # 跟「识图失败」不是一回事——settings 坏了就把所有图堵死，等于 bot 主
        # 功能停摆。要连这条也拦，得先把开关状态挪到别处存。
        log.warning("读审核开关失败，按放行处理：%s", exc)
        return True

    verdict = check(path, prompt=resolve_prompt(agent_id))
    if verdict.allow:
        return True

    log.info("图未过审，拦下不发 %s %s：failed=%s category=%s reason=%s",
             target, target_id, verdict.failed, verdict.category, verdict.reason)
    if notify:
        _notify_blocked(target, target_id, verdict)
    return False

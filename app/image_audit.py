"""发图前的 NSFW 闸门：AI 生图 → 发给对方之前，先让识图模型看一眼。

## 它拦什么

三档，命中任一就判不合格：

1. **大面积皮肤裸露**：泳装、比基尼、内衣、吊带、抹胸、背心、露肩、露背、露腰腹、
   低胸露沟、赤膊、超短裙/短裤、裸露大腿、半透明或贴身到透出身体轮廓的衣物，
   以及性器官 / 乳房 / 臀部的裸露；
2. **性暗示**：挑逗或诱惑性姿势、咬唇舔唇、迷离挑逗的眼神、暗示性表情，
   以及刻意聚焦胸 / 臀 / 胯的构图；
3. **暧昧动作**：贴身或肢体交缠的亲密动作、抚摸、拉扯或撩拨衣物，
   床榻 / 浴室 / 换衣等暗示性场景。

只有「脸、脖子、手、小臂」这类小面积裸露、且身体主体被衣物正常遮挡、姿态与
表情自然无性暗示，才算合格。

这条边界是用户 2026-10-01 定的，且当天改过一版：最初只拦「性器官裸露 / 明确
性行为」（泳装、内衣、露沟、露腿一律放行），随后整个收紧成上面这版从严口径。
`_PROMPT` 里特意写了「拿不准一律判不合格」——从严是要求本身，宁可错杀。

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
# 三个要点不能丢：
#   1. **把三档口径都具体化**（大面积裸露 / 性暗示 / 暧昧动作），各给一串例子
#      ——只写「禁止裸露和性暗示」，模型每张图的尺子都不一样，列全了界线才稳定。
#      口径是**从严**，所以同时写明「拿不准判不合格」；
#   2. **要求只输出一行 JSON** ——输出短、好解析，也省 token；
#   3. **reason 限 20 字** ——不限的话模型会写小作文，拖慢发送。
_PROMPT = (
    "你是图片审核员，标准从严，宁可错杀不可放过。判断这张图能否直接发到普通聊天群。\n"
    "出现下列任一情况就判不合格：\n"
    "一、大面积皮肤裸露：泳装、比基尼、内衣、吊带、抹胸、背心、露肩、露背、露腰腹、"
    "低胸或露沟、赤膊、超短裙、短裤、裸露大腿、半透明或贴身到透出身体轮廓的衣物，"
    "以及性器官、乳房、臀部的裸露；\n"
    "二、性暗示：挑逗或诱惑性姿势、咬唇舔唇、迷离或挑逗的眼神、暗示性表情，"
    "以及刻意聚焦胸、臀、胯的构图；\n"
    "三、暧昧动作：贴身或肢体交缠的亲密动作、抚摸、拉扯或撩拨衣物，"
    "床榻、浴室、换衣等暗示性场景。\n"
    "只有「脸、脖子、手、小臂」这类小面积裸露、身体主体被衣物正常遮挡、"
    "且姿态与表情自然无性暗示，才算合格。\n"
    "拿不准时一律判不合格。\n"
    "只输出一行 JSON，不要任何解释：\n"
    '{"allow": true/false, "reason": "不超过20字的理由", '
    '"category": "ok|skin|sexual|nudity|other"}'
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

# category 的合法值。模型给了别的（或没给）就归 other——**不影响 allow 判定**，
# 它只是给日志用的标签。`skin` 是 2026-10-01 收紧口径后加的：大面积皮肤裸露
# 既不算 nudity 也不算 sexual，单独一档看日志时好数。
_CATEGORIES = ("ok", "skin", "nudity", "sexual", "other")


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
    return Verdict(allow,
                   reason=reason if isinstance(reason, str) else "",
                   category=data.get("category") if isinstance(
                       data.get("category"), str) else "other")


def default_prompt():
    """内置默认提示词。管理页拿它给编辑器当初始内容 / 「恢复默认」的填充。"""
    return _PROMPT


def resolve_prompt(agent_id):
    """这个 agent 实际要用的提示词：自定义的优先，没设就是内置默认。

    读 settings 失败**不抛**，回落内置默认——闸门宁可继续用从严的那份，
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
        text = vision.describe(data_url, timeout=timeout,
                               prompt=prompt or _PROMPT)
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


def allow_send(path, agent_id, target, target_id):
    """发图前的总闸。**True = 可以发**。

    这是三个发图点唯一需要调用的函数：
    - 这个会话没开审核 → 直接 True（一次 settings 读，不发网络请求）；
    - 开了 → 审。**判违规和审核没生效都返回 False**（fail-closed），
      各自回一句提示；只有明确判合格才 True。

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
    _notify_blocked(target, target_id, verdict)
    return False

"""发图前的 NSFW 闸门：AI 生图 → 发给对方之前，先让识图模型看一眼。

## 它拦什么

**只拦「性器官裸露 / 明确性行为」**。泳装、内衣、比基尼、露沟、露腿、露背、
性感姿势**一律放行**——这条边界是用户 2026-10-01 定的，下面的 `_PROMPT` 就是
照它写的。实测四张样本（普通图 / 泳装 / 露点×2）判定全对：

    普通图  {"allow": true,  "reason": "紧身衣泳装，无性器官裸露"}
    泳装图  {"allow": true,  "reason": "泳装展示，未违规"}
    露点A   {"allow": false, "reason": "全裸且露出乳头及下体"}
    露点B   {"allow": false, "reason": "露骨性器官裸露及性暗示"}

同一批实测还排除了一个担心的坑：**DeepSeek 不会拒收露点图**（不是 HTTP 400），
而是正常返回 `allow: false`。所以不需要本地分类器。

## 失败一律放行（fail-open）

识图超时、报错、返回解析不了 → **放行**。这是用户明确选的（2026-10-01）。
理由：审核是锦上添花，生图才是主功能；识图 provider 一抖就把所有图堵死的话，
用户看到的是「这张没过审」，会以为自己的描述有问题——本机有 volc 429 的前科，
这不是假想。所以这里**只处理「明确判定违规」这一种拦截**。

## 为什么审的是 prepare_for_send 的产物

调用方先过 `image_out.prepare_for_send` 拿到**本地文件**（已经甩掉工作流元数据），
再审这个文件——审的就是**真正要发出去的那份字节**，不是 ComfyUI 的原图。
好处是顺带覆盖了「转格式之后才出问题」的情况，也不用再下载一次。

## 开关

三层，全在 settings.json（热生效，不用重启），见 `agents.image_audit_enabled`：
全局 `image_audit_enabled` + 单会话 `image_audit_overrides[会话号]`。
默认**全关**（加这个功能之前的行为）。
"""

import json
import logging
import re

from app.config import IMAGE_AUDIT_TIMEOUT

log = logging.getLogger("image_audit")

# 审核提示词。三个要点不能丢：
#   1. **把「允许」的清单写全**（泳装/内衣/比基尼/紧身衣/露沟/露腿/露背/性感姿势）
#      ——只写「禁止什么」的话，模型会按自己的保守标准把泳装也毙掉；
#   2. **要求只输出一行 JSON** ——输出短、好解析，也省 token；
#   3. **reason 限 20 字** ——不限的话模型会写小作文，拖慢发送。
_PROMPT = (
    "你是图片审核员。判断这张图能否直接发到普通聊天群。\n"
    "规则：只禁止「性器官裸露 / 明确性行为」。泳装、内衣、比基尼、"
    "紧身衣、露沟、露腿、露背、性感姿势都属于**允许**。\n"
    "只输出一行 JSON，不要任何解释：\n"
    '{"allow": true/false, "reason": "不超过20字的理由", "category": "ok|nudity|sexual|other"}'
)

# 被拦下来之后回给对方的话。
# **故意不回显 reason**：模型的理由写得很直白（「全裸且露出乳头及下体」），
# 原样转述等于把审核结论和露骨描述一起甩回群里。用户要的是「回一句提示」，
# 不是「把判定理由念一遍」。
BLOCKED_NOTICE = "这张图没过审，没发出去。换个描述再试试？"

# 模型有时会把 JSON 包在 ```json 围栏里，或前后带一句「好的，结果如下」。
# 所以先剥围栏，再退化成「找第一个 {...} 块」。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
_OBJ_RE = re.compile(r"\{.*\}", re.S)

# category 的合法值。模型给了别的（或没给）就归 other——**不影响 allow 判定**，
# 它只是给日志用的标签。
_CATEGORIES = ("ok", "nudity", "sexual", "other")


class Verdict(object):
    """一次审核的结果。

    allow：能不能发。failed=True 时它**一定是 True**（fail-open 的体现，
    不是笔误）——调用方只看 allow 就够了，failed 只用来打日志。
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


def _fail_open(why):
    """识图这条路走不通时统一走这里。**永远 allow=True。**"""
    log.warning("审核未生效，按放行处理（fail-open）：%s", why)
    return Verdict(True, reason=why, category="other", failed=True)


def parse_verdict(text):
    """把模型回复解析成 Verdict。解析不出来返回 None（调用方按放行处理）。

    宽容度是必要的：实测大部分时候是干净的一行 JSON，但模型偶尔会加围栏或
    客套话。**allow 字段缺失/不是布尔时算解析失败**——宁可放行，也不要拿
    「没看懂」去拦人。
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


def check(path, timeout=None):
    """审一张本地图（`image_out.prepare_for_send` 的产物）。

    **不抛异常**：读不到文件、识图失败、解析不出来，一律返回放行的 Verdict
    并把 failed 置 True。调用方不需要 try/except。
    """
    from app import vision

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as exc:
        return _fail_open("读图失败：%s" % exc)
    if not raw:
        return _fail_open("图是空的")

    try:
        data_url = vision.to_data_url(raw)
    except Exception as exc:
        return _fail_open("压缩失败：%s" % exc)

    try:
        text = vision.describe(data_url, timeout=timeout, prompt=_PROMPT)
    except Exception as exc:
        return _fail_open("识图失败：%s" % exc)

    v = parse_verdict(text)
    if v is None:
        return _fail_open("识图回复解析不出来：%r" % text[:120])
    return v


def _notify_blocked(target, target_id, verdict):
    """告诉对方这张没发出去。发不出去就算了——审核失败不该再抛一层异常。"""
    from app import qq_api
    try:
        if target == "group":
            qq_api.send_group(target_id, BLOCKED_NOTICE)
        else:
            qq_api.send_private(target_id, BLOCKED_NOTICE)
    except Exception as exc:
        log.warning("审核拦截后通知失败 %s %s：%s", target, target_id, exc)


def allow_send(path, agent_id, target, target_id):
    """发图前的总闸。**True = 可以发**。

    这是三个发图点唯一需要调用的函数：
    - 这个会话没开审核 → 直接 True（一次 settings 读，不发网络请求）；
    - 开了 → 审，明确违规返回 False 并回一句提示；其余（含识图失败）都 True。

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
        # 读 settings 都出错的话，别把图卡住——按放行走。
        log.warning("读审核开关失败，按放行处理：%s", exc)
        return True

    verdict = check(path)
    if verdict.allow:
        return True

    log.info("图未过审，拦下不发 %s %s：category=%s reason=%s",
             target, target_id, verdict.category, verdict.reason)
    _notify_blocked(target, target_id, verdict)
    return False

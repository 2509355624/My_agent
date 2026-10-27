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

调用方先过 `image_out.prepare_for_send` 拿到**本地文件**（jpg 是重编码过的，
png 是带工作流的原图副本），
再审这个文件——审的就是**真正要发出去的那份字节**，不是 ComfyUI 的原图。
好处是顺带覆盖了「转格式之后才出问题」的情况，也不用再下载一次。

## 误拦有补救：人工二审

判违规**不等于作废**。拦下的图会连 caption 一起留进 `state/review/`，管理页
「人工二审」里能直接看到图，点「过审」就按正常格式补发回原会话（见
app/image_review.py）。所以「宁可错拦」这套口径的代价是**可回收**的：错拦
只是让管理员多点一下，不是把用户那张图扔了。

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
import threading

from app.config import IMAGE_AUDIT_TIMEOUT

log = logging.getLogger("image_audit")

# 审核提示词（**内置默认**，可被 settings.json 的 image_audit_prompt 整份替换）。
# 口径沿革：10-01 适中 → 从严 → 适中 → 10-05 **最严白名单**（只有 level 0
# 清单里的才放行）→ 10-26 **只认裸露**（用户原话：「只要不是内裤或者性器官，
# 我感觉都可以过，不然 AI 老是拿不准拿不准，明明是合规的」）。
#
# 为什么必须换掉最严那版：白名单 + 「拿不准就上调一档」+ 代码再兜一遍
# 「confidence=low 一律拦」，三者叠加后**模型自己判合规的图照样被拦**。
# 10-26 日志实录（42 次拦截里挑 4 条，reason 全是它写的合规结论）：
#   category=ok       「幼态+完整服装日常场景，无性化动作；拿不准」
#   category=clothing 「仰视抬头自拍照，裙摆短露腿，无走光；拿不准」
#   category=clothing 「旗袍过短显腿，无走光，表情搞怪；拿不准」
#   category=minor    「幼态脸双马尾，但服装完整日常自拍，无性化动作；拿不准」
# 规律很清楚：**清单越细，模型越容易在「清单外但明显合规」的图上卡住**，
# 一卡就落进「拿不准 → 拦」。现在只留硬红线（裸露 / 内裤 / 幼态性化），
# 其余全部交模型判，并显式写死「拿不准就放行」。
_PROMPT = (
    "你是图片审核员，判这张图能不能直接发到普通群（群里有老人同事、随时被截图）。"
    "只有裸露这一条红线。\n"
    "只输出一行 JSON，不要解释、不要 markdown：\n"
    '{"allow": true/false, "level": 0-2, '
    '"category": "ok|skin|nudity|minor|other", "reason": "不超过20字"}\n'
    "level：0 放行 / 1 露内裤 / 2 裸露。\n"
    "【拦，只拦这三类】\n"
    "- 性器官、乳头乳晕、臀部裸露；全裸、半裸、仅用手发或物件挡住关键部位。\n"
    "- 内裤清楚可见：外穿、走光露出来、只靠细带挡关键部位的都算。\n"
    "- 幼态脸或儿童体型，**同时**命中下列任一项（封号红线，看组合不看脸）：\n"
    "  ① 性化姿势：跪趴、翘臀、张腿、俯身顶胯、跨坐；\n"
    "  ② 性化表情：吐舌、媚眼、脸红半闭眼、咬唇；\n"
    "  ③ 与成年异性同框且氛围暧昧（床或被褥上、更衣、贴身距离）。\n"
    "【放行，下面这些一律 allow=true】\n"
    "泳装、比基尼、运动内衣、内衣款上衣、吊带、背心、露肩、露背、露腰、低胸、"
    "短裙、短裤、裸腿、高开叉、透视薄纱、湿衣贴身、睡衣、浴衣、丝袜；\n"
    "卧室、浴室、温泉、泳池、沙滩场景；\n"
    "跪姿、趴姿、张腿、抬腿、劈叉、低机位仰拍、聚焦腿部或腰臀；\n"
    "咬唇、眯眼、吐舌、脸红、轻佻暧昧表情、叼烟（**幼态脸除外，见【拦】③**）；\n"
    "画风怪、比例错、画质糊、构图歪、像机器人自画像。\n"
    "——以上**单独出现或任意组合**都放行，只有同时命中【拦】那三类之一才拦。\n"
    "【最重要：拿不准就放行】\n"
    "判断不了图里有没有露内裤或性器官 → allow 写 true。"
    "「拿不准」「可能是」「擦边」「有点过」都不是拦截理由；"
    "只有你能指着图上某个位置说「这里露了内裤或性器官」才拦。"
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
    # 2026-10-26 删掉「模型自报 confidence=low 一律拦」的代码兜底。
    # 它跟 10-05 那版白名单口径配套，实测是**误拦主因**：模型明明写出
    # 「无走光；拿不准」「服装完整日常自拍，无性化动作；拿不准」这种合规结论，
    # 只要带了 low 就被代码改成拦（10-26 一天 6 次）。新口径不再要求输出
    # confidence，这里也不该再拿它当闸门——判断权交回模型。
    # 真正的兜底仍是 fail-closed：识图失败 / JSON 解析不出来 → 拦（见 _fail_closed）。
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


def _call_with_deadline(fn, timeout):
    """跑 `fn()`，**最多等 timeout 秒**，超了就放弃等待。

    为什么非要补这一层：`vision.describe` 里的 `urllib3.Timeout(total=...)`
    **不是**「整次请求的墙上时钟」。翻 urllib3 2.6.3 源码，`read_timeout` 只在
    建连后算一次（`min(total - 建连耗时, read)`）就 `settimeout` 到 socket 上，
    那是**每次 recv** 的上限，之后不再重算——对端只要持续吐数据（哪怕很慢），
    整次就能远超 total。2026-10-16 实录：一次审核识图挂了 **362.7 秒**
    （total 只有 120），而审核串在出图 worker 上，一次挂死把整条队列堵住，
    队尾 6 个任务干等 7.5 分钟。

    ⚠️ 超时后**不等**那个线程：它是 daemon，自己会在 socket 读超时（跟着同一个
    timeout 走）之后收场，异常由 `_run` 吞掉，不会再冒出来打断别人。宁可留一个
    正在收尾的线程，也不能让整条出图队列陪它等。
    """
    box = {}

    def _run():
        try:
            box["text"] = fn()
        except Exception as exc:                    # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_run, name="audit-vision", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RuntimeError("超过 %g 秒还没返回，已放弃等待" % timeout)
    if "exc" in box:
        raise box["exc"]
    return box.get("text") or ""


def check(path, timeout=None, prompt=None):
    """审一张本地图（`image_out.prepare_for_send` 的产物）。

    timeout 不传 = `IMAGE_AUDIT_TIMEOUT`（默认 30 秒）。**必须短**：审核串在出图
    worker 上，一次挂死会堵住整条队列（见 `_call_with_deadline`）。超时按
    fail-closed 处理——进人工二审，图不丢、队列不堵。

    prompt 不传 = 用内置默认；`allow_send` 会把自定义的那份传进来。

    **不抛异常**：读不到文件、识图失败、解析不出来，一律返回**拦截**的 Verdict
    （allow=False，failed=True）。调用方不需要 try/except。
    """
    from app import vision

    if timeout is None:
        timeout = IMAGE_AUDIT_TIMEOUT

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

    last_error = None
    candidates = vision.audit_candidates()
    for index, (a_pid, a_model) in enumerate(candidates):
        try:
            # 审核不跟着管理页切，首选仍是 .env 的 provider；但首选中转站
            # 故障时切到下一家，避免一次临时故障让所有图片都无法发送。
            text = _call_with_deadline(
                lambda: vision.describe(data_url, timeout=timeout,
                                        prompt=prompt or _PROMPT,
                                        provider=a_pid, model=a_model),
                timeout)
        except Exception as exc:
            last_error = "识图 %s 失败：%s" % (a_pid, exc)
            if index + 1 < len(vision.audit_candidates()):
                log.warning("审核识图 %s 失败，降级到下一家：%s", a_pid, exc)
                continue
            return _fail_closed(last_error)

        v = parse_verdict(text)
        if v is not None:
            return v
        last_error = "识图 %s 回复解析不出来：%r" % (a_pid, text[:120])
        if index + 1 < len(vision.audit_candidates()):
            log.warning("审核识图 %s 返回无效结果，降级到下一家", a_pid)
            continue
        return _fail_closed(last_error)

    return _fail_closed(last_error or "没有可用的审核识图 provider")


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


def allow_send(path, agent_id, target, target_id, notify=True, meta=None):
    """发图前的总闸。**True = 可以发**。

    这是三个发图点唯一需要调用的函数：
    - 这个会话没开审核 → 直接 True（一次 settings 读，不发网络请求）；
    - 开了 → 审。**判违规和审核没生效都返回 False**（fail-closed），
      各自回一句提示；只有明确判合格才 True。

    notify=False（2026-10-05）：拦下时**不回话**——随机口令的静默重抽用，
    话术由 image_jobs 统一管（重抽期间出声会暴露内部机制，重抽尽才回一句
    不提审核的软话术）。默认 True = 老行为，判违规/没生效各自回话。

    meta（2026-10-06）：发图口顺手带过来的上下文（编号 / 渠道 / 种子 /
    caption / 提示词 / 原图名），只为**被拦时**进人工二审队列用——管理员在
    后台点「过审」，这张图要能原样补发回去（见 app/image_review.py）。
    不传也能用，只是那条记录少几项信息。

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
    # 人工二审（2026-10-06 用户要的）：这张图**留一份**到后台，管理员看着图
    # 点「过审」就按正常格式补发。最严白名单本来就吞掉不少擦边图，没有这条
    # 通道，误拦只能作废。图已经画好了，留着不费事。
    # 挂在这里而不是三个发图口各挂一次：那是本函数存在的意义（唯一闸门）。
    try:
        from app import image_review
        image_review.record(path, target, target_id, agent_id=agent_id,
                            verdict=verdict, meta=meta)
    except Exception as exc:
        # 补救通道自己炸了，绝不能把审核闸门也带下去。
        log.warning("进人工二审队列失败（图照旧按拦截处理）：%s", exc)
    if notify:
        _notify_blocked(target, target_id, verdict)
    return False

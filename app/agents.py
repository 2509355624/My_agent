"""
Agent 定义与解析

每个 agent 是 agents/ 下的一个自包含目录：

    agents/<id>/
        agent.json     配置（显示名、工具 / skills 白名单）
        prompt.md      角色人设
        session.jsonl  会话历史

设计要点：
1. 配置与人设都按文件 mtime 缓存 → 改 agent.json / prompt.md、甚至新建
   agent 目录，刷新页面即可生效，不需要重启服务（热加载）。
2. agent_id 会直接参与文件路径，必须先过 safe_agent_id()：既做字符白名单，
   也做路径穿越防线（解析后必须确实落在 AGENTS_DIR 的直接子目录里）。
3. 配置坏了、目录缺了都退回默认值，不让一个写错的 json 把整个服务带崩。
"""

import json
import os
import re

from app.config import (AGENTS_DIR, DEFAULT_AGENT_ID, NAI_ENABLED, PROVIDERS,
                        QQ_INTERJECT_COOLDOWN, QQ_INTERJECT_MIN_GAP)


# 字母数字开头，后跟字母数字 / 下划线 / 连字符；限长防超长文件名。
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# agent.json 的默认值。tools / skills 为 None 表示「不限制」。
# provider / model 为空串表示「继承 .env 里的全局默认」，这样没写过这两个
# 字段的 agent（以及所有老 agent.json）行为与从前完全一致。
# context_budget 为 0 表示「继承 .env 的 CONTEXT_BUDGET」，同上。
_DEFAULT_CONFIG = {
    "name": "",
    "description": "",
    "prompt": "",
    "prompt_file": "prompt.md",
    "tools": None,
    "skills": None,
    "provider": "",
    "model": "",
    "context_budget": 0,
}

# context_budget 的合法区间。低于下限压缩得太频繁（每轮都在摘要，反而更贵），
# 高于上限就等于没设；越界与写错一律归 0 → 继承全局，而不是报错拦住保存。
MIN_CONTEXT_BUDGET = 4000
MAX_CONTEXT_BUDGET = 1_000_000

DEFAULT_PROMPT_FILE = "prompt.md"

# {agent_id: (mtime, config)} / {agent_id: (mtime, text)}
_cfg_cache = {}
_persona_cache = {}


def clear_cache():
    """清掉配置与人设缓存。

    测试用；也留给外部在特殊情况下强制重载（正常情况下靠 mtime 自动失效）。
    """
    _cfg_cache.clear()
    _persona_cache.clear()
    _settings_cache.clear()


# ─── agent 级运行时设置（settings.json，管理页在线改的开关放这里）───
# 配置（agent.json）描述「这个 agent 是谁」，设置（settings.json）描述
# 「运行中想临时拨动的开关」。两者都按 mtime 热加载：管理页保存即写盘，
# 下一轮对话就读到新值，无需重启。保存接口另外会 clear_cache()，挡掉
# 「读 → 写 → 立刻再读」撞上 mtime 精度、命中旧缓存的情况。

SETTINGS_FILE = "settings.json"
_settings_cache = {}


def settings_path(agent_id):
    """settings.json 的路径；非法 id 返回 None。"""
    d = agent_dir(agent_id)
    return None if d is None else os.path.join(d, SETTINGS_FILE)


def load_settings(agent_id):
    """读该 agent 的运行时设置；文件不存在/坏掉时返回 {}（绝不抛错——
    它在每轮消息的热路径上，坏了宁可全用默认值也不能让消息处理挂掉）。"""
    path = settings_path(agent_id)
    if path is None:
        return {}
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _settings_cache.get(agent_id)
    if cached and cached[0] == mt:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _settings_cache[agent_id] = (mt, data)
    return data


def save_settings(agent_id, settings):
    """覆盖写入 settings.json，返回是否成功。管理页的写入口。"""
    path = settings_path(agent_id)
    if path is None or not isinstance(settings, dict):
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    _settings_cache.pop(agent_id, None)
    return True


def image_gen_allowed(agent_id, target, target_id):
    """QQ 侧生图能不能用。返回 (True, "") 或 (False, 拒绝理由)。

    两层闸都在 settings.json（热生效，不用重启）：
    - image_gen: 全局总闸，False = 这个 agent 的 QQ 会话里一律不能生图；
    - image_gen_muted: 单群名单，「关」的语义——名单里的群不能生图。
    只在 QQ 会话里管（target 非 None 才有绑定）；网页端对话不受限。
    """
    s = load_settings(agent_id)
    if s.get("image_gen") is False:
        return False, "生图功能已被管理员全局关闭"
    if target == "group" and str(target_id) in (s.get("image_gen_muted") or []):
        return False, "生图功能在本群已被管理员关闭"
    return True, ""


def at_only_groups(agent_id):
    """「只认 @」的群名单（settings.json 的 at_only_groups）。

    掐掉的是**免 @ 的关键词**那条路：名单里的群，`QQ_GROUP_KEYWORDS` 那套呼叫词
    一律不认，只有真 @ 到才回。全局 `QQ_GROUP_AT_ONLY` 管的是「所有群要不要走
    全量模式」，这里给单个群**加严**——某个群嫌它话多，要求必须点名。

    与 `interject_muted` 是两件事，别混：那条管「能不能主动开口」，这条管
    「叫它的时候要不要 @」。某群两个都开 = 只有 @ 才有反应。
    """
    s = load_settings(agent_id)
    return set(str(x) for x in (s.get("at_only_groups") or []))


# ─── 私聊每日生图额度 ────────────────────────────────

# 默认每人每天 10 张（2026-09-30 用户拍板：「私聊除非我给白名单，不然单人每天
# 最多生成 10 个图」）。**默认是开的**——这条需求本身就是要它生效，写成默认关
# 等于上线后还得手动去管理页点一下。
PRIVATE_IMAGE_DAILY_LIMIT_DEFAULT = 10


def private_image_daily_limit_raw(agent_id):
    """settings 里存的**原始**上限，不看开关。管理页回显输入框用它——开关关掉时
    也得能看见原来的数字，否则「临时放开一下」就变成「把配置弄丢了」。

    写坏了回落默认 10 而不是回落「不限」：宁可比对方想的多扣几张，也别因为一个
    手滑的字符串把限流悄悄变成全开。
    """
    s = load_settings(agent_id)
    v = s.get("private_image_daily_limit")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return PRIVATE_IMAGE_DAILY_LIMIT_DEFAULT
    return int(v)


def private_image_daily_limit(agent_id):
    """**生效**的私聊每人每天生图上限。返回 0 表示不限。

    - `private_image_quota_on` 为 False ⇒ 0（管理员临时关掉限流，数字留着）；
    - 否则就是 settings 里的数字（0 / 负数 = 不限）。
    """
    s = load_settings(agent_id)
    if s.get("private_image_quota_on") is False:
        return 0
    return private_image_daily_limit_raw(agent_id)


def private_image_quota_whitelist(agent_id):
    """免额名单：这些 QQ 在私聊里生图不限量。

    **刻意不复用 private_whitelist**（用户 2026-09-30 选的）：那个名单管的是
    「谁能私聊」，一旦哪天把它打开，名单里的人就会顺带变成「生图不限量」——
    两件事的语义必须分开，否则以后想临时放开私聊都不敢动。
    """
    s = load_settings(agent_id)
    return set(str(x) for x in (s.get("private_image_quota_whitelist") or []))


def image_quota_allowed(agent_id, target, target_id):
    """私聊每日生图额度闸。返回 (True, "") 或 (False, 拒绝理由)。

    **只对私聊生效**：群聊、网页端一律放行（用户 2026-09-30 明确只要私聊限流）。
    白名单里的人不限量。限流关掉（上限 0）时整段是空操作。

    这里只**读**计数，不扣——扣额在 `generate_image` 里真正接单之后
    （见 app/image_quota.py 的 charge），否则「检查」这一步自己就把额度吃了。
    """
    if target != "private":
        return True, ""
    limit = private_image_daily_limit(agent_id)
    if limit <= 0:
        return True, ""
    if str(target_id) in private_image_quota_whitelist(agent_id):
        return True, ""
    from app import image_quota
    used = image_quota.used(target_id)
    if used >= limit:
        return False, ("今天私聊生图的额度用完了（%d/%d 张）" % (used, limit))
    return True, ""


def image_quota_line(agent_id, target, target_id):
    """给模型看的一行「这个私聊会话现在的生图额度状态」；非私聊返回空串。

    **为什么需要它**（2026-10-01 用户报「加了白名单，AI 还说我限额了」）：
    额度拒一次之后，那句「今天私聊生图的额度用完了」会作为 tool_result 永久
    留在会话历史里。管理员随后把人加进免额名单，**模型不知道**——它照着历史
    里的旧拒绝继续回话，而且因为那句里写着「别再重试」，它连工具都不再调一次
    （实测那几轮日志全是 `工具=-`，一次都没重新检查）。结果就是「白名单明明
    生效了，机器人却咬死说限额」。

    额度闸本身没错（`image_quota_allowed` 确实放行白名单），错在模型手里
    没有**当下**的事实。这行跟着 extra_context 每轮现取现用、出流即弃
    （与 `[最近生图]` 同一通道），给它一个当前锚点。

    **只在私聊输出**：群聊、网页端一律不限流，不该多出这一行白占尾巴
    （用户 2026-10-01 明确「群聊其实没有生图限制，不用管」）。

    三种状态**都要说话**，包括限流被关掉的那种。早先 `limit <= 0` 直接返回
    空串，理由是「没什么可说就别占尾巴」——那正好把上面这个 bug 又开了一个
    口子：管理员为了救人临时把限流关掉，历史里那句「额度用完」就成了**唯一**
    还有得看的信号，模型继续说限额。空串在这里不是「省字」，是「沉默」，而
    沉默对模型来说等于没有反驳证据。

    结尾那句「以本行为准」不是客套：模型手里同时有这行（每轮新）和历史里的
    旧拒绝（永不消失），得明确告诉它该信哪个，否则它常常挑旧的说。
    """
    if target != "private":
        return ""
    tail = ("（这行是当前状态、每轮都重新算，**以它为准**；"
            "历史里那句「额度用完」若与它冲突，已经不作数了。）")
    limit = private_image_daily_limit(agent_id)
    if limit <= 0:
        return ("[私聊生图额度] 管理员**已经把限流关掉**（或还没设上限），"
                "这个人不限张数，直接画。" + tail)
    if str(target_id) in private_image_quota_whitelist(agent_id):
        state = "这个人在免额名单里，**不限量**，可以直接画。"
    else:
        from app import image_quota
        used = image_quota.used(target_id)
        if used >= limit:
            state = ("今天已用 %d/%d 张，**已经用满**，要等**明天 00:00**（本地"
                     "日期一翻篇）才恢复；别再调 generate_image，"
                     "直接告诉对方明天再来。" % (used, limit))
        else:
            state = ("今天已用 %d/%d 张，**还能画 %d 张**，现在可以接单"
                     "（额度**接单就扣**，不是出图才扣）。"
                     % (used, limit, limit - used))
            from app import image_jobs
            inflight = image_jobs.inflight_count(target, target_id)
            if inflight:
                state += ("其中 %d 张还在排队/生成中，**图还没发出去**——"
                          "对方说「没收到图」时先想这几张，别当没画过。"
                          % inflight)
    return "[私聊生图额度] " + state + tail


def private_image_quota_info(agent_id, target_id):
    """管理页私聊行要显示的一条：{"limit": 10, "used": 3, "whitelisted": False}。

    limit 为 0 表示不限量（管理页显示「不限」而不是「0 张」）。
    """
    from app import image_quota
    return {"limit": private_image_daily_limit(agent_id),
            "used": image_quota.used(target_id),
            "whitelisted": str(target_id) in private_image_quota_whitelist(agent_id)}


def nai_allowed(agent_id, target, target_id):
    """NAI（NovelAI，群主独立 token）能不能用。返回 (True, "") 或 (False, 理由)。

    三层闸（settings.json 热生效，不用重启）：
    - config.NAI_ENABLED：.env 硬总闸（默认开，纯紧急熔断用）；
    - nai_enabled：settings 全局总闸（默认关，管理页开）；
    - 白名单（各自独立、默认空）：群聊看 nai_groups、私聊看 nai_private。
    只支持 QQ 会话（target 为 "group" / "private"）；网页端一律不可用——token 只给
    名单里的群 / 人，别的会话连碰的机会都没有。
    """
    if not NAI_ENABLED:
        return False, "NAI 已被全局紧急关闭（.env NAI_ENABLED=false）"
    s = load_settings(agent_id)
    if not s.get("nai_enabled"):
        return False, "NAI 未在本 agent 启用（管理页全局开关未开）"
    if target == "group":
        if str(target_id) not in (s.get("nai_groups") or []):
            return False, "本群未开通 NAI（管理页未把本群加入白名单）"
        return True, ""
    if target == "private":
        if str(target_id) not in (s.get("nai_private") or []):
            return False, "你还没开通 NAI（管理页未把你的 QQ 加入私聊白名单）"
        return True, ""
    return False, "NAI 仅支持 QQ 使用"


# 对外发图的编码格式。只决定「发出去那一张」怎么编码——ComfyUI output
# 里的原图永远不动（见 app/image_out.py）。png 更大但无损，jpg 省流量。
IMAGE_SEND_FORMATS = ("jpg", "png")
IMAGE_SEND_FORMAT_DEFAULT = "jpg"


def image_send_format(agent_id, target, target_id):
    """这个 QQ 会话发图用哪种格式，返回 "jpg" 或 "png"（不会是 None）。

    settings.json 三层取值（热生效，不用重启）：
    - image_send_format_overrides[会话号]：单会话覆盖（管理页行里的「格式」按钮）；
    - image_send_format：全局总开关；
    - 都没有时回落 jpg —— 和加这个开关之前的行为一致。

    **群和私聊都认覆盖**，共用同一张表，键就是会话号（群号或对方 QQ 号）。
    这跟 image_gen_muted 只认群的口径不同：格式是给对方看的，私聊里对方
    一样嫌 jpg 糊，所以单聊也得能设。两种号同出一个号池，理论上可能撞号，
    但一个 agent 的会话只有几十个，撞上的概率可以忽略——不值得为它多造
    一层前缀去换复杂度。

    target 传 None 表示只问全局值（网页端列表接口拿总开关用）。
    值非法（比如手改坏了 settings.json）一律当没设——绝不把野值透给 PIL 的
    save(format=...)，那会直接抛错把图卡住。
    """
    s = load_settings(agent_id)
    if target is not None:
        overrides = s.get("image_send_format_overrides")
        if isinstance(overrides, dict):
            v = overrides.get(str(target_id))
            if v in IMAGE_SEND_FORMATS:
                return v
    v = s.get("image_send_format")
    if v in IMAGE_SEND_FORMATS:
        return v
    return IMAGE_SEND_FORMAT_DEFAULT


def image_audit_enabled(agent_id, target, target_id):
    """这个 QQ 会话发图前要不要过一道 NSFW 审核（app/image_audit.py）。

    settings.json 三层取值（热生效，不用重启）：
    - image_audit_overrides[会话号]：单会话覆盖（管理页行里的「审核」按钮）；
    - **按会话类型分的两个总开关**：群聊看 `image_audit_groups`、
      私聊看 `image_audit_private`；
    - 都没有时回落 **False** —— 默认全关，跟加这个功能之前的行为一致。

    **总开关按群/私聊分开**（2026-10-01 用户要求）：只给群开、私聊不开（或反过来）
    是常见需求，合成一个的话每次都得再去按会话类型逐个点，很麻烦。

    群和私聊**共用同一张覆盖表**（键就是群号 / 对方 QQ 号），跟
    image_send_format 同口径，理由也一样：审核拦的是「这张图能不能给对面看」，
    私聊里对面一样有这个问题。两种号同出一个号池、理论上会撞号，但一个 agent
    的会话只有几十个，撞上的概率可以忽略。

    **要问「两个总开关现在各是什么」用 `image_audit_globals()`**，
    别拿 target=None 来问——那个签名是「按会话查生效值」，没有会话就没有答案。
    """
    s = load_settings(agent_id)
    overrides = s.get("image_audit_overrides")
    if isinstance(overrides, dict):
        v = overrides.get(str(target_id))
        if isinstance(v, bool):
            return v
    return _audit_flag(s, target)


# 审核总开关的两个作用域。值 = settings.json 里 `image_audit_<后缀>` 的后缀，
# 也等于会话的 kind（群聊 "group" / 私聊 "private"），一处定义别处引用。
IMAGE_AUDIT_SCOPES = ("group", "private")

# scope → settings.json 的键名。键名用复数（groups / private），因为一个是
# 「所有群」一个是「所有私聊」，别和会话 kind 的单数混淆。
_AUDIT_KEYS = {"group": "image_audit_groups", "private": "image_audit_private"}


def _audit_flag(settings, scope):
    """读一个总开关。**只认真正的布尔**——`bool()` 会把字符串 "yes" 当 True，
    手改坏了 settings.json 就会变成「以为关着其实开着」。"""
    v = settings.get(_AUDIT_KEYS[scope])
    return v if isinstance(v, bool) else False


def image_audit_globals(agent_id):
    """管理页顶部两个总开关的当前值，返回 {"group": bool, "private": bool}。

    跟 image_send_format 那边的 `target=None` 写法不同：那个只有一个总开关，
    塞进同一个签名里没问题；这里有两个，塞不下，所以单开一个函数。
    """
    s = load_settings(agent_id)
    return {sc: _audit_flag(s, sc) for sc in IMAGE_AUDIT_SCOPES}


# 自定义审核提示词的 settings.json 键名。
_AUDIT_PROMPT_KEY = "image_audit_prompt"


def image_audit_prompt(agent_id):
    """自定义的审核提示词；没设 / 设成空白 → 返回 ""（调用方回落内置默认）。

    2026-10-01 用户要求「审核的提示词我要能自己改」。原先它写死在
    app/image_audit.py 的 `_PROMPT` 里，想调一句就得改代码 + 重启适配层。

    **只认非空字符串**：手改坏了存成 null / 数字 / 空串，一律当「没设」，
    回落内置默认。绝不因为配置写坏就把闸门放空——这条比「尊重用户输入」重要。
    """
    v = load_settings(agent_id).get(_AUDIT_PROMPT_KEY)
    return v.strip() if isinstance(v, str) else ""


# 自定义**识图（通用读图）提示词**的 settings.json 键名。
_VISION_PROMPT_KEY = "vision_prompt"


def vision_prompt(agent_id):
    """自定义的识图提示词；没设 / 设成空白 → 返回 ""（调用方回落内置默认）。

    2026-10-02 用户要求：「识图模型老是分析不清楚图片，我要能在管理页自己改
    通用那份的提示词。」原先它写死在 app/vision.py 的 `_PROMPT` 里，想调一句
    就得改代码 + 重启。

    跟 `image_audit_prompt` 同款口径：**只认非空字符串**——手改坏了存成
    null / 数字 / 空串一律当「没设」，回落内置默认。这里回落是**安全的**
    （内置那份是能用、也只是不够细），不像审核那边回落会改变闸门语义。

    ⚠️ 只管**通用读图**这一条链路（agent 收图 → 转文字）。生图前的 NSFW
    审核提示词在 `image_audit_prompt`，表情包打标签写死在 `stickers._tag`，
    两处都不吃这个开关。
    """
    v = load_settings(agent_id).get(_VISION_PROMPT_KEY)
    return v.strip() if isinstance(v, str) else ""


# 主动发言冷却的合法范围（秒）。0 = 不限频；上限防手滑输成天文数字。
INTERJECT_COOLDOWN_MIN = 0
INTERJECT_COOLDOWN_MAX = 3600


def _clamp_cooldown(v):
    """收敛成 0~3600 的整数；不是数字返回 None（让调用方回落默认）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return max(INTERJECT_COOLDOWN_MIN, min(INTERJECT_COOLDOWN_MAX, int(v)))


def interject_cooldown(agent_id, group_id):
    """这个群主动发言的冷却秒数（热生效，不用重启）。

    settings.json 三层取值：
    - interject_cooldown_overrides[群号]：单群覆盖（管理页单群设置）；
    - interject_cooldown：全局值；
    - 都没有时回落 .env 的 QQ_INTERJECT_COOLDOWN（重启级默认）。
    settings 读的是热路径，文件坏了 load_settings 返回 {}，这里自然回落默认。
    """
    s = load_settings(agent_id)
    overrides = s.get("interject_cooldown_overrides")
    v = overrides.get(str(group_id)) if isinstance(overrides, dict) else None
    if v is None:
        v = s.get("interject_cooldown")
    clamped = _clamp_cooldown(v)
    if clamped is None:
        clamped = _clamp_cooldown(QQ_INTERJECT_COOLDOWN) or 0
    return clamped


# 触发概率：0~100 的整数百分比。0 = 从不主动开口；100 = 每批都去问模型。
# 默认 12%（≈1/8）——主动开口是点缀，绝大多数消息不该惊动模型。
DEFAULT_INTERJECT_CHANCE = 12

# 判断间隔的合法范围（秒）。0 = 不限（但还有概率门在前面挡着）。
INTERJECT_GAP_MIN = 0
INTERJECT_GAP_MAX = 3600


def _clamp_percent(v):
    """收敛成 0~100 的整数；不是数字返回 None（让调用方回落默认）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return max(0, min(100, int(v)))


def interject_chance(agent_id, group_id):
    """这个群「收到消息后去问一次模型」的概率（百分比整数，热生效）。

    三层取值与冷却同型：interject_chance_overrides[群号] > interject_chance
    > DEFAULT_INTERJECT_CHANCE。概率门挡掉的批次压根不调模型，这是省调用
    最有效的一道闸——它挡在冷却和判断之前。
    """
    s = load_settings(agent_id)
    overrides = s.get("interject_chance_overrides")
    v = overrides.get(str(group_id)) if isinstance(overrides, dict) else None
    if v is None:
        v = s.get("interject_chance")
    clamped = _clamp_percent(v)
    return DEFAULT_INTERJECT_CHANCE if clamped is None else clamped


def interject_min_gap(agent_id, group_id):
    """两次判断之间的最小秒数（热生效，不用重启）。

    判断本身也是一次 API 调用，群聊刷屏时不能每条都问。三层取值同冷却：
    interject_min_gap_overrides[群号] > interject_min_gap > .env 的
    QQ_INTERJECT_MIN_GAP。0 = 不做这道闸（只剩概率门挡着）。
    """
    s = load_settings(agent_id)
    overrides = s.get("interject_min_gap_overrides")
    v = overrides.get(str(group_id)) if isinstance(overrides, dict) else None
    if v is None:
        v = s.get("interject_min_gap")
    if v is None:
        v = QQ_INTERJECT_MIN_GAP
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return _clamp_gap(QQ_INTERJECT_MIN_GAP)
    return _clamp_gap(v)


def _clamp_gap(v):
    return max(INTERJECT_GAP_MIN, min(INTERJECT_GAP_MAX, int(v)))


def safe_agent_id(agent_id):
    """校验并规范化 agent_id；非法返回 None。

    agent_id 会被拼进文件路径，所以这里既是格式白名单（挡掉奇怪字符），
    也是路径穿越防线：realpath 解析后必须确实是 AGENTS_DIR 的直接子目录。
    """
    if not isinstance(agent_id, str):
        return None
    aid = agent_id.strip()
    if not _AGENT_ID_RE.match(aid):
        return None
    root = os.path.realpath(AGENTS_DIR)
    target = os.path.realpath(os.path.join(root, aid))
    if os.path.dirname(target) != root:
        return None
    return aid


def agent_dir(agent_id):
    """agent 目录绝对路径（不保证存在）；非法 id 返回 None。"""
    aid = safe_agent_id(agent_id)
    if aid is None:
        return None
    return os.path.join(AGENTS_DIR, aid)


# 子会话 key 的字符白名单：字母数字开头，后跟字母数字 / 下划线 / 连字符。
# 不含路径分隔符，也不含点（挡掉 ".."），所以拼进路径不会逃逸。
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def safe_session_key(key):
    """校验子会话 key；非法返回 None。

    用于 QQ 这类「一个 agent 下挂多条独立会话线」的场景（每个私聊用户 /
    每个群各一条），key 会被拼进文件名，所以做白名单校验。字符集本身已经
    排除了路径分隔符和点，拼出来不可能逃出 sessions/ 目录，无需再做
    realpath —— agent_id 那一层的穿越防线在 safe_agent_id 里。
    """
    if not isinstance(key, str):
        return None
    k = key.strip()
    if not _SESSION_KEY_RE.match(k):
        return None
    return k


def session_file(agent_id, key=None):
    """该 agent 的会话文件路径（不保证存在，写入时会自动建目录）。

    key 为 None 时是 agent 的主会话（session.jsonl），网页端用的就是这条。
    传 key 时落到 sessions/<key>.jsonl，供一个 agent 承载多条互不相干的
    会话线使用（QQ 适配层按「私聊用户 / 群」分线）。key 非法则退回主会话
    —— 存不下来比抛异常打断一轮对话更糟。
    """
    aid = safe_agent_id(agent_id) or DEFAULT_AGENT_ID
    if key:
        safe = safe_session_key(key)
        if safe is not None:
            return os.path.join(AGENTS_DIR, aid, "sessions", safe + ".jsonl")
    return os.path.join(AGENTS_DIR, aid, "session.jsonl")


# 主会话在管理接口里的表示。真正的主会话文件是 session.jsonl（不在
# sessions/ 目录里），这里用一个合法、好记且不会与 QQ 侧相撞的名字代替
# ——QQ 的 key 一律带 group_ / private_ 前缀。
MAIN_SESSION_KEY = "main"


def _session_kind(key):
    """从 key 推断这条会话线是什么，供界面分类显示。"""
    if key.startswith("private_"):
        return "private", key[len("private_"):]
    if key.startswith("group_"):
        return "group", key[len("group_"):]
    return "other", key


def _session_stat(path):
    """一条会话线的体量：消息条数 / 字节数 / 最后修改时间；读不到返回 None。

    首行是 system 头（人设锚点），不算「聊过的内容」，从条数里扣掉。
    """
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    lines = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines += 1
    except (OSError, UnicodeDecodeError):
        pass
    return {"messages": max(lines - 1, 0), "size": size, "mtime": mtime}


def list_sessions(agent_id):
    """列出该 agent 的所有会话线：主会话 + sessions/ 下每一条。

    只读本地文件，不碰网络——群名/昵称要问 QQ 协议端，慢且可能不可用，
    由上层按需补，补不上也不影响列表本身。
    最近有活动的排最前：想重置哪个群，通常就是刚在说话的那个。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return []

    items = []
    stat = _session_stat(session_file(aid))
    if stat:
        items.append(dict(stat, key=MAIN_SESSION_KEY, kind="main",
                          target_id="", name="主会话（网页端）"))

    sess_dir = os.path.join(AGENTS_DIR, aid, "sessions")
    try:
        names = sorted(os.listdir(sess_dir))
    except OSError:
        names = []
    for fname in names:
        if not fname.endswith(".jsonl"):
            continue
        key = fname[:-len(".jsonl")]
        # safe_session_key 顺带把点开头的杂物（备份目录之类）挡在外面
        if safe_session_key(key) is None:
            continue
        stat = _session_stat(os.path.join(sess_dir, fname))
        if stat is None:
            continue
        kind, target_id = _session_kind(key)
        items.append(dict(stat, key=key, kind=kind, target_id=target_id,
                          name=""))

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def recent_group_stats(agent_id):
    """群聊观察缓存的体量：{群号: {messages, size, mtime}}。

    群消息一律先记进 recent/（旁观记忆），但只有真正回复过的群才有
    sessions/ 会话线。管理页要把「收到过消息但从没回过」的群也列出来
    （不然开关都找不到它们），就靠这份统计补行。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return {}
    d = os.path.join(AGENTS_DIR, aid, "recent")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return {}
    stats = {}
    for fname in names:
        if not fname.startswith("group_") or not fname.endswith(".jsonl"):
            continue
        gid = fname[len("group_"):-len(".jsonl")]
        if not gid.isdigit():
            continue
        path = os.path.join(d, fname)
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
            with open(path, "r", encoding="utf-8") as f:
                lines = sum(1 for line in f if line.strip())
        except (OSError, UnicodeDecodeError):
            continue
        # recent 缓存没有 system 头，行数就是消息条数（别复用 _session_stat，
        # 那个会扣首行）
        stats[gid] = {"messages": lines, "size": size, "mtime": mtime}
    return stats


def delete_session(agent_id, key):
    """删除一条会话线，返回 (是否成功, 错误信息)。

    不可逆：删掉的就是聊天记录本身，所以调用方必须二次确认（管理页拦一道）。

    删完不需要任何收尾动作：下一轮对话加载时发现会话缺 system 头会自动
    重建，而人设、白名单、触发词都在 agent.json / prompt.md 里，不受影响
    ——这正是「重置这个群」想要的效果。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return False, "非法的 agent id"

    if key == MAIN_SESSION_KEY:
        path = session_file(aid)
    else:
        safe = safe_session_key(key)
        if safe is None:
            return False, "非法的会话 key"
        path = session_file(aid, safe)

    if not os.path.exists(path):
        return False, "会话不存在"
    try:
        os.remove(path)
    except OSError as e:
        return False, "删除失败：" + str(e)
    return True, ""


def resolve(agent_id):
    """把请求里的 agent_id 解析成可用的 agent id。

    非法、缺失、目录不存在 → 兜底到 DEFAULT_AGENT_ID。默认 agent 即使目录
    还没建也能用：写入会话时自动创建，人设为空则用默认角色定义。
    """
    aid = safe_agent_id(agent_id)
    if aid and os.path.isdir(os.path.join(AGENTS_DIR, aid)):
        return aid
    return DEFAULT_AGENT_ID


def _normalize(cfg):
    """规范化配置：tools / skills 收敛成 None 或去重后的字符串列表。"""
    out = dict(cfg)

    for key in ("tools", "skills"):
        val = out.get(key)
        if val is None:
            continue
        if not isinstance(val, (list, tuple)):
            # 写成了字符串之类的，视为「不限制」，比默默当成白名单更安全
            out[key] = None
            continue
        seen, items = set(), []
        for x in val:
            if isinstance(x, str) and x.strip() and x.strip() not in seen:
                seen.add(x.strip())
                items.append(x.strip())
        out[key] = items

    # provider / model 留空串 = 继承 .env 的全局默认。
    # provider 写错（不在 PROVIDERS 里）一律清空而不是原样保留：留着会让请求
    # 带着一个不存在的名字走到 llm 层，再被静默回退到全局默认，界面显示的和
    # 实际在用的就对不上了。model 则刻意不做校验——模型名由各家平台决定，
    # 写错时让它带着 404 报出来（llm 层有专门的接入点提示），比悄悄回退有用。
    for key in ("provider", "model"):
        val = out.get(key)
        out[key] = val.strip() if isinstance(val, str) else ""
    _prov = out["provider"].lower()
    out["provider"] = _prov if _prov in PROVIDERS else ""

    # context_budget：0 = 继承全局。只接受区间内的整数——写错（空串、非数字、
    # 越界）一律归 0，不让一个手滑的数字把压缩彻底关掉、或调到每轮都摘要。
    # 容忍字符串形式的数字（"32000"），手改 json 时不必纠结类型。
    try:
        n = int(out.get("context_budget"))
    except (TypeError, ValueError):
        n = 0
    if n and not (MIN_CONTEXT_BUDGET <= n <= MAX_CONTEXT_BUDGET):
        n = 0
    out["context_budget"] = n

    if not isinstance(out.get("prompt_file"), str) or not out["prompt_file"].strip():
        out["prompt_file"] = DEFAULT_PROMPT_FILE
    if not isinstance(out.get("prompt"), str):
        out["prompt"] = ""
    for key in ("name", "description"):
        if not isinstance(out.get(key), str):
            out[key] = ""
    return out


def agent_config(agent_id):
    """读取 agent.json（按 mtime 热加载），缺失字段用默认值补齐。

    读不到 / 解析失败都返回默认配置，而不是报错：agent 目录里只有
    session.jsonl 也应该能用（等价于全部工具、全部 skills）。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return dict(_DEFAULT_CONFIG)

    path = os.path.join(AGENTS_DIR, aid, "agent.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return dict(_DEFAULT_CONFIG)

    cached = _cfg_cache.get(aid)
    if cached and cached[0] == mtime:
        return cached[1]

    cfg = dict(_DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in _DEFAULT_CONFIG:
                if k in raw:
                    cfg[k] = raw[k]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        cfg = dict(_DEFAULT_CONFIG)

    cfg = _normalize(cfg)
    _cfg_cache[aid] = (mtime, cfg)
    return cfg


def persona_path(agent_id):
    """人设文件的绝对路径；agent_id 非法时返回 None。

    prompt_file 不允许带路径分隔符——否则在 agent.json 里写一个 ../.. 就能
    指到 agent 目录外面去。读取与保存两边都必须走这个函数，防穿越规则才
    不会各写一份、日后只改了一处。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return None
    fname = agent_config(aid)["prompt_file"]
    if os.path.basename(fname) != fname:
        fname = DEFAULT_PROMPT_FILE
    return os.path.join(AGENTS_DIR, aid, fname)


def persona_text(agent_id):
    """人设文本：优先 prompt_file（默认 prompt.md），其次 agent.json 的 prompt 字段。

    文件不存在时返回空串，调用方用默认角色定义兜底。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return ""

    cfg = agent_config(aid)
    path = persona_path(aid)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return cfg.get("prompt") or ""

    cached = _persona_cache.get(aid)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except (OSError, UnicodeDecodeError):
        text = cfg.get("prompt") or ""

    _persona_cache[aid] = (mtime, text)
    return text


def _atomic_write_text(path, text):
    """临时文件 + fsync + os.replace 的原子写。

    和 memory.save_history 同一套路：直接 open("w") 覆盖时，写到一半进程
    被杀就留下半截文件，下次读直接解析失败。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def agent_raw_config(agent_id):
    """磁盘上 agent.json 的原始内容，不做补默认值与规范化。

    给「后台改配置」用：读-改-写必须以磁盘原文为底，只覆盖用户真正改过的
    字段。若拿规范化后的 dict 回写，那些界面上没暴露的字段（tools / skills
    等）会被默认值冲掉。目录缺失或文件损坏时返回空 dict。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return {}
    path = os.path.join(AGENTS_DIR, aid, "agent.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_agent_config(agent_id, cfg):
    """原子写入 agent.json，并清掉配置缓存。

    清缓存不是可选项：mtime 精度有限，「读 → 写 → 立刻再读」有可能命中旧
    缓存，表现就是「点了保存但没生效」。配置本身很小，重读一次可忽略。
    """
    aid = safe_agent_id(agent_id)
    if aid is None or not isinstance(cfg, dict):
        return False
    d = os.path.join(AGENTS_DIR, aid)
    os.makedirs(d, exist_ok=True)
    _atomic_write_text(os.path.join(d, "agent.json"),
                       json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    clear_cache()
    return True


def save_persona(agent_id, text):
    """原子写入人设文件（默认 prompt.md），并清掉人设缓存。"""
    aid = safe_agent_id(agent_id)
    if aid is None or not isinstance(text, str):
        return False
    path = persona_path(aid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_text(path, text)
    _persona_cache.pop(aid, None)
    return True


# 生图方法文档（可选）。单独成文件而不是写在 prompt.md 里，理由见
# image_guide_text 的注释。
IMAGE_GUIDE_FILE = "image_guide.md"


def image_guide_path(agent_id):
    """生图方法文档的绝对路径；agent_id 非法时返回 None。

    路径写死成固定文件名（不像 prompt_file 那样可在 agent.json 里配）：
    它对应的是 build_stable_prompt 里一个**固定段落**，不参与人设覆盖，
    配来配去只会让「哪些会话拿得到生图方法」变得不可预测。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return None
    return os.path.join(AGENTS_DIR, aid, IMAGE_GUIDE_FILE)


def image_guide_text(agent_id):
    """生图方法文档正文；文件不存在返回空串（多数 agent 都没有）。

    为什么要单独一个文件，而不是跟着人设写在 prompt.md 里：
    **prompt.md 整份就是 build_stable_prompt 的 Role 段**，而会话级自定义
    人设（persona_override）是**整份顶替** Role 的。写在一起的后果是
    「一旦给某条会话设了自定义人设，生图方法连同人设一起被顶掉」——
    生图质量莫名其妙地掉，而且极难查（会话越"定制"越容易中招）。
    抽成独立文件后它进的是**独立 section**，任何会话都拿得到。
    """
    path = image_guide_path(agent_id)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def revision(agent_id):
    """该 agent 配置的版本串（agent.json、人设文件、生图方法文档的 mtime 拼接）。

    给上层缓存用：system prompt 的稳定层把版本串算进指纹，于是改了配置
    或人设就自动重建，不用重启服务。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return "-"
    cfg = agent_config(aid)
    parts = [aid]
    for fname in ("agent.json", cfg["prompt_file"], IMAGE_GUIDE_FILE):
        path = os.path.join(AGENTS_DIR, aid, fname)
        try:
            parts.append(str(os.path.getmtime(path)))
        except OSError:
            parts.append("0")
    return "|".join(parts)


def allows_tool(agent_id, name):
    """该 agent 是否允许使用某个工具（tools 为 None 表示全部允许）。"""
    limit = agent_config(agent_id)["tools"]
    return limit is None or name in limit


def allows_skill(agent_id, name):
    """该 agent 是否允许看到某个 skill（skills 为 None 表示全部允许）。"""
    limit = agent_config(agent_id)["skills"]
    return limit is None or name in limit


def list_agents():
    """列出所有 agent（默认 agent 排最前，其余按 id 排序）。

    每次都实时扫目录、不缓存：新建一个 agent 目录，刷新页面就能用。
    """
    if not os.path.isdir(AGENTS_DIR):
        return []

    items = []
    for name in os.listdir(AGENTS_DIR):
        path = os.path.join(AGENTS_DIR, name)
        if not os.path.isdir(path) or safe_agent_id(name) is None:
            continue
        cfg = agent_config(name)
        items.append({
            "id": name,
            "name": cfg["name"] or name,
            "description": cfg["description"],
            "tools": cfg["tools"],
            "skills": cfg["skills"],
            # 空串 = 继承全局默认。管理页用它标出「谁单独配过模型」
            "provider": cfg["provider"],
            "model": cfg["model"],
            # 0 = 继承全局预算，同样是「谁单独配过」的标记
            "context_budget": cfg["context_budget"],
        })

    items.sort(key=lambda a: (a["id"] != DEFAULT_AGENT_ID, a["id"]))
    return items

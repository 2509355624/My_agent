"""出图 caption 与投递。

从 `app/image_jobs.py` 抽出：caption 是「编号 · 分辨率 · 渠道 · seed」那一行，
`_send_image` 负责先出副本、再过审核、最后回原会话。队列与 worker 不在这里。
"""

import logging

from app.config import QQ_AGENT_ID

log = logging.getLogger("image_jobs")


def _caption(tag, path, skill, image_out, seed=None, anima_draw=None):
    """图上那行字：`编号 · 分辨率 · 渠道 · seed 数字`。

    编号**必须在最前且原样**——它是群友引用那条消息时被正则抠回来的锚点
    （见 app/features/image/log.py，TAG_RE 只认 `HT-` 开头那一串）。后面几项纯粹是给
    人看的附注，读不出来就少一项，不影响编号。

    为什么要在这行里带 seed（2026-10-02 用户提，需求是「图有多手多脚，拿种子
    改提示词重画」）：caption 跟图片在**同一条消息**里，群友引用它时整行正文
    回到模型眼前——种子于是自己就回来了，不用模型先猜编号再去查账本。这是
    唯一一条不依赖模型记忆的通路（理由同 image_log 模块开头那段）。

    没种子时（NAI 那种云端图 / 老调用方）不多写那一段，整行与从前一致。

    anima_draw（2026-11-13，ANIMA 随心配抽卡参数）：有值就在 seed 之后追加
    一行「抽卡：模型 A / 模型 B · LoRA×5 简写」，让用户一眼看到这次随机抽到
    什么画风。⚠️ 文件名里只有字母数字、`-`、`.` 才安全（这行会过
    `qq_api.to_qq_text` 做 Markdown 降级），这里做一层清洗。

    ⚠️ 每一项都只放**字母数字和 `-`**：这行会过一遍 `qq_api.to_qq_text` 做
    Markdown 降级，`_` `*` 反引号 那些会被吃掉或改变排版。
    """
    if not tag:
        return ""
    bits = [str(tag)]
    size = image_out.local_size(path)
    if size:
        bits.append(size)
    if skill:
        bits.append(str(skill))
    if seed is not None:
        bits.append("seed %d" % int(seed))
    line = " · ".join(bits)
    # 随心配抽卡参数（2026-11-13）：抽卡结果贴出来，人看得懂、引用也能复刻。
    # 这里**不做激进清洗**（保留下划线/中文/空格）——它们是模型/LoRA 的正式
    # 文件名，去掉分隔符反而没法照着复刻。只剥 `.safetensors` 后缀。
    if anima_draw:
        models = anima_draw.get("models") or []
        loras = anima_draw.get("loras") or []
        strip = lambda s: str(s).replace(".safetensors", "").strip()
        clean_m = [strip(m) for m in models if str(m).strip()]
        clean_l = [strip(l) for l in loras if str(l).strip()]
        if clean_m or clean_l:
            line += "\n随心配 模型: " + " / ".join(clean_m) \
                    + "  LoRA: " + " / ".join(clean_l)
    # 后面那段附言**可在管理页改**（settings.json 的 `caption_note`，见
    # agents.caption_note）。2026-10-06 用户拍板：「那些字我就希望我可以自己
    # 去附加这一些内容……一点换行、二点换行」。没设就用内置默认（引用这张图
    # 的三条用法——他实测下来「引用图一直出问题」的就是这三条没讲清）。
    # 附言只在有编号时跟着走：`_caption` 没 tag 就整行不发，附言也不该单独刷一条。
    note = _caption_note_text()
    return line + ("\n" + note if note else "")


# 出图 caption 附言的内置默认（管理页整段可改；把框**清空** = 这段不要了，
# 只发 `编号 · 分辨率 · 渠道 · seed` 那一行）。2026-10-11 用户拍板：回执字数
# 太多，压成一行；qwen 图生图 / qwen 修复两条冷门操作挪进 /更多渠道 的
# 【引用玩法】段，不再每张图后面复读。
_CAPTION_NOTE_DEFAULT = "引用这张图：「提取提示词」拿原词 ｜ 说需求 = 照着改图"


def _caption_note_text():
    """这段附言的当前内容：管理页设了就用设的（空串 = 不要附言），没设用默认。"""
    try:
        from app.core.agents import caption_note
        note = caption_note(QQ_AGENT_ID)
    except Exception:                        # 配置读坏了也不能把发图这一路弄断
        return _CAPTION_NOTE_DEFAULT
    return _CAPTION_NOTE_DEFAULT if note is None else note


def caption_note_default():
    """内置的那份附言（管理页 GET 用它当编辑框的初始内容）。"""
    return _CAPTION_NOTE_DEFAULT


# 随机口令重抽尽后的兜底话术（image_audit 的 BLOCKED_NOTICE/FAILED_NOTICE
# 都不合适：那是「用户点的单被拦」的话术，随机图是机器人自己推的服务）。
RANDOM_BLOCKED_NOTICE = "这轮抽到的画面没画好，再发一次口令试试～"


def _send_image(target, target_id, filename, tag="", skill="", seed=None,
                notify=True, prompt="", reply_to="", at_user=None,
                anima_draw=None):
    """发回原会话。先过 image_out 出副本（jpg 压扁丢元数据 / png 原样带工作流），
    格式看管理页开关。

    返回 True = 真发出去了。审核拦下时返回 False（**不抛异常**）——
    调用方靠它区分「发了」和「被拦了」，别把拦截记成发送失败。

    tag / skill / seed 是这张图的编号、渠道和种子，作为 caption 和图片放在
    **同一条消息**里（见 app/features/image/log.py 与本文件 `_caption`）：群友引用这条
    消息时它们会跟着引用回到模型眼前。不传 tag = 不加那行字，行为与从前一致；
    不传 seed = 那一段不写。

    prompt（2026-10-06）只为**被拦时**服务：它和上面几项一起进人工二审队列，
    管理员点「过审」补发时，图有了、账本也补得上（编号 → 提示词）。正常发送
    路径不用它（账本由调用方在确认发出后记）。

    ⚠️ 审核审的是 `prepare_for_send` 的产物（本地文件），也就是**真正要发出去
    的那份字节**，不是 ComfyUI 的原图。见 app/image_audit.py 的模块注释。
    """
    from app.features.qq import api as qq_api
    from app.features.image import audit as image_audit
    from app.features.image import out as image_out
    from app.core.agents import image_send_format
    fmt = image_send_format(QQ_AGENT_ID, target, target_id)
    path = image_out.prepare_for_send(filename, fmt)
    # caption 提到审核**之前**算：被拦时它要跟着图一起进人工二审队列，管理员
    # 点「过审」补发的就是这一行原文——跟没被拦过的图一个字都不差。
    caption = _caption(tag, path, skill, image_out, seed, anima_draw)
    if not image_audit.allow_send(path, QQ_AGENT_ID, target, target_id,
                                  notify=notify,
                                  meta={"tag": tag, "skill": skill,
                                        "seed": seed, "caption": caption,
                                        "file": filename, "prompt": prompt}):
        return False
    # 只在真有引用/@ 时才多传参数：没有的时候调用形态与从前**完全一致**，
    # 那些按老签名接的替身（测试、其他投递路径）不会平白收到新 kwarg。
    extra = {}
    if reply_to:
        extra["reply_to"] = reply_to
    if at_user:
        extra["at_user"] = at_user
    try:
        qq_api.send_image(target, target_id, path, caption=caption, **extra)
    except Exception as exc:
        # 自愈（2026-10-31）：副本发到一半没了 → 从 ComfyUI 原图**重新取一份**
        # 再发。图从来没坏过（原图一直在 output 里），坏的只是 temp 里那份
        # 中间产物，让用户重画等于为了一个临时文件白扔 100 秒。
        if not image_out.missing_file_error(exc):
            raise
        log.warning("发送副本不见了（%s），从原图重取一份重发：%s", exc, filename)
        path = image_out.prepare_for_send(filename, fmt)
        qq_api.send_image(target, target_id, path, caption=caption, **extra)
    return True

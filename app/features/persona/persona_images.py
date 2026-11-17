"""扮演配图：会话队列身份、失败原因与入队出图。"""

import logging
import random

log = logging.getLogger("persona")


def _session_target(session_key):
    """会话线 → `(target, target_id)`（`group_123` → `("group", "123")`）。

    **网页线（2026-11-04 补）**：`web_<qq>` / `web_<qq>__<卡名>` → `(None, qq)`。

    为什么 target 是 `None`：`image_jobs` 早就把 None 当「网页侧」——
    照常排进**同一条图片队列**（同渠道、同优先级、同一块显卡，用户要的
    「网页也走 QQ 那条队列」就是这个意思），但 worker 不往 QQ 发任何东西
    （`_notice` / `_deliver` 在 target 为 None 时直接返回），图落
    `output/web/生图页/`（`comfy_out.session_base` 的既有约定）。

    `target_id` 仍然带 qq：`enqueue` 的会话键是 `("", qq)`，每个网页用户
    各算各的在途张数。都填 None 会让所有人共用 `("", "")` 这一个额度。

    不映射 `web_` 的后果：这里返回 `("", "")` → `draw()` 认为「这条线没有
    队列身份」直接返回空，网页「出图」永远失败。实测 `logs/agent.log`
    只有「扮演出图」那行，后面既没有「扮演配图完成」也没有「没画出来」，
    就是卡在这个静默分支。

    返回 `("", "")` 只表示这条线真的没有队列身份（不该出图）。
    """
    key = str(session_key or "")
    target, _, tid = key.partition("_")
    if target == "web":
        qq = tid.split("__", 1)[0]
        return (None, qq) if qq and qq != "local" else ("", "")
    if target not in ("group", "private") or not tid:
        return "", ""
    return target, tid


def _draw_fail(reason):
    """记下这一轮没画出来的真实原因，返回值仍是 ""（不改 draw 的签名）。"""
    from app.features.persona import service as svc

    svc._DRAW_ERR.reason = reason
    log.info("扮演配图没画出来：%s", reason)
    return ""


def draw(scene, card, session_key, reply_to="", state="", skill=None,
         landscape=False, on_submit=None, outfit=""):
    from app.features.persona import service as svc

    """出一张配图，返回文件名（没画出来返回 ""）。**阻塞**——调用方放后台线程。

    走 `SHOT_SKILL`（角色扮演默认 `silver`，两段采样，横屏竖屏同一条渠道，
    画幅只由 `landscape` 决定）。排队走**正常 QQ 队列**，不插队。
    入队带 `quiet=True`：worker 只跑图不发，图由调用方拿去 `_deliver`（要带
    caption 和引用，见 `qq_bot._draw_persona`）。

    `reply_to`（2026-11-02）：玩家触发这一轮的那条消息 id。**必须在消息线程上
    传进来**——`enqueue` 的 `reply_to=None` 会去读 `qq_api.current_message_id()`，
    而本函数跑在后台线程，那时上下文早不是本轮了，读出来是空或者别的轮次。
    这就是「图不引用消息」的根因。

    `outfit`（2026-11-09）：这一轮固化的当前穿着（英文），由 `shot_outfit()`
    从那一轮的归档取，前置到画面行之前。`state` 是中文状态行，只给
    `clothes_changed()` 判要不要出图，不参与拼提示词。

    也不扣私聊每日额度：扮演态配图是聊天的一部分，不是「点单生图」。队列满 /
    被审核拦下时，`_draw_persona` 那条自动配图一律静默跳过，聊天照常；
    `draw_shot()`（网页「出图」/ QQ 点单）会读 `_DRAW_ERR` 把真实
    原因回给玩家。两者共用同一个 `draw()`，返回值始终是字符串。
    ComfyUI 正在重启时**照常入队**（2026-11-09）——worker 提交前会等它回来，
    不再把重启窗口顶回成一次失败。
    """
    from app.features.image import jobs as image_jobs
    from app.tools.normal import generate_image as gi

    svc._DRAW_ERR.reason = ""       # 同一个线程可能被复用，先清掉上一轮的原因
    # 机位固定拼，主体由角色固定块声明一次（`svc.build_prompt`）。
    # 2026-11-08 撤掉出图前的 tag 自动修：演员写的是英文散文长句，不是
    # Danbooru tag，那套漫画判据在这里 0 命中、纯空转；自动补 `1other`
    # 反而会把视角方实体化（漫画工作台 2026-10-12 的旧事故）。
    # 代码只检测事实，画面写什么交给模型。
    prompt = svc.build_prompt(card, scene, state, pov=True, outfit=outfit)
    target, target_id = _session_target(session_key)
    # `target == ""` 才是「这条线没有队列身份」。不能用 `not target`：网页线
    # 的 target 是 None（见 `_session_target`），那也是合法身份。
    if not svc._clean_scene(scene) or not prompt or target == "":
        return _draw_fail("这一轮没有可画的画面")
    seed = random.randint(0, 2 ** 32 - 1)
    chan = skill or svc.FAST_SKILL
    # 角色卡上写了 `char_lora` 就硬挂（2026-11-08 用户拍板）：卡已经知道
    # 她是谁，不用再让角色库猜一遍。没写就原样（旧行为，走向量召回）。
    wf = gi.build_t2i_workflow(
        chan, prompt, seed,
        user_text=str((card or {}).get("name") or ""),
        key=str((card or {}).get("char_lora") or ""))
    if wf is None:
        log.warning("扮演配图渠道 %s 没有文生图工作流", chan)
        return _draw_fail("渠道 %s 没有文生图工作流" % chan)
    # 探活**不再拒单**（2026-11-09 用户拍板，对齐 QQ 侧 2026-10-19 的口径）：
    # 从前这里探不到就当场 `_draw_fail`，等于把每一个 ComfyUI 重启窗口（实测
    # 14~22 秒）变成一次失败——而 worker 提交前本来就会 `wait_comfy_up` 等它
    # 回来（见 image_jobs._worker）。现在照常入队，`comfy_down` 只影响回执措辞。
    comfy_down = not image_jobs.comfy_alive()
    try:
        # landscape=False 写死：这里跑在后台线程，读不到本轮原话（qq_api 的
        # 上下文是 threading.local），不能让 enqueue 自己去判——判不出会退回
        # 「本轮原话里有没有横屏」，而这里恒为 None。
        job, reason = image_jobs.enqueue(
            target, target_id, wf, skill=chan, prompt=prompt,
            seed=seed, landscape=bool(landscape), quiet=True,
            # 输出目录（2026-11-07 用户点名）：扮演的配图**不许**再混进
            # `web/生图页/`——那是手动出图的地方，两边的图混在一起找不回来。
            # `job.out_dir` 优先于按 target 推断（漫画工作台走的是同一条路）。
            out_dir=svc.PLAY_OUT_DIR,
            comfy_down=comfy_down,
            reply_to=str(reply_to or ""))
    except Exception as e:
        log.exception("扮演配图入队失败 %s", session_key)
        return _draw_fail("排队时出了岔子：%s" % e)
    if reason:
        log.info("扮演配图没排上（%s）：%s", session_key, reason)
        return _draw_fail("没排上：%s" % reason)
    # 提交成功那一刻给调用方一个钩子（2026-11-05 用户要求）：出图要几十秒到
    # 一两分钟，不回一句「已提交」，用户根本不知道这单到底排上没有。
    # 只传 job 不传调度细节——回执文案由调用方决定（QQ 用卡片发，网页蹲轮询）。
    if on_submit is not None:
        try:
            on_submit(job)
        except Exception:
            log.exception("扮演配图回执失败 %s", session_key)
    try:
        entry = job.wait()
    except Exception as e:
        log.info("扮演配图没画出来（%s）：%s", session_key, e)
        return _draw_fail("生成没成：%s" % e)
    names = image_jobs.output_images(entry, job=job)
    if not names:
        log.warning("扮演配图跑完却没有图片（%s）", session_key)
        return _draw_fail("ComfyUI 跑完了，但这一轮没拿到图片")
    log.info("扮演配图完成 %s：%s（%s）", session_key, names[0], prompt[:60])
    return names[0]

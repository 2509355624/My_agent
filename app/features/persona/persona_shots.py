"""出图点单：触发词、素材提取与单张 CG。"""

import logging

log = logging.getLogger("persona")


def is_shot_request(text):
    """这条消息是不是在要求「按当前场景出图」。

    只认**整条消息**是触发词（可带 # 前缀、可带「一下」「吧」这类语气词），
    不做子串匹配——扮演态里的「你刚才出图了？」是在聊天，不是在下命令。
    """
    from app.features.persona import service as svc

    t = str(text or "").strip()
    if not t:
        return False
    if t.startswith("#"):
        t = t[1:].strip()
    if t in svc.SHOT_WORDS:
        return True
    # 去掉结尾的语气词再比。**注意顺序**：先比原串再剥——「画一张」本身就在
    # 词表里，先剥「一张」会剥成「画」，反而认不出（实测踩过）。
    for tail in ("吧", "啊", "呀", "呢", "一下", "一张", "！", "!", "。", "."):
        if t.endswith(tail):
            t = t[: -len(tail)].strip()
            if t in svc.SHOT_WORDS:
                return True
    return False


def _shot_parts(session_key, msg_index=None):
    """「出图」要用的素材：`(用户原话, 她的正文, 画面行, 状态)`。

    `msg_index=None` → 最后一轮（扫窗口）；数字 → **归档第 N 行**的 assistant
    （必须是 assistant 行，行号只增不删，不会随窗口裁剪漂移）。
    `comic_brief`（moss 漫画 brief）和 `shot_scene`（单张 CG）共用这套提取，
    保证两个出图路径画的是同一轮。
    """
    from app.features.persona import service as svc

    session_key = svc.resolve_line(session_key)
    ent = svc._entry(session_key) or {}

    last_user = ""
    last_ai = ""
    scene = ""                  # 「画面：」那一行——本轮画面的定稿
    state = str(ent.get("state") or "").strip()

    if msg_index is None:
        hist = [m for m in svc.trim(svc._load_hist(session_key))
                if m.get("role") in ("user", "assistant")]
        for m in reversed(hist):
            if m["role"] == "assistant":
                body, sc, _mo, _it, _st = svc.split_turn(m["content"] or "")
                if not last_ai:
                    last_ai = body.strip()
                    scene = (sc or "").strip()
            else:
                body = str(m["content"] or "").strip()
                if not last_user:
                    last_user = body
            if last_user and last_ai:
                break
    else:
        # 按**归档下标**定位（2026-11-04）。以前这里是「窗口里第几条 assistant」，
        # 而窗口每次落盘都会裁，聊一轮整体前移一位——网页点旧气泡出图时传的
        # 是当时的号，压缩后就指到别人那条上。归档只增不删，行号才是稳定锚。
        try:
            n = int(msg_index)
        except (TypeError, ValueError):
            n = -1
        rows = svc.load_archive(session_key)
        if 0 <= n < len(rows) and rows[n].get("role") == "assistant":
            last_ai, sc, _mo, _it, st = svc.split_turn(rows[n].get("content") or "")
            last_ai = last_ai.strip()
            scene = (sc or "").strip()          # 用**那一轮**的画面，不是最后一轮
            if st.strip():
                state = st.strip()      # 用**那一轮**的状态，不是此刻的
            # 往前找最近的一条用户消息
            for j in range(n - 1, -1, -1):
                if rows[j].get("role") == "user":
                    last_user = str(rows[j].get("content") or "").strip()
                    break
    return last_user, last_ai, scene, state


def shot_outfit(session_key, msg_index=None):
    """「出图」这一轮的「服饰：」行（当前穿着的英文定稿）。

    优先取**那一轮**归档/窗口里的原文：点旧气泡出图时，画的是当时的穿着，
    不是此刻的。老线没有这一行（功能上线前的归档），回落到当前固化的
    `ent["outfit"]`；再没有就返回空串，`build_prompt` 走老拼法。

    和 `_shot_parts` 分开，是为了不改它那个四元组返回（漫画 brief 也按它拆包）。
    """
    from app.features.persona import service as svc

    key = svc.resolve_line(session_key)
    ent = svc._entry(key) or {}
    cur = str(ent.get("outfit") or "").strip()
    if msg_index is None:
        rows = [m for m in svc.trim(svc._load_hist(key))
                if m.get("role") == "assistant"]
        if rows:
            return svc.split_outfit(str(rows[-1].get("content") or "")) or cur
    else:
        try:
            n = int(msg_index)
        except (TypeError, ValueError):
            n = -1
        rows = svc.load_archive(key)
        if 0 <= n < len(rows) and rows[n].get("role") == "assistant":
            return svc.split_outfit(str(rows[n].get("content") or "")) or cur
    return cur


def draw_shot(card, session_key, msg_index=None, reply_to="", on_submit=None,
              landscape=False, skill=None, scene=None):
    from app.features.persona import service as svc

    """「出图」→ 默认 `svc.SHOT_SKILL`（silver）单张 CG，竖屏/横屏可选。

    2026-11-19：网页「出图」可以临时指定渠道（`skill`），走同一套 `svc.draw`，
    画面词仍是写作 AI 给的英文定稿，不经过跑图台的中文需求转译。
    `scene` 可由调用方先补写；不传则自己取（空标签会走 `ensure_shot_scene` 补）。
    """
    scene = (scene or "").strip() or svc.ensure_shot_scene(session_key, msg_index)
    if not scene:
        return "", "这一轮没有可画的画面"
    chan = str(skill or "").strip() or svc.SHOT_SKILL
    # 排查用（2026-11-05）：线上反馈「出图还是六格漫画」。这条日志把
    # **实际用的渠道和画幅**打出来，一眼就能分辨是代码没生效（进程跑着旧
    # 版本 → 没有这行）还是走了别的入口（这行有但渠道不是 svc.SHOT_SKILL）。
    outfit = shot_outfit(session_key, msg_index)
    svc.log.info("扮演出图：渠道=%s 画幅=%s 行号=%s 画面=%s 服饰=%s",
             chan, "横屏" if landscape else "竖屏", msg_index,
             scene[:60], outfit[:60])
    name = svc.draw(scene, card, session_key, reply_to=reply_to,
                skill=chan, landscape=bool(landscape),
                outfit=outfit, on_submit=on_submit)
    if not name:
        # 真实原因在 thread-local 上（svc.draw 的返回值十几个调用方都当字符串用，
        # 不改签名）。拿不到才退回老的那句笼统文案。
        return "", (getattr(svc._DRAW_ERR, "reason", "")
                    or "没画出来（排队满 / ComfyUI 没在线 / 被审核拦下）")
    return name, ""

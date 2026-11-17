"""扮演主对话轮：判断意图、拼提示词、调模型、结算状态。"""

import logging

from app.features.persona import state_inject

log = logging.getLogger("persona")


def run_turn(session_key, text, target, sender="", sender_uin="",
             manual=None, writing=None):
    from app.features.persona import service as svc

    """扮演态的一轮：**两次** LLM 调用，返回 `svc.Turn(正文, 画面, 情绪, 强度, 状态)`。

    `writing`（2026-11-06）：`{mode, note}`，酒馆那套分层写法的开关。`mode`
    是文风档位（进 system 头），`note` 是玩家给的「这一幕的调子」（进贴脸
    尾注）。**不传或全空 = 不拼档位、不加尾注**，提示词其余部分照旧。

    `manual`（2026-11-06）：网页「精确输入」的三栏
    `{thought, action, speech, plot}`。填了就以它为准，编剧 AI 不用再拆主
    输入框；`thought` 只给编剧，绝不给演员。

    2026-11-02 起是两段式：
      1. `svc._judge_intent()` —— 短提示词、不带人设，判断这句话在干什么；
      2. 主调用 —— 拿着判断结果写正文。
    第一段失败（超时、没交 JSON、类别不认识）就退化成原来的单次流程，
    只是少了那一行注入，不影响这一轮能回话。

    正文空 = 这轮什么都不发（调用方别开口）；画面空 = 这轮不出图。

    `sender_uin`（2026-11-02）：群里说话人的 QQ 号。**群聊按人隔离状态**，
    见 `svc._persona_key`——不隔离的话 A 刷的好感会算到 B 头上。私聊传不传都行。
    """
    key = svc._persona_key(session_key, sender_uin)
    # 写作档位只做归一化：非法档位退回常态，作者注压成单行。判断写什么
    # 交给模型，这里不设准入闸门。
    mode, author_note = svc.writing_style.norm(writing)
    styled = mode != svc.writing_style.DEFAULT_MODE or bool(author_note)
    # QQ 私聊分线（2026-11-03 方案 B）：base → 当前角色线。网页传来的已是
    # 线键，resolve 幂等。历史/小记/好感从此按角色各存各的。
    key = svc.resolve_line(key)

    # 玩家直接说话了 = 那个「射在哪里」的弹窗被跳过了（2026-11-06）。
    # 不清的话，刷新页面还会把上一轮的弹窗再弹一次。
    svc._set_pending_finish(key, None)

    # ── 群聊不演（2026-11-03 用户反馈）──────────────────────────
    #
    # 用户发现「群里其他人能看到别人的聊天内容」。状态隔离一直是好的，
    # 问题在**群聊内容本来就公开**——机器人发的消息全群可见，隔离记忆
    # 没用。所以群聊这条彻底关掉实际扮演。
    #
    # 返回空的 svc.Turn：调用方不会发任何东西（`_run_persona_turn` 里
    # `turn.reply.strip()` 为空就不发）。群里想玩的人会先收到
    # `handle_command` 的引导语，不会莫名其妙等一个没反应。
    #
    # 存量的 `group_<群号>_<QQ号>` 扮演态自然失效——那条线再也不会被激活。
    if str(session_key or "").startswith("group_"):
        svc.log.info("群聊不扮演，已忽略：%s", key)
        return svc.Turn("", "")

    card = svc.active_card(key)
    if card is None:
        return svc.Turn("", "")

    # 长期记忆 RAG（2026-11-05）：小模型 CPU 冷启动十几秒，只在后台拉起来。
    # 这一轮如果还没 ready，就照常聊，不带回忆；下一轮开始生效。
    # 游客只玩 30 轮，不存也不查（见 `svc.persona_rag.enabled_for`）——不白占资源。
    rag_on = svc.persona_rag.enabled_for(key)
    if rag_on:
        svc.persona_rag.warm()
        # 老线原文补齐（2026-11-10）：原文层上线前的轮次只有提炼摘要，
        # 旧条目还被 400 字截过。本进程每条线只排一次后台回填，幂等；
        # 全量归档用 callable 传，没排上就不读，不拖聊天这一轮。
        try:
            svc.persona_rag.schedule_backfill(key, lambda: svc.load_archive(key))
        except Exception:                                    # noqa: BLE001
            svc.log.exception("原文回填排程失败 %s", key)

    ent = svc._entry(key) or {}
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")
    # 当前穿着（2026-11-09）：独立固化的英文串，本轮要作为事实递给演员。
    # 首轮还没有，就让它按角色卡自己定一身，这一轮交回来后开始固化。
    prev_outfit = str(ent.get("outfit") or "").strip()
    gd = svc._gameday(ent) if ent else None
    day_before = svc._day_of(gd)

    # 历史先读出来（2026-11-05）：第一段从「打分器」升级成「整理器 + 编剧」，
    # 它也要看最近发生过什么，才能判潜台词。原来它只看当前这一句。
    raw_hist = svc._load_hist(key)
    hist = svc.trim(raw_hist)

    # 精确输入（2026-11-06）：玩家分栏标注时，分段以玩家为准，编剧只负责
    # 判意图/打分/导演条。心理活动放进编剧输入，但不进演员输入。
    man = svc._manual_norm(manual)
    man_segs = svc._manual_segments(man)
    man_block = svc._manual_block(man)
    judge_said = text
    if man_block:
        judge_said = man_block + (
            ("\n\n【主输入框（补充）】\n" + text) if text else "")

    # 第一段：这句话在干什么。**用原始文本判**，不带「张三：」这种前缀。
    # 攻略模式（2026-11-03）：同一次调用顺带给关系打分，不多烧一轮。
    raid = svc.affection.is_raid(key)
    # v2 正向关系线：新档走独立打分器和独立结算，老档一条代码都不换。
    v2 = raid and svc.affection.is_v2(key)
    extras = {}
    score = None
    if raid:
        if v2:
            intent, inote, score = svc._judge_v2(
                card, judge_said, prev_state, line_key=key, gameday=gd,
                history=hist, note=note)
        else:
            intent, inote, score = svc._judge_intent_scored(
                card, judge_said, prev_state, line_key=key, gameday=gd,
                history=hist, note=note)
        extras = score if isinstance(score, dict) else {}
    else:
        intent, inote, extras = svc._judge_intent(
            card, judge_said, prev_state, history=hist, note=note)
        extras = extras if isinstance(extras, dict) else {}
    # 精确输入（2026-11-09）：玩家亲手填的三栏是他本人的意图，照旧按块发；
    # 编剧那边已经不拆段了（它只记数值和事实），所以这里只剩 man_segs 一个来源。
    # 有心理活动就锁 secret——内容给写作层，但不许她"听见"。
    if man_segs:
        extras["addressed"] = True
        extras["secret"] = bool(man.get("thought"))
    finish = svc._norm_finish(extras.get("finish"))
    # 射精选择（2026-11-05）：上一轮她高潮满了，玩家在弹窗里选了「射在哪里」。
    # 这个选择**不是**玩家在正文里写的，所以编剧判不出来，得由系统补进去。
    # 取一次就清，免得后面每一轮都在射。
    fin_pick = svc.take_finish(key)
    if fin_pick:
        # 位置决定「有没有射在里面」——怀孕记账只认这个，不能靠编剧猜。
        if fin_pick in ("pussy", "anus"):
            finish = "inside"
        elif fin_pick in ("mouth", "body"):
            finish = "outside"
        else:
            finish = "none"
    # 亲密场景那 1,284 字只在该用的时候拼（2026-11-03）。
    # 判不出来（intent 为空）时按 True 走——宁可多发，不要因为分类失败让
    # 亲密场景写得干巴巴。`neutral` 也带上，它随时可能拐到亲密去。
    intimate = intent in (None, "", "intimate", "neutral")
    head = svc.build_head(card, session_key, sender, note, prev_state,
                      intimate=intimate, mode=mode, dynamic=False)
    # 缓存 Phase B：逐轮恒定的写法说明并进 system 头（稳定前缀）吃命中，
    # 末尾玩家消息里那份由 svc._strip_stable_notes 剔除，文字总量不变。
    head = head + "\n\n" + "\n\n".join(svc._stable_note_blocks())
    # 缓存优化 Phase A（2026-11-10）：所有逐轮变化的内容不再进 system 头，
    # 改攒进 `dyn_parts`，最后统一贴到末尾玩家消息，保住 head 的前缀缓存。
    dyn_parts = []
    nb = svc._note_block(note)
    if nb:
        dyn_parts.append(nb)
    sb = svc._state_block(prev_state)
    if sb:
        dyn_parts.append(sb)
    block = svc._intent_block(intent, inote)
    if block:
        dyn_parts.append(block)
    # 导演条（2026-11-05）：第一段顺手判的潜台词 / 她察觉到的 / 这一拍的态度。
    # 它只给演员「怎么反应」，不给台词——所以放在 intent 之后、结算之前。
    dblock = svc._director_block(extras, plot=man.get("plot"))
    if dblock:
        dyn_parts.append(dblock)
    # 射精位置（2026-11-05）：玩家在弹窗里选的那个，不由正文决定。
    # 拼在导演条之后、结算之前——它改的是「这一拍怎么写」，不是数值。
    fblock = svc.finish_block(fin_pick)
    if fblock:
        dyn_parts.append(fblock)
    # 长期记忆（2026-11-05）：按玩家这一轮**看得见的话**去翻旧账。
    # 用户没说出口的「想」不参与检索——拿心里话去翻回忆，容易让她提前知道。
    #
    # 2026-11-07：这里只**取**不拼。回忆块改成和「故事到现在」一起放到
    # 最后一条玩家消息里——system 头是前缀，动态内容插进去会把前缀缓存
    # 打散（实测命中 59~70%，一轮两万 token 时这一步很贵），放末尾既保住
    # 缓存，又离生成点更近。
    visible = svc._visible_user_text(
        man_segs, "" if man.get("thought") else text)
    rblock = ""
    if rag_on:
        try:
            rblock = svc.persona_rag.build_block(
                key, visible, day=day_before, context=svc._rag_context(hist))
        except Exception:                                # noqa: BLE001
            svc.log.exception("回忆注入失败 %s", key)
    # 攻略模式结算（2026-11-03）：分数落账 → 按档位往主提示词里注入关系块。
    # 没打出分（score 为 None）也照走 settle——按 +0 记账，档位注入照常。
    preg_event = None
    aff_res = None
    if raid:
        aff_res = svc.affection.settle(key, score, game_day=day_before)
        # v2 的 res 是另一套轴，没有高潮/内射/怀孕那几项，别拿 v1 后处理去读。
        if isinstance(aff_res, dict) and not v2:
            # 让状态块知道这一轮有没有内射：高潮和内射同一拍时换写法。
            aff_res["finish"] = finish
            # 怀孕/产后阶段（2026-11-06）：开发度满格时要靠它判断
            # 「一碰就出水 / 一含就冒汁」该不该加码。
            try:
                aff_res["pregStage"] = svc.pregnancy.stage_key(
                    svc._entry(key) or {}, svc._gameday(svc._entry(key) or {}).get("day"))
            except Exception:                        # noqa: BLE001
                aff_res["pregStage"] = ""
            # 高潮满了要弹窗（2026-11-05）：前端看到 `climax` 才问
            # 「射在哪里」。`last=True` 表示这已经是第 5 次——AI 回完之后
            # 还要再问一次「结束 / 最后一次」。
            cl = svc._climax_info(aff_res)
            if cl:
                aff_res["climax"] = cl
                # 落盘成待办：页面刷新/断网后还能再问一次（见
                # `svc._set_pending_finish` 的说明）。
                svc._set_pending_finish(key, cl)
        # 道具（2026-11-08）：记录进出的东西 + 拼注入块。
        # 三块拼在一起：能用的清单（常态）、这一轮用到的写法、她给过的东西。
        try:
            _istate = svc._load_state()
            _irec = _istate.get(key) or {}
            # 用 extras 不是 score：自由模式下 score 是 None，但 extras 三种
            # 情况都有值（v2 / v1攻略 / 自由），道具在三条路上都要能用。
            _sc = extras if isinstance(extras, dict) else {}
            _idelta = _sc.get("items") if isinstance(_sc.get("items"), dict) else {}
            # 编剧没给就自己从原话里认一遍（只认表里的别名，不猜）
            _use = [str(x) for x in (_idelta.get("use") or []) if str(x).strip()]
            if not _use:
                _use = svc._items.match(text)
            _gains = _idelta.get("gain") or []
            if _gains:
                _irec["items"] = svc._items.apply_gain(_irec.get("items"), _gains)
                _istate[key] = _irec
                svc._save_state(_istate)
            # 生效中的道具（2026-11-08）：一次只一个。倒计时由代码做，
            # 不靠模型记「第几轮」——它记不住。
            _act = svc._items.normalize_active(_irec.get("active_item"))
            _fade_name, _fade_key = "", ""
            if _act:
                _act, _fade_name = svc._items.tick(_act)
                _fade_key = str((_irec.get("active_item") or {}).get("key") or "")
            _started = ""
            if _use:
                # 一次只用一样：多个就用第一个，其余忽略（用户拍板，先做简单版）
                _act, _old = svc._items.start(_act, _use[0], svc.turn_total(key))
                if _old:
                    svc.log.info("道具顶替：%s 被 %s 换下", _old, _use[0])
                _started = _use[0]
            # 写回存档（active 变了就存）
            if _act or _fade_name or _irec.get("active_item"):
                _irec["active_item"] = _act
                # 失效那一轮留个标记给前端画删除线，读一次就清（见 pop_item_fade）
                if _fade_name:
                    _irec["item_fade"] = {"key": _fade_key, "name": _fade_name}
                _istate[key] = _irec
                svc._save_state(_istate)
            for _blk in (svc._items.list_block(),
                         svc._items.use_block([_started] if _started else []),
                         svc._items.active_block(_act, just_started=bool(_started)),
                         svc._items.fade_block(_fade_name, _fade_key),
                         svc._items.keepsake_block(_irec.get("items"))):
                if _blk:
                    dyn_parts.append(_blk)
        except Exception:                                # noqa: BLE001
            svc.log.warning("道具注入失败 %s", key, exc_info=True)
        # v2 成人向（2026-11-08）：编剧判好的 adult delta → 结算 → 拼中文注入。
        # 老档（没 schema=2）不会进这个分支，v1 一个字不动。
        if v2 and isinstance(score, dict) and isinstance(score.get("adult"), dict):
            try:
                _astate = svc._load_state()                  # 读整份，改一条，写回
                _rec = _astate.get(key) or {}
                _ast = svc.adult_state.load(_rec)
                _n_before = int(_ast.get("orgasm_count") or 0)
                _ast, _anotes = svc.adult_state.apply(
                    _ast, score["adult"],
                    value=(aff_res or {}).get("value") if isinstance(aff_res, dict) else None)
                # 只写 svc.affection 下面那一份（2026-11-08）：adult 状态属于
                # 关系档的一部分，和 value/contacts 放在一起，别在顶层再存一份
                # ——两份会各自被读，一旦不同步就是「显示的和我看到的不一样」。
                svc.adult_state.save(_rec, _ast)     # 写 svc.affection.adult，顺手清顶层那份
                _astate[key] = _rec
                svc._save_state(_astate)
                # 高潮弹窗（2026-11-09 补 v2）：v2 以前没有 `just_orgasm`，
                # 「射在哪里」永远不弹，怀孕记账也就没有来源。这里用
                # `orgasm_count` 的增量当唯一事实，和 v1 的 `svc._climax_info` 同口径。
                _n_now = int(_ast.get("orgasm_count") or 0)
                if _n_now > _n_before and isinstance(aff_res, dict):
                    aff_res["just_orgasm"] = _n_now
                    aff_res["orgasmMax"] = svc.affection.ORGASM_MAX
                    _cl = svc._climax_info(aff_res)
                    if _cl:
                        aff_res["climax"] = _cl
                        svc._set_pending_finish(key, _cl)
                for _n in _anotes:
                    svc.log.info("adult %s：%s", key, _n)
            except Exception:                            # noqa: BLE001
                svc.log.exception("成人向结算失败 %s", key)
            else:
                _ab = svc.adult_state.block(_ast)
                if _ab:
                    dyn_parts.append(_ab)
        blk = svc.affection.injection_block(aff_res)
        if blk:
            dyn_parts.append(blk)
        # 动态状态（2026-11-06 用户要求）：冲动/高潮是这一轮的气氛，档位到了
        # 就临时注入一段行为规则——她这一拍会主动到什么程度、脸上什么样。
        # 和长期轴（好感/堕落/屈服）分开：只在这一轮有效。
        dyn = svc.affection.dynamic_block(aff_res)
        if dyn:
            dyn_parts.append(dyn)
        # 游戏内时间（2026-11-04）：把打分 AI 判的跨天/约定落账，
        # 再把「第 N 天 + 今天的事 + 约定」拼进系统头。
        #
        # **2026-11-05 取消「第五次自动跨天」**（用户拍板）：那一步改成弹窗
        # 驱动的两段——第五次先正常输出，AI 回完再问「结束 / 最后一次」。
        # 自动跳会让玩家没得选，而且第五次那一轮被硬塞了「过渡到第二天」的
        # 指令，写出来就不再是那一场本身了。
        gd = svc._apply_gameday(key, score) or gd
        # 第一次进入（2026-11-18）：本轮注入只读旧事实；先让 state_inject 看到
        # 「这一拍之前有没有做过」，否则刚置上的 flag 会把本轮的第一次规则
        # 自己压掉。落盘放在 state_inject 之后（见下面）。
        if isinstance(aff_res, dict) and not v2:
            try:
                aff_res["first_entry"] = svc.affection.first_entry(key)
            except Exception:                            # noqa: BLE001
                svc.log.exception("first_entry 读取失败 %s", key)
                aff_res["first_entry"] = {}
        # 跨天过渡（2026-11-10）：代码只报「上一拍收在第几天」这个事实，
        # 怎么把昨天接过来由模型自己写。自由模式不推进天数，恒为 False。
        crossed_day = int((gd or {}).get("day") or day_before) > day_before
        # 怀孕记录（2026-11-05）：放在时间推进/快照**之后**写。`preg_prev`
        # 必须是这一轮开始前的状态，回退时才能把本轮的内射记录一起撤掉。
        if finish == "inside":
            try:
                svc._preg_record(key, card, day_before, finish)
            except Exception:                            # noqa: BLE001
                svc.log.exception("怀孕记录失败 %s", key)
        try:
            new_day = int((gd or {}).get("day") or day_before)
            if new_day > day_before:
                # 先清 v2 的高潮值/冷却：怀孕结算万一抛异常，收场也不该漏。
                svc._clear_adult_night(key)
                preg_event = svc._preg_advance(key, day_before, new_day)
        except Exception:                                # noqa: BLE001
            svc.log.exception("怀孕结算失败 %s", key)
        tblock = svc._gameday_block(gd, new_day=crossed_day)
        if tblock:
            dyn_parts.append(tblock)
    else:
        # 自由模式不推进天数，但内射照样按天记账，等时间跳跃时统一结算。
        if finish == "inside":
            try:
                svc._preg_record(key, card, day_before, finish)
            except Exception:                            # noqa: BLE001
                svc.log.exception("怀孕记录失败 %s", key)
        # 手动切的时段也要进系统头（2026-11-05）：否则自由模式切到晚上，
        # 她的下一句还按白天的场景写。
        tblock = svc._gameday_block(gd, new_day=False)
        if tblock:
            dyn_parts.append(tblock)
    # 怀孕/孩子（2026-11-05）：身体状态每轮都该在她脑子里，不需要等玩家问。
    # 必须放在跨天结算**之后**拼：刚发现怀孕 / 刚生产这一轮就要按新状态演，
    # 不能等下一轮才让她想起来（用户说的「先结算，再开场」）。
    pblock = svc.pregnancy_block(key, preg_event)
    if pblock:
        dyn_parts.append(pblock)
    # 压迫模式（2026-11-05）：在 `aff_res` 上补一个 `forced` 标记。
    # **必须在这里补，不能指望 `injection_block` 写回**——它是纯函数，
    # 拿结果拼完就丢；这里不补的话，下面 `svc.intimate_guide` 里的压迫层
    # 和 `writing_skills` 的 forced 轴永远进不去，整套玩法等于没接。
    if isinstance(aff_res, dict) and aff_res.get("active") and not v2:
        aff_res["forced"] = svc.affection.is_forced(
            aff_res.get("value"), aff_res.get("corrupt"),
            aff_res.get("arousal"), aff_res.get("excite"))
    # 分层写法（2026-11-04 深夜）：按**这一轮的高潮值**挑一块「这一拍该怎么写」
    # 拼上去。放在 settle 之后是因为要拿最新状态——放在 svc.build_head 里只能拿到
    # 上一轮的值，永远慢一拍。只在判成亲密时拼：`joke` / `literal` / `insult`
    # 那些场合用不上「到了怎么写」，拼进去只会分走注意力。
    if intimate and not v2:
        extra = svc.intimate_guide(aff_res)
        if extra:
            dyn_parts.append(extra)
    # 数值读数（2026-11-09 用户拍板）：**每轮都给**，不再只挂在亲密轮上。
    #
    # 以前 `svc.body_state_block` 写在上面那个 `intimate` 分支里，聊天轮她手里一个
    # 数都没有，只能靠编剧那句二手判断演；而 v2 那条线整段被 `not v2` 挡掉，
    # 写作层连「现在第几档」都看不到，全靠编剧的摘要。现在关系和数值每轮都给：
    # 她自己得知道现在站在哪儿，才谈得上「按区间演」。
    #
    # 位置保持在写法层之后（原来的顺序）：先把这一拍写到什么程度定下来，
    # 再谈用词，免得数值被写法层的词句盖住。
    if isinstance(aff_res, dict) and aff_res.get("active") and not v2:
        body = svc.body_state_block(aff_res, gd)
        if body:
            dyn_parts.append(body)
        # 状态原文分层注入（2026-11-18）：只把当前命中档位的原书原文贴进
        # 这一轮，不再把整本 1.1 万字固定塞进 system 头。选择在 state_policy，
        # 这里只负责拼；不设字数上限——现实里有几条就注入几条。
        state_txt = state_inject.build(aff_res, gd)
        if state_txt:
            dyn_parts.append(state_txt)
        # 本轮是不是「第一次进入」已经由上面的注入判定完；现在才落盘，
        # 下一轮读到 flag 就不会再写「第一次进入」。落盘失败只影响下一轮去重，
        # 不影响这一轮回复。
        try:
            aff_res["first_entry"] = svc.affection.mark_first(
                key, (gd or {}).get("contact"),
                game_day=(gd or {}).get("day"),
                place=(gd or {}).get("place"))
        except Exception:                                # noqa: BLE001
            svc.log.exception("first_entry 记录失败 %s", key)

    # 第一轮把网页上那段开场导读也发给模型（2026-11-04）。
    # 否则玩家照着导读里的场景开口（「咖啡好香」），模型手里根本没这个场景，
    # 只能另起一个头，两边当场对不上。只在没有历史时拼，之后靠状态行承接。
    if not hist:
        intro = str((svc._entry(key) or {}).get("intro") or "").strip()
        if intro:
            dyn_parts.append("【此刻】\n" + intro
                             + "\n（这是场景设定，照着它写，不要把这段话复述出来。）")

    # 群里带上说话人，模型才知道是谁在说话（同 qq_bot 正文前缀的做法）；
    # 私聊和空正文都不加——「张三：」后面什么都没有比不写更糊涂。
    said = text
    if target == "group" and sender and text:
        said = sender + "：" + text

    # 分段渲染（2026-11-09 改写）：**只有玩家自己标注的分栏**才按块发。
    #
    # 编剧那一层已经不再拆「说 / 做 / 想」了（它只记数值和事实）：它没有写作
    # 契约，硬拆经常拆错——把「他把杯子推过去」记成一句台词，写作 AI 照着错的
    # 分段写，动作就丢了。写作 AI 手里有整份契约和足够长的上下文，读原话比读
    # 别人的摘要准。玩家在「精确输入」里亲手填的三栏是**他本人的意图**，保留。
    segments = []
    facts = extras.get("facts") if isinstance(extras, dict) else []
    addressed = extras.get("addressed", True) if isinstance(extras, dict) else True
    user_fallback = said
    if man_segs:
        segments = man_segs
        addressed = True
        # 填了心理活动 → 主输入框只给编剧，不给她（防止顺手写在主框里的
        # 心里话漏出去）。看得见的内容请填动作/台词/剧情栏。
        user_fallback = "" if man.get("thought") else said
    user_block = svc.render_user_block(segments, addressed, fallback=user_fallback,
                                   remind=False)
    # 近因位（2026-11-07）：所有组装路径统一用 `svc.writing_style.tail()`，把最
    # 要紧的一句放在历史之后、生成之前。并进最后一条玩家消息，消息角色序列
    # 不变（不插中途 system，免得打断历史里带三行元数据的格式 few-shot）。
    # 常态只给一句正向锚点，替换原来那句人称禁令；玩家选了档位或写了调子
    # 才多带轮次和「这一幕」。
    #
    # 顺序是「原话 → 回忆 → 发展线 → 锚点」：越靠后越贴近生成点。发展线比
    # 回忆更近，因为用户抱怨的正是「不知道发展线路」；锚点（人称/篇幅/剧情
    # 推进）永远最后一句。
    if rblock:
        user_block = user_block + "\n\n" + rblock
    story = svc.story_line(gd, note)
    if story:
        user_block = user_block + "\n\n" + story
    turn_no = None
    if styled:
        try:
            turn_no = svc.turn_total(key) + 1
        except Exception:
            turn_no = 1
    # 上一轮的开头（反重复用，见 `svc.writing_style.tail` 的 `last_open`）
    last_open = ""
    for _m in reversed(hist or []):
        if _m.get("role") == "assistant":
            _body, _ = svc._split_meta(str(_m.get("content") or ""))
            last_open = " ".join(_body.split())[:24]
            break
    # 场景义务（2026-11-08）：以前只在「多行 / 三栏分段」时才拼，单行输入
    # （最常见）整段没生效——玩家写「结完账我提起袋子」，模型直接跳到她的
    # 反应，缺了打包提袋那几步。每轮都给一次；已拼过的路径不重复。
    # 放在 tail 之前：它管这一拍怎么落地，tail 仍是最后一句。
    if svc._SCENE_DUTY not in user_block:
        user_block = user_block + "\n\n" + svc._SCENE_DUTY
    # 本轮事实点清单（2026-11-08）：放在场景义务之后、尾注之前，是演员
    # 生成前读到的最后一块实质内容。第一段没给 facts 就整块不出，行为不变。
    fb = svc._facts_block(facts)
    if fb:
        user_block = user_block + "\n\n" + fb
    # 当前穿着（2026-11-09）：和 facts 同层的事实，放在尾注之前。服饰只靠
    # 滑动窗口里上一轮那三行太弱，脱了外套下一轮就被模型重新穿回来。
    ob = svc._outfit_block(prev_outfit)
    if ob:
        user_block = user_block + "\n\n" + ob
    # 原地打转的刻度（2026-11-09）：同一地点连写 ≥3 拍才出现，只报事实。
    pb = svc._place_block(gd)
    if pb:
        user_block = user_block + "\n\n" + pb
    # 缓存优化 Phase A（2026-11-10）：把这一轮所有逐轮变化的块（导演条 /
    # 关系块 / 时间 / 怀孕 / 小记 / 状态 / 写法层 / 身体读数 / 开场导读…）统一
    # 贴到末尾玩家消息，与回忆/故事/事实/穿着同层、紧贴生成点。system 头因此
    # 只剩逐字节稳定的前缀，DeepSeek 前缀缓存才能整段命中。
    dyn_block = "\n\n".join(p for p in dyn_parts if p)
    if dyn_block:
        user_block = user_block + "\n\n" + dyn_block
    # 近因位再钉一次原话（2026-11-10）：写了心里话时主输入框本来就不给她，
    # 这时 user_fallback 为空，跳过——分段标签已经把可见动作说清了。
    echo_text = str(user_fallback or "").strip()
    if echo_text:
        user_block = user_block + "\n\n" + svc._THIS_TURN_ECHO + echo_text
    user_block = user_block + "\n\n" + svc.writing_style.tail(
        turn_no, author_note, plot=man.get("plot"),
        name=svc.char_name(card, key=key), mode=mode,
        last_open=last_open)
    # 恒定块已在 system 头里，这里去掉重复的一份：它们本来每轮都按 miss 付。
    # tail 仍是最后一句，原话仍在最前，动态块的相对顺序一点没动。
    user_block = svc._strip_stable_notes(user_block)
    # 写作 AI 固定最近 6 轮（12 条）——2026-11-17 用户拍板。
    #
    # 为什么不保留 30 条滞回：真实账单里写作调用命中一直卡在 14,115 token
    # （正好 system 头），历史 7,100 token 每轮按 miss 重付；历史并没有跟着
    # 头一起进缓存。30 条只把 miss 越堆越高，没有换来缓存收益。
    # 为什么不按 9000 字滑窗：按字数每轮重算，窗口起点会滑，反而更难解释、
    # 更容易丢整轮。固定 12 条 + 每 5 轮小记，刚好覆盖 6 轮，不出现空档。
    #
    # 落盘/归档仍是长窗口（`cache_rows`），回看和小记一条不少；这里只裁
    # **送模型**的副本。编剧 / 打分器上面用的 `hist` 也仍是最近 12 条。
    model_hist = svc._render_hist(
        svc.trim(raw_hist), name=svc.char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": user_block}])
    try:
        raw = svc._llm_call(msgs, temperature=svc.TEMPERATURE)
    except svc.llm.PersonaChainExhausted:
        # ⚠️ 链路全挂不能吞成空回复：吞了就变成 502「她这轮没说话」，前端
        # 拿不到 relay_down、引导弹窗永远不弹（2026-11-09 真机实测）。
        # 让它冒到 persona_api 的 errorhandler，回 503 让前端弹窗。
        raise
    except Exception:
        svc.log.exception("扮演轮调用失败 %s", key)
        return svc.Turn("", "")
    # 服饰：演员这一轮交的就是新的当前穿着；没交作业沿用上一轮，不清零。
    outfit = svc.split_outfit(raw) or prev_outfit
    reply, scene, mood, intensity, state = svc.split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    if not reply:
        # 整轮空 = 模型没交作业。这属于可用性问题，值得说一句，
        # 但**不重跑**：重跑一次未必更好，还白烧一轮。
        svc.log.warning("扮演轮空回复 %s", key)
        return svc.Turn("（我这会儿有点卡，你再说一句？）", "")

    # 女生主动表白（2026-11-08）：第一段正常回完之后，本轮结算的表达到了
    # 满值、感情线也到了「认准你了」，再让演员补一段把话说出口。
    # 历史里也要带上这一段，否则下一轮模型不知道她已经表白过。
    if v2 and isinstance(aff_res, dict) and aff_res.get("auto_confess"):
        extra = svc._confession_reply(head, model_hist, reply, card,
                                  user_block=user_block)
        if extra:
            body, meta = svc._split_meta(raw)
            raw = body.rstrip() + "\n\n" + extra + (meta or "")
            reply = reply.rstrip() + "\n\n" + extra
            svc.log.info("v2 主动表白第二段 %s（%d 字）", key, len(extra))

    # 落历史：存**完整原文**（含「画面 / 心情 / 状态」三行）。2026-11-02 改：
    # 这三行是下一轮的 few-shot 示范，剥掉的话模型第 2 轮起就把格式丢了
    # （实测第 1 轮完美、第 2 轮全空）。展示时由 `svc.split_turn` 再剥一次。
    #
    # 2026-11-02 补：漏格式的那几轮要先**补成合规形状**再存。否则历史里
    # 混着「没有三行」的回复，下一轮看到的就是「原来可以不写」，越写越散。
    #
    # 2026-11-05：用户消息额外挂 `seg`（分段）。原文 `content` 不动，
    # 网页回看/归档照旧；送模型时由 `svc._render_hist` 再拼成结构化块。
    new_user = {"role": "user",
                "content": svc._manual_display(man, text) if man else said}
    if segments:
        new_user["seg"] = segments
        new_user["addressed"] = addressed
    raw_hist.append(new_user)
    raw_hist.append({"role": "assistant", "content": svc._ensure_meta(
        raw, scene, mood, intensity, state, outfit)})
    # 2026-11-10：落盘就是**下一轮要送的窗口**，逐轮只追加、到高水位才砍回。
    # 以前每轮 `[-svc.HISTORY_LIMIT:]`，聊过 6 轮后窗口每轮前移一条，前缀缓存
    # 全部错位（hit 永远只有 system 头那 15,104）。全量原文另有归档，不丢。
    try:
        svc._save_hist(key, svc.cache_rows(raw_hist))
    except Exception:
        svc.log.exception("扮演轮落史失败 %s", key)

    # 归档（2026-11-03）：窗口只留 12 条给模型看，**存档要全**。
    # 老会话第一次补写时拿当前窗口当起点——更早的已经没了，救不回来。
    # 归档只存 role/content（分段是模型内部格式，不给回看页看）。
    try:
        archive_rows = [{"role": m["role"], "content": m["content"]}
                        for m in raw_hist[-2:]]
        svc._append_archive(key, archive_rows,
                        seed=svc.trim(raw_hist[-svc.HISTORY_LIMIT:]))
    except Exception:
        svc.log.exception("归档失败 %s", key)

    # 情绪与状态落盘（2026-11-02）：只覆盖不累加——情绪是当下的，
    # 状态是这一格的结果，下一轮读 `state` 续着演。
    svc._save_mood(key, mood, intensity, state, prev_state, outfit=outfit)

    svc._bump_turn(key)
    # 长期记忆（2026-11-05）：先攒进缓冲，每 N 轮才在后台提炼成事实入库。
    # 只写用户可见的话 + 她的回复；「想」不进去（见 `svc._visible_user_text`）。
    # 自定义模型配置要在这里取一份：后台线程没有请求线程的线程本地覆盖。
    if rag_on:
        try:
            svc.persona_rag.remember(
                key, svc._visible_user_text(segments, said), reply,
                custom=svc.llm.get_custom_override(),
                day=day_before,
                phase=str((gd or {}).get("phase") or ""),
                card_name=svc._card_label(card))
        except Exception:                                # noqa: BLE001
            svc.log.exception("回忆入库失败 %s", key)
    return svc.Turn(reply, scene, mood, intensity, state, aff_res, finish)

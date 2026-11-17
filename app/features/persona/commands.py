"""角色扮演命令分发：把一条消息变成动作或回包。"""


def handle_command(session_key, text, addressed=False, quoted_ids=None):
    """认扮演命令。返回要回的话；**不是命令返回 None**（照常往下走）。

    2026-11-05 用户改口：所有命令**必须带 `#` 或 `/` 前缀**，避免日常聊天
    里的「妹妹」「清空」「好感」被误触。认这几种形态：

      - `#和泉纱雾` / `/和泉纱雾`           → 进角色
      - `#角色扮演` / `/角色扮演`           → 列清单
      - `#出戏` / `/出戏`                   → 退出
      - `#角色扮演，你是我的白兔妹妹…`      → 进角色（命令词后面带描述也认）

    ⚠️ 例外只有一个：删档第二句的确认句（「我同意清空存档」）本身不是
    打命令，而是**引用警告后回的一句话**，不要求前缀——它已经有引用闸门。

    `addressed`（2026-11-03）：群里这条消息**确实在叫机器人**（真 @，或者
    句子里喊了「大大怪」）。为真时先把开头的 `@某人` / 名字剥掉——
    不剥的话「@大大怪 #青梅竹马」因为不以 `#` 开头，整条命令被当成普通
    聊天，落到 direct_gen 被当成画面需求。

    `quoted_ids`（2026-11-05）：这条消息引用到的消息 id 列表。只有「清空存档」
    的第二步用得上——确认句必须**引用它那条警告**才生效。
    """
    from app.features.persona import service as svc

    t = svc._strip_prefix(text, addressed)
    if not t:
        return None

    # 删档确认句：不要求前缀（见 docstring）。引用闸门在 `svc.wipe_confirm` 里。
    if t == svc.WIPE_CONFIRM:
        if not str(session_key or "").startswith("private"):
            return None
        return svc.wipe_confirm(session_key, quoted_ids)

    body = svc._command_body(text, addressed)
    if body is None:
        # 没带前缀的普通聊天：上一步的删档确认作废。不这样的话，玩家
        # 12:00 发了「#清空存档」又反悔聊了两句，12:01 引用警告回一句
        # 「我同意清空存档」就真删了——两分钟的窗口不该这么宽。
        svc._WIPE_PENDING.pop(session_key, None)
        return None

    if body in svc.LIST_WORDS:
        # 私聊才带网页入口（2026-11-03 用户拍板）。群里发外链容易被风控、
        # 而且一次就刷屏，所以群里只回规则本身。
        url = ""
        if str(session_key or "").startswith("private"):
            try:
                from app.features.qq import friend_site
                url = friend_site.url()
            except Exception:                      # noqa: BLE001
                url = ""
        return svc.roster_text(site_url=url)
    if body in svc.EXIT_WORDS:
        if not svc._entry(session_key):
            return None                     # 本来就没在演，当普通消息
        svc.leave(session_key)
        return "出戏了，我又变回%s了。" % svc.QQ_BOT_NAME

    if body in svc.CLEAR_WORDS:
        if not (svc._entry(session_key) or svc._entry(svc.resolve_line(session_key))):
            return None                     # 没在演时不算命令，免得误触
        # 2026-11-05：不再直接删——和「清空存档」一样先亮红灯、要引用确认。
        return svc.wipe_request(session_key)

    # ── 攻略模式命令（2026-11-03）────────────────────────
    # 只在私聊认：群聊不扮演（上面群聊分支会拦），而且「好感」这种词在
    # 群聊日常里太常见，整条消息才认已经够了，私聊再放行。
    # 词表见文件头 svc.RAID_WORDS 一带；细节在 app/svc.affection.py。
    if str(session_key or "").startswith("private"):
        line = svc.resolve_line(session_key)
        if body in svc.RAID_WORDS:
            return svc.affection.set_mode(line, True)
        if body in svc.FREE_WORDS:
            return svc.affection.set_mode(line, False)
        if body in svc.SKIP_WORDS:
            turn = svc.skip_day(session_key)
            if turn is None:
                return None             # 没在演：当普通消息，不误触
            return turn.reply
        if body in svc.WIPE_WORDS:
            return svc.wipe_request(session_key)
        # 说了别的话：上一步的确认作废。不这样的话，玩家 12:00 发了
        # 「#清空存档」又反悔聊了两句，12:01 引用警告回一句
        # 「我同意清空存档」就真删了 —— 两分钟的窗口不该这么宽。
        svc._WIPE_PENDING.pop(session_key, None)
        if body in svc.MERCY_WORDS:
            return svc.affection.mercy(line)
        if body in svc.AFF_WORDS:
            return svc.affection.detail_text(line)
        if body in svc.ACH_WORDS:
            return svc.affection.achievement_text(line)
        if body in svc.STATUS_WORDS:
            return svc.affection.panel_text(line)

    # `#角色扮演 青梅竹马` / `#角色扮演，你是我的白色小兔妹妹`：
    # 剥掉命令词，剩下那段先当卡名找；找不到再看整条是不是卡名。
    target = body
    for w in svc.LIST_WORDS:
        if body.startswith(w):
            rest = body[len(w):].strip(" ，,、:：")
            if rest:
                target = rest
            break

    card = svc.find_card(target)
    if card is None and target != body:
        # 「角色扮演，你是我的白色小兔妹妹…」这种没写准卡名的，从描述里找。
        card = svc._match_card_in_text(target)
    if card is None:
        return None

    # ── 群聊不实际扮演，只做引导（2026-11-03 用户反馈）────────────
    #
    # 起因：群友在群里跟机器人聊，**其他群成员能直接看到全部内容**。
    # 状态隔离（`_persona_key`）一直是好的，隔离的是记忆和好感；但机器人
    # 发到群里的消息本来就是全群可见，这是 QQ 群聊的固有属性，压不住。
    #
    # 用户玩的又是亲密内容，公开出去不合适。所以群里不再开演，改成把话
    # 说清楚 + 给私聊/网页的去处。
    #
    # 群里发「角色扮演」看规则仍然允许（规则本身不敏感），只有真正「切进
    # 某个角色」这一步被拦下来。
    if str(session_key or "").startswith("group_"):
        return svc._group_redirect(card)

    was = svc.active_card(session_key)
    # 分线切换（2026-11-03 方案 B）：写线键 + base 指针。切回演过的角色时
    # 那条线的小记/历史原样保留——「接着上次聊」。
    svc.switch_line(session_key, card)
    if was and was["id"] == card["id"]:
        return "我现在就是%s呀。" % card["name"]
    # 默认攻略模式（2026-11-04 用户要求）：进来就先有一份好感记录，
    # 网页右栏 / QQ 尾行立刻有东西可看；顺手把玩法讲明白，别让玩家自己猜。
    try:
        svc.affection.status(svc.resolve_line(session_key))
    except Exception:                                  # noqa: BLE001
        svc.log.warning("进入扮演时建好感记录失败 %s", session_key)
    return ("好，从现在起我是%s（攻略模式）。每轮回话末尾会带一行你俩现在的关系，"
            "想关掉就发「#自由模式」。想让我变回来就发「#出戏」。" % card["name"])

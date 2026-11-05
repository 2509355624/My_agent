"""攻略模式：好感度数值层（2026-11-03）。

## 它是什么

自由模式（现状）完全不经由这个模块。攻略模式下，两段式扮演的第一段
（意图判断）会顺带输出一个打分 JSON，这里负责把分数**结算**成分线状态里
持久的好感值，并按档位给出要注入主对话的提示词。

设计原则（和整个项目一致）：

  - **判断交给 AI**：这轮算多少分、算聊天还是剧情，由第一段模型斟酌；
  - **代码只做检测和记账**：夹单轮上限、降档滞回、越线判结局；
  - **不设按天的时间闸**（2026-11-04 晚）：聊天和剧情同权，玩家一次会话里
    能一路推到底，不存在「今天涨够了明天再来」；
  - 档位**文案**全部来自 `config/affection_tiers.json`（用户自己配），
    机制层只认 key / 区间 / aftercare 标记，不掺和内容。

## 数值与档位

好感 -50 ~ +100，初始 0。11 个行为档 + 1 个结局（进局子）。

**2026-11-04 晚：毕业结局退休**。原来 ≥50 就「毕业」、数值锁死，用户实测
20 轮就到了——到了反而没得玩。现在 50 只是**里程碑**（在一起，见
`config.milestones`），触发一次、不锁数值，接着往上走；100 到顶也不收场。
理由很直接：这类玩法要的是「一直有下一步」，不是一个奖状。

结局只剩**越线进局子**（≤-50），触发后数值锁定，直到「清空存档」
（回到这张卡的起点）或切回自由模式（封存原值）。切自由模式会**顺手解除结局**
（2026-11-04）：不解除的话，切回攻略模式的第一轮结算会拿同一个越线值
再判一次结局，玩家等于被关在局子里出不来。

## 状态长什么样

存在 persona 分线条目的 `affection` 字段（没有这个字段 = 自由模式）：

    {"mode": "raid" | "free", "value": 12, "tier": "touch",
     "down": 0, "ending": "" | "jail",
     "milestone": "" | "together",
     "excite": 0, "scene": false,
     "day": "2026-11-03", "chat": 0, "acts": 0,
     "hist": [{"d": 3, "r": "原因", "ts": "..."}]}

`mode` 为 "free" 时数值**封存不删**——切回 raid 原值接着来。
"""
import copy
import datetime
import json
import logging
import os

from app.config import BASE_DIR

log = logging.getLogger("affection")

MIN_V = -50
MAX_V = 100             # 2026-11-04 晚从 50 抬到 100：50 处只放「在一起」里程碑
INITIAL_V = 0
ROUND_CAP = 15          # 单轮加分绝对值上限（防一句话刷满）

# ── 身体进度（2026-11-04 深夜改版）──────────────────────
# 「你们到哪一步了」。0~6 级，**每一级的名字就是这一级能做什么**：
# 还没碰过 / 能碰到了 / 能抱了 / 亲过了 / 碰过了 / 做过了 / 要不够。
#
# 改名的理由：原来的名字（隔得很远 / 点头之交 / 说得上话 / 说得进心里 /
# 靠得很近 / 不分彼此）是**社交距离量表**，描述「多熟」不描述「能做什么」。
# 玩家站在第 3 级，看不出这一级解锁了什么、下一级要做什么，只能靠猜。
#
# 为什么从 5 级加到 6 级：用户指出「亲过了」和「做过了」之间少了一整级——
# 脱光、亲乳头、手指、用嘴，这些**都不是做爱但都过线了**，塞进任何一边
# 都会让那一级的文案自相矛盾。现在 l4「碰过了」专门放这些。
LEVEL_MAX = 6
AROUSAL_MAX = 100       # 冲动值：她此刻有多想，持续性气氛值
AROUSAL_DECAY = 15      # 每轮回落多少（不去拱它就会凉）
# ── 冲动与高潮的分工（2026-11-04 深夜）─────────────────
# 用户把这两根轴的语义彻底定死了：
#   冲动值 = 「她此刻想不想做、想多狠」。**持续性**气氛值，靠喝酒/独处/
#            触碰/情话/深夜拱起来，每轮自然回落。它和羁绊**一起**决定
#            她接不接得住（见 `_can_cross`）——这是双轴门槛的核心。
#   高潮值 = 「这一场里她到哪了」。只在场景内累积，按**这一轮动作的强度**
#            给基数（前戏/手指/嘴/进入/抽送/加重），到 100 触发一次高潮、
#            回落到余韵值，单场最多 5 次，用满强制收场。
# 旧名 `excite`/`兴奋度` 保留成字段名兼容老存档，但对外一律叫「高潮值」。
EXCITE_MAX = 100
EXCITE_START = 15       # 刚进入场景时的起步值：不是从 0 开始，她已经有反应了
ORGASM_AFTER = 55       # 高潮之后的余韵值：不是回 0，是回到还想要的半路
ORGASM_MAX = 5          # 单场最多几次。用满强制收场——不只是她受不了，
                        # 更是再写下去就是水词了，第 6 次不会比第 5 次好看
# ── 场景中断后的处理（2026-11-05 用户拍板）──────────────────
# 原来一判「不在场景」就把高潮值和次数当场清零，线上表现是「上一轮还 70，
# 插一句日常话就没了」。现在分两层：
#   · 单轮没有身体接触 = **空闲轮**，不算收场，只让数值自然往下走一点点；
#   · 连续 ORGASM_IDLE_END 轮都没有接触，才算这一场真的结束。
# 降多少由这一轮的场景 AI 判（它给负的 excite 增量）；没给就按这个常量。
# 也**不再有冷却期**——用满 5 次这一场就收，下一轮想开随时能开。
ORGASM_IDLE_DECAY = 8   # 空闲轮的默认回落（AI 没给负值时用它）
ORGASM_IDLE_END = 2     # 连续几轮空闲才算真收场
# 越界被拒的代价，按当晚第几次递增（不冻结、不降级——自己哄回来）
CROSS_PENALTY = (3, 6, 10)
CROSS_HARD = 20         # 她说了不要还继续：一次就够狠
CROSS_JAIL_AT = 2       # 强行几次触发报警（这条线结束，只能重开）

# ── 戒备（2026-11-05，用户拍板的「爽要带代价」）─────────────
# 「他对我这么好，是不是有所图」。独立于好感的一根弦：
#   - 涨：关系浅却做远超关系的事（喊老婆、直接上手、带去私密空间）、
#         高频物质投入、无铺垫的表白——判据在打分 prompt 的 suspicion 字段。
#   - 降：行为和关系相称、兑现承诺、把误会圆回来（模型给负值）。
# 效果两层：prompt 层（注入她的戒备状态，模型自己演冷淡/试探）+ 代码层保底
# （戒备高时正 delta 减半——模型手松也不能让戒备变成摆设；负分不打折）。
# 区间 0~40 就够：它是个「减速带」，不是第二套好感，做满 100 等于凭空
# 再养一条进度条。
SUSPICION_MAX = 40
SUSPICION_DISCOUNT_AT = 20   # 从这个值起，正 delta 减半
SUSPICION_HARD_FLOOR = 8     # hard 越界这轮，戒备至少涨这么多（模型手松的保底）
SUSPICION_NO_FLOOR = 4       # mild 越界被明确拒绝：至少 +4
# 2026-11-04 晚：**按天的额度全部取消**。原来剧情每天 5 次大额、纯聊天按
# 「当天第几次」递减（3/2/1/1 之后每 3 轮 +1），造成的实际效果是「今天补不完，
# 明天再来」——用户拍板去掉：「没有人喜欢一个限制你 24 小时的游戏」。
# 现在聊天和剧情同权，每轮按模型给的 delta 走，只留 ROUND_CAP 兜底。
# 想再调节奏就改 ROUND_CAP，不要再往回合数上按天加闸。
DOWN_CONFIRM = 2        # 降档要连续 N 轮确认（防阈值边缘反复横跳）
HIST_KEEP = 8           # 每条线留最近 N 条变化记录（给「好感」卡片看）

MODE_RAID = "raid"
MODE_FREE = "free"

# 结局 key → 触发线
# 2026-11-04 晚：`graduated`（毕业）退休。它原来的语义是「好感触顶 = 故事收场」，
# 但用户实测 20 轮就到、到了反而没得玩。现在 50 只是里程碑（`MILESTONE_TOGETHER`），
# 不锁数值；100 到顶也不收场。只剩「进局子」这一条真结局。
ENDING_JAIL = "jail"
# 里程碑（不是结局）：到点触发一次，记在 rec["milestone"] 里，回头不再重复。
MILESTONE_TOGETHER = "together"

# 「再给我一次机会」（2026-11-04 用户定的）：进局子之后能真的重来一次，
# 但每人只给 MERCY_MAX 次，**按每条角色线各算**。
# 存在的理由不是宽容，是防人跑：只给「清空存档」的话，玩家在进局子那一刻
# 会以为存档要没了，索性不玩了。用完就只剩自由模式（封存）和重新开始。
MERCY_MAX = 3

# ── 默认配置（config 文件缺失/坏掉时的兜底；prompt 留空 = 该档只注入档位名）──
DEFAULT_CONFIG = {
    "min": MIN_V, "max": MAX_V, "initial": INITIAL_V,
    "tiers": [
        {"key": "devoted", "min": 75, "name": "交给你了",  "name_soft": "认准你了",
         "prompt": ""},
        {"key": "lover",   "min": 50, "name": "在一起了",  "name_soft": "心里有你",
         "prompt": ""},
        {"key": "full",   "min": 40,  "name": "随便凿",   "prompt": ""},
        {"key": "care",   "min": 30,  "name": "凿+关心",  "prompt": "",
         "aftercare": True},
        {"key": "start",  "min": 20,  "name": "开始凿",   "prompt": ""},
        {"key": "touch",  "min": 10,  "name": "肢体接触", "prompt": ""},
        {"key": "normal", "min": 0,   "name": "正常关系", "prompt": None},
        {"key": "miffed", "min": -10, "name": "小反感",   "prompt": ""},
        {"key": "cold",   "min": -20, "name": "爱答不理", "prompt": ""},
        # 负分两档（2026-11-04 改）：原来叫「强制爱」「堕落模式」，名字和文案
        # 都是「扭曲占有 / 自我毁掉」，等于告诉模型这时候可以更进一步——可
        # 机制上负分区间 value >= guard - 冲动//5 永远不成立（guard 最低 15），
        # 她根本接不住，文案和判定当场打架。这两档的真实含义是**赎罪期**：
        # 她在算账、在考他，身体上的靠近一律挡掉。名字改成大白话，让玩家
        # 一眼看懂「还有救，但要先把账还了」。
        {"key": "forced", "min": -30, "name": "在等你认错", "prompt": ""},
        {"key": "fallen", "min": -50, "name": "随时会走",   "prompt": ""},
    ],
    "endings": {
        "jail": {"at": -50, "name": "进局子", "prompt": ""},
    },
    # 里程碑兜底：config 缺了也不会因为「找不到 together 的文案」而崩
    "milestones": {
        "together": {"at": 50, "name": "在一起", "prompt": ""},
    },
    # 冲动档位兜底（2026-11-04 深夜改版）：五档，词义从「心里乱」改成
    # **「想不想做」**。旧的四档（平静/有点乱/很乱/不设防）描述的是心绪，
    # 玩家看不出「她现在能不能推到」，也看不出离门槛还差多少。
    "arousal_levels": [
        {"key": "burning", "min": 90, "name": "忍不住了", "short": "她这会儿自己会动手。"},
        {"key": "hot",     "min": 70, "name": "很想",     "short": "她眼神已经不一样了。"},
        {"key": "warm",    "min": 45, "name": "想",       "short": "她在往你这边靠。"},
        {"key": "mild",    "min": 20, "name": "有点想",   "short": "她比刚才近了半步。"},
        {"key": "calm",    "min": 0,  "name": "没什么",   "short": "她跟平时一样。"},
    ],
    # 高潮值兜底（2026-11-04 深夜）：旧名「兴奋度」，五档按**身体信号**排。
    # 旧四档（还没开始/有点发紧/跟着你走/快不行了）描述的是「状态」，
    # 新五档描述的是「到她身上发生到什么程度了」，写文时可以直接照着落笔。
    "orgasm_levels": [
        {"key": "over",   "min": 90, "name": "到了",     "prompt": ""},
        {"key": "edge",   "min": 75, "name": "快要",     "prompt": ""},
        {"key": "rising", "min": 55, "name": "上来了",   "prompt": ""},
        {"key": "awake",  "min": 30, "name": "有反应了", "prompt": ""},
        {"key": "cold",   "min": 0,  "name": "还没起来", "prompt": ""},
    ],
    "orgasm_hints": {
        "start": EXCITE_START, "max": EXCITE_MAX,
        "max_orgasm": ORGASM_MAX, "after": ORGASM_AFTER,
        # 这张表退成**兜底**：正常轮次涨多少由场景 AI 给的 excite 增量决定，
        # 只有当它没给（或给了 0）时才按动作档位记。
        "base": {"foreplay": 12, "hand": 22, "mouth": 28,
                 "enter": 25, "thrust": 32, "intense": 40, "stop": -10},
    },
    # 堕落 / 屈服兜底（2026-11-04 深夜）：五档，和 config 里的词对齐。
    # 堕落**只涨不掉**（soothe 是唯一的口子），屈服会回涨——两根轴的落差
    # 才是这类剧情好看的地方，别把它们的涨跌规则做成一样。
    #
    # ⚠️ `prompt` 这里**必须带正文**，不能像别的兜底表那样留空串。
    # 这三个字读起来像注释，其实是踩出来的：配置坏了的时候，堕落这段
    # 注入会只剩一个档位名（「认了」），模型拿到「认了」三个字不知道该
    # 演成什么样，整根轴等于失效。兜底文案短一点没关系，不能没有。
    "corruption_levels": [
        {"key": "unbroken", "min": 0, "name": "还是她",
         "prompt": "被逼到的时候她会反抗、会骂、会记仇，事后还会哭。"},
        {"key": "cracked", "min": 25, "name": "开始松",
         "prompt": "嘴上还硬，力气先没了；开始给自己找理由，难受的时间变短。"},
        {"key": "giving", "min": 50, "name": "认了",
         "prompt": "不再每次都推，推两下就放；刻意不去想这是被逼的。"},
        {"key": "sinking", "min": 70, "name": "陷进去",
         "prompt": "开始主动凑近，然后自己被吓到；怕的是发现自己竟然想要。"},
        {"key": "ruined", "min": 90, "name": "回不去了",
         "prompt": "不再解释也不再拦，会自己开口要；最糟的是她不再哭了。"},
    ],
    "submission_levels": [
        {"key": "proud", "min": 0, "name": "不服",
         "prompt": "顶嘴、拖延、阳奉阴违，眼神不躲。"},
        {"key": "reluctant", "min": 25, "name": "勉强",
         "prompt": "照做了，但动作里带着不服气。"},
        {"key": "yielding", "min": 50, "name": "让了",
         "prompt": "不争也不主动，让做什么做什么，做完不吭声。"},
        {"key": "trained", "min": 70, "name": "听话",
         "prompt": "会猜你想听什么，先一步把话递上来，做完还等你反应。"},
        {"key": "owned", "min": 90, "name": "认主",
         "prompt": "不用开口她就动了，自己给自己定规矩，还怕做得不够。"},
    ],
    "corrupt_hints": {
        "up": {}, "submit_up": {}, "soothe": -10,
    },
    # 身体进度兜底（2026-11-04 深夜）：名字 = 这一级能做什么。
    # `arousal_gate` 是新增的第二道门槛——见 `_can_cross`。
    "intimacy": {"levels": [
        {"key": "l0", "name": "还没碰过", "gate": 0,  "arousal_gate": 0,  "guard": 50, "prompt": ""},
        {"key": "l1", "name": "能碰到了", "gate": 10, "arousal_gate": 20, "guard": 46, "prompt": ""},
        {"key": "l2", "name": "能抱了",   "gate": 20, "arousal_gate": 35, "guard": 40, "prompt": ""},
        {"key": "l3", "name": "亲过了",   "gate": 30, "arousal_gate": 50, "guard": 34, "prompt": ""},
        {"key": "l4", "name": "碰过了",   "gate": 40, "arousal_gate": 65, "guard": 28, "prompt": ""},
        {"key": "l5", "name": "做过了",   "gate": 50, "arousal_gate": 77, "guard": 20, "prompt": ""},
        {"key": "l6", "name": "要不够",   "gate": 65, "arousal_gate": 88, "guard": 14, "prompt": ""},
    ]},
    # 压迫模式（2026-11-05，用户拍板）：好感低 + 堕落高 + 气氛/身体在动。
    # 这三件事同时成立，才是「她不是愿意的，可她拦不住自己」。
    # 每一档堕落降一次门槛——第一次要堆满气氛，第二次只要一半。
    # 压迫模式（2026-11-05，用户拍板）：好感低 + 堕落高 + 气氛/身体在动。
    # 这三件事同时成立，才是「她不是愿意的，可她拦不住自己」。
    # 每一档堕落降一次门槛——第一次要堆满气氛，第二次只要一半。
    "forced": {
        "_note": "压迫模式的门槛。value 好感上限 / corrupt 堕落下限 / arousal 冲动下限；"
                 "discount 是每跨一个堕落档位，冲动门槛降多少。",
        "value_max": 20,
        "corrupt_min": 45,
        "arousal_min": 30,
        "discount": 12,
        "corrupt_steps": [25, 50, 70, 90],
    },
    # 开发度（2026-11-05，用户拍板）：五个部位，**不设上限，500 当满格**。
    # 涨多少由打分 AI 按「这一轮动作的激烈程度」判，代码只做夹值和敏感系数。
    # 满格（五处都 500）= 身体开发 100%：碰一下就来，她自己都怕。
    "dev": {
        "_note": "step 是单轮单部位上限（防止模型一笔给爆），gain 是满格时的敏感倍数。",
        "max": 500,
        "step": 25,
        "gain": 1.5,
        "levels": [
            {"key": "full",   "min": 500, "name": "开发满了",
             "prompt": "碰一下整个人就软。皮肤先红，水自己出来，腰跟着手走。她会主动要，"
                       "要得比你还急；不用你开口，她自己找地方、自己脱。"},
            {"key": "wild",   "min": 400, "name": "阈值乱了",
             "prompt": "不用碰到也能起来。看见、听见、想起，都能让她当场软掉；"
                       "她自己都怕这个反应，又挡不住。"},
            {"key": "hooked", "min": 300, "name": "离不开",
             "prompt": "在不相干的场合也会想。看到别人亲近就湿，会自己把身体往你那边送，"
                       "会拉着你的手往那里放，会用外套盖住不想被人看见。"},
            {"key": "wants",  "min": 200, "name": "会要了",
             "prompt": "会自己凑、会开口要这个，说完自己脸红但不收回；被弄到会自己跟着动。"},
            {"key": "reacts", "min": 100, "name": "起反应",
             "prompt": "被碰到会突然安静一下，装作没事，呼吸先变；嘴上不认，身体答了。"},
            {"key": "virgin", "min": 0,   "name": "没碰过",
             "prompt": "这里还没被开发过。碰了会躲、会夹、会说是第一次——按她的性格来。"},
        ],
    },
}


def config_path():
    return os.path.join(str(BASE_DIR), "config", "affection_tiers.json")


def load_config():
    """读档位配置。文件缺失或 JSON 坏了就用默认档位（机制不能因配置挂掉）。"""
    try:
        with open(config_path(), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return DEFAULT_CONFIG
    if not isinstance(d, dict) or not isinstance(d.get("tiers"), list):
        return DEFAULT_CONFIG
    return d


def tier_for(value, cfg=None):
    """数值 → 行为档（取 min ≤ value 的最大那一档）。不在区间返回最边缘档。"""
    cfg = cfg or load_config()
    tiers = sorted(cfg.get("tiers") or [], key=lambda t: t.get("min") or 0,
                   reverse=True)
    for t in tiers:
        try:
            lo = int(t.get("min") or 0)
        except (TypeError, ValueError):
            continue
        if value >= lo:
            return t
    return tiers[-1] if tiers else None


def bounds(cfg=None):
    """好感的上下限 `(hi, lo)`。config 可覆盖（越线判定和「退出结局」都用它）。"""
    cfg = cfg or load_config()
    try:
        hi = int(cfg.get("max") if cfg.get("max") is not None else MAX_V)
        lo = int(cfg.get("min") if cfg.get("min") is not None else MIN_V)
    except (TypeError, ValueError):
        hi, lo = MAX_V, MIN_V
    return hi, lo


def ending_for(value, cfg=None):
    """越线判定。**2026-11-04 深夜：结局全部拿掉了，这里恒返回 None。**

    用户拍板：想玩胁迫和半推半就，那两条线必然被判成 `cross=hard`；
    而旧规则是「hard 累计 2 次 → 进局子、数值锁死」，等于在最想玩的那条
    线上装了个只响两次的警报。玩到一半被强制收场，比没有处罚更难受。

    现在越界只有**软后果**：掉羁绊 + 推高堕落（见 `settle` 里那段），
    故事接着走。羁绊跌到下限也不收场——她可以很恨你，戏还能演。

    函数留着不是因为还有别的结局，是因为调用方（`settle` /
    `injection_block` / `tail_line`）和一堆测试都按「可能返回结局 key」
    写的。留着这个口子，以后想加真结局（不是报警那种）不用再把调用链
    改一遍。
    """
    return None


def milestone_for(value, rec, cfg=None):
    """跨过里程碑线时返回它的 key（已经触发过就返回空串）。

    和结局的区别只有一个：**不锁数值**。触发一次、记在 `rec["milestone"]` 里，
    回头不再重复——它的作用是给一段叙事，不是给一个奖状。
    """
    rec = rec if isinstance(rec, dict) else {}
    got = str(rec.get("milestone") or "")
    for key, m in (cfg_milestones(cfg) or {}).items():
        if not isinstance(m, dict):
            continue
        at = m.get("at")
        if at is None:
            continue
        try:
            at = int(at)
        except (TypeError, ValueError):
            continue
        if int(value or 0) >= at and got != key:
            return key
    return ""


def cfg_milestones(cfg=None):
    """里程碑配置表（key → {at, name, prompt}）。去掉注释键。"""
    cfg = cfg or load_config()
    raw = cfg.get("milestones")
    if not isinstance(raw, dict):
        raw = DEFAULT_CONFIG.get("milestones") or {}
    return {k: v for k, v in raw.items()
            if not str(k).startswith("_") and isinstance(v, dict)}


def milestone_cfg(key, cfg=None):
    return cfg_milestones(cfg).get(str(key or "")) or {}


def ending_cfg(key, cfg=None):
    cfg = cfg or load_config()
    return (cfg.get("endings") or {}).get(key) or {}


# ── 距离层级 ──────────────────────────────────────────

def levels_of(cfg=None):
    """六级的配置列表，按 0→5 顺序。配置坏了回退默认表。"""
    cfg = cfg or load_config()
    lst = (cfg.get("intimacy") or {}).get("levels")
    if not isinstance(lst, list) or not lst:
        lst = (DEFAULT_CONFIG.get("intimacy") or {}).get("levels") or []
    return lst


def level_cfg(n, cfg=None):
    """第 n 级（0~5）的配置；越界取边缘。"""
    lst = levels_of(cfg)
    if not lst:
        return {}
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    return lst[max(0, min(len(lst) - 1, n))]


def level_name(n, cfg=None, card=None):
    """这一级在**这张卡**里叫什么。卡片给了 level_names 就用卡片的
    （骨科卡第 1 级可能叫「妹妹」），没给就用通用名。"""
    names = (card or {}).get("level_names") if isinstance(card, dict) else None
    if isinstance(names, list) and 0 <= int(n or 0) < len(names):
        got = str(names[int(n or 0)] or "").strip()
        if got:
            return got
    return str(level_cfg(n, cfg).get("name") or "")


def _intimacy_label(value, g):
    """亲密度那一句人话（QQ 尾行和网页共用，别在两边各写一套）。

    - 负分：只报分——这时候没有「下一档」可言，赎罪期看的是能不能回到 0；
    - 到顶：`42 到顶`；
    - 分数够但没收成：`够了 · 差一步`。**不写数字比**：好感能涨到 50，
      等级却卡在 l1 的 `50/12`，分子比分母大，看着像 bug。这句话在说的是
      「分够了，那场戏还没做成」（升一级要 `beat` + 门槛两个条件），不是卡住；
    - 其余：`21/30 · 还差 9`。
    """
    v = int(value or 0)
    if g.get("maxed"):
        return "%d 到顶" % v
    if v < 0 or not g.get("nextGate"):
        return "%d" % v
    if int(g.get("need") or 0) <= 0:
        return "够了 · 差一步"
    return "%d/%d · 还差 %d" % (v, int(g.get("nextGate") or 0),
                                int(g.get("need") or 0))


def _next_tier_gate(value, cfg=None):
    """关系等级满级之后，用**档位门槛**接着算「下一站」。

    为什么需要它：等级只有 6 档（门槛 0/5/12/20/30/40），好感上限却是 100。
    升到「不分彼此」之后等级不再变，如果这里直接报「到顶」，玩家会以为
    后面没内容了——而 50（在一起了）、75（交给你了）两档恰恰在后面。
    所以满级后把进度交给档位算：45/50 → 55/75 → 85 到顶。
    """
    cfg = cfg or load_config()
    tiers = [t for t in (cfg.get("tiers") or [])
             if isinstance(t, dict) and int(t.get("min") or 0) >= 0]
    tiers.sort(key=lambda x: int(x.get("min") or 0))
    for t in tiers:
        if int(t.get("min") or 0) > int(value or 0):
            return int(t.get("min") or 0), str(t.get("name") or "")
    return 0, ""


def intimacy_gauge(level, value, card=None, cfg=None):
    """亲密度进度：从**这一档的门槛**走到**下一档的门槛**。

    2026-11-04 用户报「只看到档名叫什么，不知道怎么触发后续剧情」——
    档名（家里的人 → 靠得很近）本身在变，但玩家看不出还差多远。这里只报
    事实：这一档的下限、下一档叫什么、还差几分。**升不升还是模型说了算**，
    分数只是门票，`beat`（这轮做成了那件事）照样要过。

    2026-11-04 晚：上限抬到 100 之后，等级满级（不分彼此）**不等于到顶**——
    50 / 75 两个档位还在后面。所以满级时改用档位门槛计算，见 `_next_tier_gate`。

    返回 dict：
        gate      这一档的门槛 = 档内进度的起点
        nextGate  下一档门槛；真的到顶时等于 gate
        nextName  下一档叫什么；到顶时空串
        need      还差几分到下一档（分数已够 / 到顶时为 0）
        pct       档内进度 0~100（低于本档门槛时压到 0，到顶 100）
        maxed     已经到顶
        label     给人看的一句，见 `_intimacy_label`
    """
    cfg = cfg or load_config()
    try:
        lv = int(level or 0)
    except (TypeError, ValueError):
        lv = 0
    lv = max(0, min(LEVEL_MAX, lv))
    gate = int(level_cfg(lv, cfg).get("gate") or 0)
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        v = 0

    if lv >= LEVEL_MAX:
        # 等级满级：下一站改由档位给。档位也没了，才算真的到顶。
        ngate, nname = _next_tier_gate(v, cfg)
        if not ngate:
            g = {"gate": gate, "nextGate": gate, "nextName": "",
                 "need": 0, "pct": 100, "maxed": True}
            g["label"] = _intimacy_label(v, g)
            return g
        # 档内起点 = 当前所在档位的门槛（比等级门槛更贴），没有就用等级门槛
        cur = 0
        for t in (cfg.get("tiers") or []):
            if isinstance(t, dict) and int(t.get("min") or 0) >= 0 \
                    and int(t.get("min") or 0) <= v:
                cur = max(cur, int(t.get("min") or 0))
        span = max(1, ngate - cur)
        pct = int(round((v - cur) * 100.0 / span))
        g = {"gate": cur, "nextGate": ngate, "nextName": nname,
             "need": max(0, ngate - v),
             "pct": max(0, min(100, pct)), "maxed": False}
        g["label"] = _intimacy_label(v, g)
        return g

    ngate = int(level_cfg(lv + 1, cfg).get("gate") or 0)
    span = max(1, ngate - gate)          # 配置被改成同级门槛也别除零
    pct = int(round((v - gate) * 100.0 / span))
    g = {"gate": gate, "nextGate": ngate,
         "nextName": level_name(lv + 1, cfg, card),
         "need": max(0, ngate - v),
         "pct": max(0, min(100, pct)), "maxed": False}
    g["label"] = _intimacy_label(v, g)
    return g


# ── 冲动档位（2026-11-04）─────────────────────────────

def arousal_levels(cfg=None):
    """冲动四档配置，按 min 从高到低。配置坏了回退默认表。"""
    cfg = cfg or load_config()
    lst = cfg.get("arousal_levels")
    if not isinstance(lst, list) or not lst:
        lst = DEFAULT_CONFIG.get("arousal_levels") or []
    return sorted(lst, key=lambda x: int(x.get("min") or 0), reverse=True)


def arousal_level(v, cfg=None):
    """冲动值 → 它落在哪一档（返回整条配置）。"""
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    v = max(0, min(AROUSAL_MAX, v))
    for l in arousal_levels(cfg):
        if v >= int(l.get("min") or 0):
            return l
    return {}


def arousal_name(v, cfg=None):
    """冲动值 → 给玩家看的那个词（没什么 / 有点想 / 想 / 很想 / 忍不住了）。

    2026-11-04 深夜改语义：这五个词说的都是**「她想不想做」**，不是心绪。
    旧版（平静/有点乱/很乱/不设防）玩家看不出离门槛还差多少，也不知道
    「不设防」是能推还是不能推。
    """
    return str(arousal_level(v, cfg).get("name") or "")


# ── 高潮值档位（2026-11-04 深夜，原「兴奋度」）──────────
# 和冲动分工：冲动 = 她**想不想**（持续性气氛，决定接不接得住）；
# 高潮值 = 她**到哪了**（只在场景内，决定这一拍怎么写）。
# 五档全是身体信号（还没起来 / 有反应了 / 上来了 / 快要 / 到了），
# 写文时可以直接照着落笔，不需要再翻译一层。

def orgasm_levels(cfg=None):
    """高潮值五档，按 min 从高到低。配置坏了回退默认表。"""
    cfg = cfg or load_config()
    lst = cfg.get("orgasm_levels")
    if not isinstance(lst, list) or not lst:
        lst = DEFAULT_CONFIG.get("orgasm_levels") or []
    return sorted(lst, key=lambda x: int(x.get("min") or 0), reverse=True)


def orgasm_level(v, cfg=None):
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    v = max(0, min(EXCITE_MAX, v))
    for l in orgasm_levels(cfg):
        if v >= int(l.get("min") or 0):
            return l
    return {}


def orgasm_name(v, cfg=None):
    """高潮值 → 给玩家看的词。0 时返回空串（没在进行，不占一行）。"""
    try:
        if int(v or 0) <= 0:
            return ""
    except (TypeError, ValueError):
        return ""
    return str(orgasm_level(v, cfg).get("name") or "")


# 旧名兼容：老代码和老测试还在叫 excite_*，指向同一套实现
excite_levels = orgasm_levels
excite_level = orgasm_level
excite_name = orgasm_name


def orgasm_hints(cfg=None):
    """高潮值的记账参数：起步值、上限、单场次数、余韵值、空闲回落、动作档基数。"""
    cfg = cfg or load_config()
    raw = cfg.get("orgasm_hints")
    if not isinstance(raw, dict):
        raw = DEFAULT_CONFIG.get("orgasm_hints") or {}
    base = raw.get("base")
    base = base if isinstance(base, dict) else {}
    return {
        "start": int(raw.get("start") or EXCITE_START),
        "max": int(raw.get("max") or EXCITE_MAX),
        "max_orgasm": int(raw.get("max_orgasm") or ORGASM_MAX),
        "after": int(raw.get("after") if raw.get("after") is not None
                     else ORGASM_AFTER),
        # 中断后的自然回落（2026-11-05）：AI 没给负增量时按这个数往下走。
        "idle_decay": int(raw.get("idle_decay") if raw.get("idle_decay") is not None
                          else ORGASM_IDLE_DECAY),
        "base": {str(k): int(v) for k, v in base.items()},
    }


# 动作档位的别名表：模型可能回 act/motion/action 等几种写法，统一成
# 配置里 `base` 的 key。认不出来的按 foreplay 记（宁可少给，别给多）。
_ACT_ALIAS = {
    "foreplay": "foreplay", "前戏": "foreplay", "touch": "foreplay",
    "hand": "hand", "finger": "hand", "手指": "hand",
    "mouth": "mouth", "oral": "mouth", "嘴": "mouth", "口": "mouth",
    "enter": "enter", "insert": "enter", "进入": "enter",
    "thrust": "thrust", "抽送": "thrust", "piston": "thrust",
    "intense": "intense", "hard": "intense", "加重": "intense", "冲刺": "intense",
    "stop": "stop", "pause": "stop", "停": "stop", "中断": "stop",
}


def act_base(act, cfg=None):
    """这一轮**动作档位** → 高潮值基数。

    2026-11-04 深夜改法：以前是模型自己给 `excite` 增量（-40~+40），它想给
    多少给多少——换个模型、降一次智，曲线就全歪了。现在模型只判「这一轮
    在干什么」，涨多少由这张表说了算。
    """
    h = orgasm_hints(cfg)
    key = _ACT_ALIAS.get(str(act or "").strip().lower(), "foreplay")
    d = int(h["base"].get(key) or h["base"].get("foreplay") or 12)
    return key, d


# ── 堕落 / 屈服（2026-11-04 深夜，用户加的第五、第六根轴）──────
# 和羁绊的分工：
#   羁绊（value）   「她心里怎么看你」——会涨会跌，正常交流就能涨。
#   堕落（corrupt） 「她变成了什么样」——**只涨不掉**（除了主动安抚），
#                    越界、被拒后继续、事后不管她，都会推高。
#   屈服（submit）  「她此刻听不听话」——会掉，压一压就降下来。
#
# 为什么要有这两根：拿掉「进局子」之后，越界如果只是掉羁绊，那惩罚太软、
# 而且掉到 -50 就没戏可掉了。堕落值给了越界一个**不会饱和、也不会锁死**
# 的去处——账记在她身上，不在数字上。她可以很恨你（羁绊低），同时身体
# 已经完全驯服（堕落高）。这个落差就是这类剧情最有张力的地方。
CORRUPT_MAX = 100
SUBMIT_MAX = 100


def corrupt_levels(cfg=None):
    """堕落五档，按 min 从高到低。配置坏了回退默认表。"""
    cfg = cfg or load_config()
    lst = cfg.get("corruption_levels")
    if not isinstance(lst, list) or not lst:
        lst = DEFAULT_CONFIG.get("corruption_levels") or []
    return sorted(lst, key=lambda x: int(x.get("min") or 0), reverse=True)


def corrupt_level(v, cfg=None):
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    v = max(0, min(CORRUPT_MAX, v))
    for l in corrupt_levels(cfg):
        if v >= int(l.get("min") or 0):
            return l
    return {}


def corrupt_name(v, cfg=None):
    """堕落 → 词。0 时是「还是她」，不是空串——这根轴一直在，只是没动。"""
    return str(corrupt_level(v, cfg).get("name") or "")


def submit_levels(cfg=None):
    """屈服五档，按 min 从高到低。配置坏了回退默认表。"""
    cfg = cfg or load_config()
    lst = cfg.get("submission_levels")
    if not isinstance(lst, list) or not lst:
        lst = DEFAULT_CONFIG.get("submission_levels") or []
    return sorted(lst, key=lambda x: int(x.get("min") or 0), reverse=True)


def submit_level(v, cfg=None):
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    v = max(0, min(SUBMIT_MAX, v))
    for l in submit_levels(cfg):
        if v >= int(l.get("min") or 0):
            return l
    return {}


def submit_name(v, cfg=None):
    return str(submit_level(v, cfg).get("name") or "")


# ── 开发度（2026-11-05，用户加的第七组轴）─────────────────
#
# 用户原话：「把明面上的性器官都展示出来——嘴巴、乳头、小穴、肛门、身体，
# 编剧 AI 根据玩家动作的激烈程度去加，不设上限，500 当满格」。
#
# 和另外几根轴的分工：
#   羁绊   「她心里怎么看你」——双向
#   堕落   「她被怎么对待过」——只涨不掉
#   屈服   「她此刻听不听话」——会掉
#   开发度 「她身体被开发成什么样」——**只涨不掉、无上限**
#
# 为什么单列一组：堕落在讲「她愿不愿意」，开发度在讲「她身体现在是什么
# 状态」。她可以对你有 0 点好感、并且已经开发度 400——那正是压迫模式
# 最吃的一套数值（见 `is_forced`）。这两件事分开记，戏才写得出来。
#
# 数值用途有两处：
#   1. 敏感系数——同一个动作，开发度高的涨得更多（见 `sensitivity`）；
#   2. 编剧 AI 的勾子——300 以上她会在**无关的日常场景**里自己发作
#      （公交车、饭桌、看到别人牵手就湿）。这才是攻略模式压过自由模式的地方。
DEV_MAX = 500
DEV_KEYS = ("mouth", "nipple", "pussy", "anus", "body")
DEV_LABELS = {"mouth": "嘴巴", "nipple": "乳头", "pussy": "小穴",
              "anus": "肛门", "body": "身体"}
_DEV_ALIAS = {
    "mouth": "mouth", "嘴": "mouth", "嘴巴": "mouth", "口": "mouth",
    "oral": "mouth",
    "nipple": "nipple", "乳头": "nipple", "胸": "nipple", "breast": "nipple",
    "pussy": "pussy", "小穴": "pussy", "穴": "pussy", "阴道": "pussy",
    "vagina": "pussy",
    "anus": "anus", "肛门": "anus", "后穴": "anus", "anal": "anus",
    "body": "body", "身体": "body", "皮肤": "body",
}


def dev_cfg(cfg=None):
    """开发度的配置：满值、单轮上限、五档阈值、敏感系数曲线。"""
    cfg = cfg or load_config()
    raw = cfg.get("dev")
    if not isinstance(raw, dict):
        raw = DEFAULT_CONFIG.get("dev") or {}

    def _i(k, dv):
        try:
            return int(raw.get(k, dv))
        except (TypeError, ValueError):
            return dv

    levels = raw.get("levels")
    if not isinstance(levels, list) or not levels:
        levels = (DEFAULT_CONFIG.get("dev") or {}).get("levels") or []
    return {"max": _i("max", DEV_MAX),
            "step": _i("step", 25),
            "gain": float(raw.get("gain") or 1.5),
            "levels": levels}


def dev_blank():
    return {k: 0 for k in DEV_KEYS}


def norm_dev(v):
    """把模型给的 `dev` 增量洗成五部位字典。中文键、英文键都认。"""
    out = dev_blank()
    if isinstance(v, (int, float)):
        # 只给一个数：当成「这一轮整体在推进」，平均分到五个部位。
        try:
            n = int(v)
        except (TypeError, ValueError):
            n = 0
        if n:
            for k in DEV_KEYS:
                out[k] = n
        return out
    if not isinstance(v, dict):
        return out
    for key, val in v.items():
        k = _DEV_ALIAS.get(str(key or "").strip().lower())
        if not k:
            continue
        try:
            out[k] = int(val or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def dev_levels(cfg=None):
    """开发度五档，按 min 从高到低。"""
    return sorted(dev_cfg(cfg)["levels"],
                  key=lambda x: int(x.get("min") or 0), reverse=True)


def dev_level(v, cfg=None):
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    v = max(0, v)
    for l in dev_levels(cfg):
        if v >= int(l.get("min") or 0):
            return l
    return {}


def dev_name(v, cfg=None):
    """开发度 → 词。0 时是「没碰过」，不是空串——这根轴一直在。"""
    try:
        cur = int(v or 0)
    except (TypeError, ValueError):
        cur = 0
    if cur <= 0:
        return DEV_LABELS.get("virgin", "没碰过")
    levels = sorted(dev_levels(cfg), key=lambda x: int(x.get("min") or 0))
    # 语料里 500 是最高一档的最低线；刚好 500 时也要算「开发满了」。
    for lo, name in [(int(l.get("min") or 0), str(l.get("name") or ""))
                     for l in levels]:
        if cur == lo:
            return name
    return str(dev_level(cur, cfg).get("name") or "")


def dev_stage(v, cfg=None):
    """跨过了几道门槛（0~5）。0 分 = 0，100 = 1，500 = 5。

    注意和 `dev_level()` 的差别：`dev_level()` 返回**档位本身**（含 0 分
    那档「没碰过」），这里返回**跨过几道**。接入方要「第几档」用前者，
    要「跨过几次」用后者。两者正好差一，别混。
    """
    return sum(1 for l in dev_levels(cfg)
               if int(l.get("min") or 0) > 0 and int(v or 0) >= int(l.get("min") or 0))


def dev_total(dev):
    """五个部位加起来。全满 = 5 × 500 = 2500，面板显示「开发 100%」用。"""
    dev = dev if isinstance(dev, dict) else {}
    return sum(max(0, int(dev.get(k) or 0)) for k in DEV_KEYS)


def sensitivity(dev, spot=None, cfg=None):
    """敏感系数：1.0（没开发过）→ 1 + gain（满格 2.5）。

    `spot` 给了就只看那个部位，没给就取五部位里最高的——她身上只要有一处
    被开发透了，整体反应就不会慢。
    """
    dc = dev_cfg(cfg)
    dev = dev if isinstance(dev, dict) else {}
    if spot and spot in DEV_KEYS:
        v = int(dev.get(spot) or 0)
    else:
        v = max([int(dev.get(k) or 0) for k in DEV_KEYS] or [0])
    v = max(0, min(dc["max"], v))
    return 1.0 + (v / float(dc["max"])) * dc["gain"]


def after_value(dev, cfg=None):
    """高潮之后的回落值。开发度越高，她越缓不下来。

    固定 55 在高开发度下会自相矛盾：明明「离不开」，做完却能安稳缓上半天。
    500 满格时回到 75——还是能接着要，只是没到临界。
    """
    h = orgasm_hints(cfg)
    dc = dev_cfg(cfg)
    top = max([int((dev or {}).get(k) or 0) for k in DEV_KEYS] or [0])
    add = int(round(min(top, dc["max"]) / float(dc["max"]) * 20))
    return min(h["max"] - 10, h["after"] + add)


def dev_all_full(dev, cfg=None):
    """五个部位是不是全部满格。满格 = 身体开发 100%。"""
    dc = dev_cfg(cfg)
    dev = dev if isinstance(dev, dict) else {}
    return all(int(dev.get(k) or 0) >= dc["max"] for k in DEV_KEYS)


def dev_text(dev, cfg=None):
    """五处开发度拼成一行给**编剧 AI** 看的文本（带档名，不带 /500）。

    编剧要的是「她身体现在是什么状态」，不是进度条。所以这里给的是
    「嘴巴 120（起反应）／小穴 310（离不开）」，它据此判勾子。
    """
    dev = dev if isinstance(dev, dict) else {}
    parts = []
    for k in DEV_KEYS:
        v = max(0, int(dev.get(k) or 0))
        if v <= 0:
            continue
        parts.append("%s %d（%s）" % (DEV_LABELS.get(k, k), v, dev_name(v, cfg)))
    if not parts:
        return "（五处都还没被开发过）"
    return "／".join(parts)


def dev_top(dev, cfg=None):
    """五项里最高的那一处：`(key, 值, 档名)`。开发度轴和勾子都靠它判。"""
    dev = dev if isinstance(dev, dict) else {}
    k = max(DEV_KEYS, key=lambda x: int(dev.get(x) or 0))
    v = max(0, int(dev.get(k) or 0))
    return k, v, dev_name(v, cfg)


def dev_hook_ready(dev, cfg=None):
    """这一轮编剧**可以**给一个「身体先失控」的勾子：最高一处到 300 以上。

    300 是「离不开」那一档——过了这条线，她才会在跟性无关的场合自己发作。
    低于 300 硬给，就成了「明明还没被开发多少，怎么就饥渴到在公交车上
    拉你的手」——数值和剧情对不上。
    """
    _k, v, _n = dev_top(dev, cfg)
    return v >= 300


# 戒备的模糊档（2026-11-05）。**刻意不给配置文件**：这四档是给玩家和模型看的
# 态度描述，不是数值曲线——没有「调参」需求，硬编码反而防止配置写飞。
# 玩家侧只露档名（犯嘀咕/起疑/戒备），露数字会变成「刷警戒-消警戒」的循环。
SUSPICION_BANDS = ((30, "戒备"), (20, "起疑"), (10, "犯嘀咕"), (0, "坦然"))


def suspicion_name(v, cfg=None):
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    v = max(0, min(SUSPICION_MAX, v))
    for lo, name in SUSPICION_BANDS:
        if v >= lo:
            return name
    return "坦然"


def corrupt_hints(cfg=None):
    """堕落/屈服的记账参数。配置缺失时回退默认表，机制不能因缺配置停摆。"""
    cfg = cfg or load_config()
    raw = cfg.get("corrupt_hints")
    if not isinstance(raw, dict):
        raw = DEFAULT_CONFIG.get("corrupt_hints") or {}

    def _num(d, k, dv):
        try:
            return int((d or {}).get(k, dv))
        except (TypeError, ValueError):
            return dv

    up = raw.get("up") if isinstance(raw.get("up"), dict) else {}
    sub = raw.get("submit_up") if isinstance(raw.get("submit_up"), dict) else {}
    return {"up": {str(k): _num(up, k, 0) for k in up},
            "submit_up": {str(k): _num(sub, k, 0) for k in sub},
            "soothe": _num(raw, "soothe", -10)}


def card_start(card):

    """角色卡给的起点：{value, level}。没配就从 0 / 第 0 级开始。"""
    card = card if isinstance(card, dict) else {}
    try:
        lv = int(card.get("start_level") or 0)
    except (TypeError, ValueError):
        lv = 0
    lv = max(0, min(LEVEL_MAX, lv))
    gate = int(level_cfg(lv).get("gate") or 0)
    try:
        val = int(card.get("start_value"))
    except (TypeError, ValueError):
        val = gate          # 没给分数：正好卡在这一级的门槛上
    return {"value": max(MIN_V, min(MAX_V, val)), "level": lv}


def _today():
    return datetime.date.today().isoformat()


def _blank(line_key="", value=None):
    """新建一份攻略记录。

    起点**不是**固定的 0：先看角色卡有没有配 `start_level` / `start_value`。
    骨科卡可以一上来就是「妹妹」（第 2 级），不用从陌生人爬。
    """
    card = {}
    if line_key:
        try:
            from app import persona
            card = persona.active_card(line_key) or {}
        except Exception:                                  # noqa: BLE001
            card = {}
    st = card_start(card)
    if value is not None:
        st["value"] = max(MIN_V, min(MAX_V, int(value)))
    t = tier_for(st["value"]) or {}
    return {"mode": MODE_RAID, "value": st["value"], "tier": str(t.get("key") or ""),
            "level": st["level"], "arousal": 0, "cross_n": 0,
            "down": 0, "ending": "", "milestone": "",
            "excite": 0, "scene": False, "orgasms": 0, "idle": 0,
            "corrupt": 0, "submit": 0,
            # 开发度（2026-11-05）：五个部位单独记，**只涨不掉、不设上限**，
            # 500 是满格（面板显示 /500）。老档在 `_migrate` 里补 0。
            "dev": dev_blank(),
            "day": _today(), "chat": 0, "acts": 0,
            "streak": 1, "turns": 0, "ach": [], "hist": [],
            "mercy": MERCY_MAX, "suspicion": 0}


def _entry(line_key):
    """读分线条目（懒导入 persona，避免循环依赖）。

    2026-11-04：老存档（没 level 字段）在这里**迁移一次并落盘**。
    以前只在 `record_of` 里就地补内存，每次读都重算——用户改一次
    `affection_tiers.json` 的 gate，同一条线的层级就跟着漂。
    """
    from app import persona
    ent = persona._entry(line_key)                        # noqa: SLF001
    if not isinstance(ent, dict):
        return None
    rec = ent.get("affection")
    if isinstance(rec, dict) and "level" not in rec:
        _migrate(rec)
        try:
            _save_entry(line_key, ent)
        except Exception:                                 # noqa: BLE001
            # 落盘失败不能挡住这一轮——内存里已经迁好了，下次读再试
            log.warning("老存档迁移落盘失败 %s", line_key)
    return ent


def _save_entry(line_key, ent):
    from app import persona
    state = persona._load_state()                         # noqa: SLF001
    state[line_key] = ent
    persona._save_state(state)                            # noqa: SLF001


def _card_of(line_key):
    """这条线现在演的角色卡（取不到就空字典）。"""
    try:
        from app import persona
        card = persona.active_card(line_key)
        return card if isinstance(card, dict) else {}
    except Exception:                                      # noqa: BLE001
        return {}


def _migrate(rec):
    """老存档（只有 value/tier，没有 level/arousal/ach）就地补齐。

    2026-11-04 加距离层级之前的记录没有这两个字段，直接按 0 算会让老玩家
    一夜之间退回「隔得很远」。这里按现有好感反推一个相符的层级。

    ⚠️ 反推的是「你现在的好感够得上哪一级」，不是「你当年走过哪几级」——
    旧的单轴系统里没有距离这个概念，这个信息本来就不存在，猜不出来。
    """
    if not isinstance(rec, dict):
        return rec
    v = int(rec.get("value") or 0)
    # 按门槛反推「好感够得上第几级」
    by_gate = 0
    for i, l in enumerate(levels_of()):
        if v >= int(l.get("gate") or 0):
            by_gate = i
    if "level" not in rec:
        lv = by_gate
        # 老玩家补偿（2026-11-04 用户拍板）：旧的单轴里根本没有「距离」这个
        # 概念，按好感反推出来的层级天然偏保守——好感 20 的老档在旧系统里
        # 身体早就到了（档位叫「开始凿」），新标尺一上却退回「还隔着一层」，
        # 等于把走过的路抹了。凡是反推出至少走过一级的，一律再补一级（到顶为止）。
        # 宁可让老玩家快一步，也不让他们觉得「我的进度没了」。
        if lv >= 1:
            lv = min(LEVEL_MAX, lv + 1)
            # 但**不许白送到顶格**：「不分彼此」是终点站，靠补偿走到那里
            # 就没有下一步可走了，往后聊什么都只是原地。要顶格得自己够到
            # 最高一级的门票。
            if lv >= LEVEL_MAX and v < int(level_cfg(LEVEL_MAX).get("gate") or 0):
                lv = LEVEL_MAX - 1
        rec["level"] = lv
    else:
        # 2026-11-04 深夜：**换标尺之后才有的分支**。原来这里什么都不做，
        # 于是「已经有 level 字段」的老档被原样保留——用户那条伊蕾娜线就是
        # value=50、level=1（旧 5 级标尺下的 1 级），换到 7 级标尺后
        # 50 分本该是第 5 级「做过了」，却还挂在「能碰到了」。
        #
        # 层级**只升不降**：已经记下的层级是「走过的路」，不能因为标尺重排
        # 就收回去。所以只在「反推值比记录高」时往上抬，从不往下按。
        try:
            cur_lv = int(rec.get("level") or 0)
        except (TypeError, ValueError):
            cur_lv = 0
        if by_gate > cur_lv:
            rec["level"] = min(LEVEL_MAX, by_gate)
    if "arousal" not in rec:
        rec["arousal"] = 0
    if "cross_n" not in rec:
        rec["cross_n"] = 0
    # 2026-11-04 加成就与连续天数：老存档没有这两个，按「从今天开始算」补
    if "ach" not in rec or not isinstance(rec.get("ach"), list):
        # 补发：已经走过的层级不能因为「升级系统才做」就没记录。
        # 距离层级只升不降，反推出第 N 级 = 前 N 级都走过，可以确定地补。
        # 档位**不补**——档位会涨会掉，没法确认当年是否真的首次进过。
        rec["ach"] = [{"k": "lv%d" % i, "ts": ""}
                      for i in range(1, int(rec.get("level") or 0) + 1)]
        # 已经定格在结局的线不会再走结算，这两个只能在这里补
        ended = str(rec.get("ending") or "")
        if ended == "jail":
            rec["ach"].append({"k": "jail", "ts": ""})
        # 老档的 graduate 勋章：成就表里那条已经改成「在一起」，这里换成新 key
        if ended == "graduated" or int(rec.get("value") or 0) >= 50:
            rec["ach"].append({"k": "together", "ts": ""})
    # 已经有 ach 数组的老档同样要换：不然成就页会「7/12」却只列出 6 个
    # （graduate 已经不在 _keys 里，数得出来、画不出来）。
    for a in (rec.get("ach") or []):
        if isinstance(a, dict) and str(a.get("k") or "") == "graduate":
            a["k"] = "together"
    if "streak" not in rec:
        rec["streak"] = 1
    if "turns" not in rec:
        rec["turns"] = 0
    # 「再给我一次机会」的次数（2026-11-04 加）：老档没有这个字段，
    # 按满额算——不能因为系统后加，就把已经进过局子的人判成「机会用完了」。
    if "mercy" not in rec:
        rec["mercy"] = MERCY_MAX
    # 2026-11-04 晚 毕业结局退休：原来被锁在 graduated 的线（用户那条伊蕾娜
    # 就是 value=50 锁死、连消息都推不动）在这里**就地解锁**，把 50 转成里程碑，
    # 数值原样保留——不降级、不收走已经攒到的分。
    if str(rec.get("ending") or "") == "graduated":
        rec["ending"] = ""
        if not rec.get("milestone"):
            rec["milestone"] = MILESTONE_TOGETHER
    if "milestone" not in rec:
        rec["milestone"] = ""
    # 兴奋度（2026-11-04 晚）：只在亲密场景里累积，场景外归零
    if "excite" not in rec:
        rec["excite"] = 0
    if "scene" not in rec:
        rec["scene"] = False
    # 戒备（2026-11-05）：老档没有这个字段，从 0 起——她还没起疑。
    if "suspicion" not in rec:
        rec["suspicion"] = 0
    # 高潮次数（2026-11-04 深夜）：单场累计；只有真收场（连续空闲）才清零
    if "orgasms" not in rec:
        rec["orgasms"] = 0
    # 空闲轮计数（2026-11-05）：连续几轮没有身体接触。1 轮只是「这一轮在说别的」，
    # 2 轮才算这一场结束。
    if "idle" not in rec:
        rec["idle"] = 0
    # 冷却期（2026-11-04 深夜 → 2026-11-05 用户拍板**取消**，
    # 当天下午再取消次数上限）。
    # 老存档里还留着 `cool_left` 的线一次性收场：清掉冷却字段、场景结束。
    #
    # 2026-11-05 下午：这里原来还有一条「orgasms >= max_orgasm 就收场」——
    # 第 4 条规则取消封顶之后它就变成了**隐藏的剩余限制**：`settle` 不再封顶，
    # 但每次 `_ensure` 读到存档都按老规矩把次数清零。表现是玩家过了第 5 次，
    # 下一轮面板又变回「第 0 次」。现在只认冷却残留，不认次数。
    _cool_old = rec.pop("cool_left", 0) or 0
    try:
        had_cooldown = int(_cool_old) > 0
    except (TypeError, ValueError):
        had_cooldown = False
    if had_cooldown:
        rec["scene"] = False
        rec["excite"] = 0
        rec["orgasms"] = 0
        rec["idle"] = 0
    # 旧的 `scene_capped`（第一版补丁用过一晚上）：更早的标记，一并清掉。
    rec.pop("scene_capped", None)
    # 堕落 / 屈服（2026-11-04 深夜）：老档按 0 起——「她还是她」。
    # 不按羁绊反推：这两根轴记录的是「被怎么对待过」，旧系统里根本没这个
    # 概念，猜不出来。给 0 是最保守也最诚实的答案。
    if "corrupt" not in rec:
        rec["corrupt"] = 0
    if "submit" not in rec:
        rec["submit"] = 0
    # 开发度（2026-11-05）：老档从 0 起。已开发过的量没有痕迹可推，
    # 按 0 是最诚实的；缺某个部位（手工改过存档）也只补那一个。
    dev = rec.get("dev") if isinstance(rec.get("dev"), dict) else {}
    for _k in DEV_KEYS:
        if _k not in dev:
            dev[_k] = 0
    rec["dev"] = dev
    return rec


def record_of(ent):
    """条目里的好感记录；没有（自由模式从没开过）返回 None。"""
    if not isinstance(ent, dict):
        return None
    rec = ent.get("affection")
    return _migrate(rec) if isinstance(rec, dict) else None


def _ensure(line_key):
    """这条线的好感记录；没有就按角色卡起点建一份。

    2026-11-04 用户要求「一进来就是攻略模式」：以前只有手动切过一次模式的线
    才有 `affection` 记录，新线全走自由模式——网页右栏整块不显示、QQ 尾行也
    没有好感，玩家不知道自己在玩什么。现在只要有角色线就懒建一份（`_blank`
    的默认 mode 本来就是 raid，起点取角色卡的 `start_level`）。

    返回 `(ent, rec)`；没在演谁时都是 None。
    """
    ent = _entry(line_key)
    if not isinstance(ent, dict) or not str(ent.get("card") or "").strip():
        return None, None
    # 先快照再迁移：`_migrate` 是就地改，等它跑完就看不出「这原本是老档」了。
    # 老档（缺 excite/milestone，或还锁在 graduated）迁移完顺手落一次盘，
    # 否则磁盘上永远是旧结构，每次读都要重算一遍。
    raw = ent.get("affection")
    stale = (isinstance(raw, dict)
             and ("excite" not in raw or "milestone" not in raw
                  or str(raw.get("ending") or "") == "graduated"
                  or any(isinstance(a, dict) and str(a.get("k") or "") == "graduate"
                         for a in (raw.get("ach") or []))))
    rec = record_of(ent)
    if rec is not None:
        if stale:
            try:
                _save_entry(line_key, ent)
            except Exception:                             # noqa: BLE001
                log.warning("迁移后落盘失败 %s", line_key)
        return ent, rec
    rec = _blank(line_key)
    ent["affection"] = rec
    try:
        _save_entry(line_key, ent)
    except Exception:                                     # noqa: BLE001
        # 落盘失败不能挡住这一轮：内存里的 rec 已经能用，下次读再试。
        log.warning("新建好感记录落盘失败 %s", line_key)
    return ent, rec


def is_raid(line_key):
    """这条线现在是否在攻略模式（默认就是；切过自由模式才算不是）。"""
    _ent, rec = _ensure(line_key)
    return bool(rec) and rec.get("mode") == MODE_RAID


def mercy_left(rec):
    """这条线还剩几次「再给我一次机会」。缺字段的老档按满额算。"""
    try:
        n = int(rec.get("mercy", MERCY_MAX))
    except (TypeError, ValueError):
        n = MERCY_MAX
    return max(0, min(MERCY_MAX, n))


def status(line_key):
    """给网页/管理页看的只读快照。没在演谁返回 None。

    2026-11-04 加冲动与越界次数：网页右栏要画三轴，缺一个就得再请求一次。
    `levelMax` 一起给，前端不用把「5」写死在 JS 里。
    2026-11-04 晚：默认攻略模式——没记录的线在这里顺手建一份，所以右栏一进来
    就有东西可画，不用等玩家先发一条消息。
    """
    _ent, rec = _ensure(line_key)
    if not rec:
        return None
    cfg = load_config()
    value = int(rec.get("value") or 0)
    t = tier_for(value, cfg) or {}
    lv = int(rec.get("level") or 0)
    ar = int(rec.get("arousal") or 0)
    card = _card_of(line_key)
    return {
        "mode": str(rec.get("mode") or MODE_FREE),
        "value": value,
        "tier": str(rec.get("tier") or ""),
        "tierName": _tier_view(t, lv).get("name") or "",
        "level": lv,
        "levelMax": LEVEL_MAX,
        "levelName": level_name(lv, cfg, card),
        "intimacy": intimacy_gauge(lv, value, card, cfg),
        "arousal": ar,
        "arousalName": arousal_name(ar, cfg),
        "arousalShort": str(arousal_level(ar, cfg).get("short") or ""),
        # 兴奋度/高潮值（2026-11-04 晚/深夜）：只在场景内大于 0，网页按这个决定要不要画第三条
        "excite": int(rec.get("excite") or 0),
        "exciteName": excite_name(rec.get("excite"), cfg),
        "orgasms": int(rec.get("orgasms") or 0),
        "orgasmMax": orgasm_hints(cfg)["max_orgasm"],
        "inScene": bool(rec.get("scene")),
        "milestone": str(rec.get("milestone") or ""),
        "crossN": int(rec.get("cross_n") or 0),
        "ending": str(rec.get("ending") or ""),
        # 堕落 / 屈服（2026-11-04 深夜）：和冲动/高潮不一样，这两根是**常驻**的，
        # 不进场景也在，所以一直给，不靠 inScene 开关。
        "corrupt": int(rec.get("corrupt") or 0),
        "corruptName": corrupt_name(rec.get("corrupt"), cfg),
        "submit": int(rec.get("submit") or 0),
        "submitName": submit_name(rec.get("submit"), cfg),
        "mercy": mercy_left(rec),
        "mercyMax": MERCY_MAX,
    }


# ── 状态面板（2026-11-04 深夜，用户要的）────────────────
# 玩家真正想看的东西：**每一行一个数值，左边现在多少、右边满是多少，
# 中间挂着这一行对应的是什么状态**。够格的行亮、不够的灰掉——
# 人看到「差 3 个」就会想去补齐，这是这类玩法最原始的驱动力。
#
# 为什么不做成「成就」：成就只回答「到没到过」，回答不了「现在卡在哪、
# 还差多少」。玩家要的是把条顶满，不是收集徽章。

def _panel_rows(levels, cur, key_field="name", extra=None):
    """把一个档位表画成面板行：每一行 = 这一级 + 它够没够。

    `levels` 要按从低到高排（面板从上往下读是「往上爬」）。
    每一行给：
        name   这一级叫什么
        gate   这一级的门槛（左边画「当前值/门槛」时用）
        state  reached（到了）/ current（就在这一级）/ locked（还没到）
    """
    rows = []
    for i, l in enumerate(levels or []):
        if not isinstance(l, dict):
            continue
        gate = int(l.get("gate") if l.get("gate") is not None
                   else l.get("min") or 0)
        st = "locked"
        if i == cur:
            st = "current"
        elif i < cur:
            st = "reached"
        r = {"key": str(l.get("key") or ""),
             "name": str(l.get(key_field) or l.get("name") or ""),
             "gate": gate, "state": st}
        if extra:
            r.update({k: l.get(k) for k in extra})
        rows.append(r)
    return rows


def panel(line_key):
    """给网页右栏和 QQ「状态」指令共用的**整张面板**。

    一次算全，两边不各写一套：网页画竖列，QQ 拼成聊天记录卡片。
    返回 None = 没在演谁（这条线还没选角色）。

    三段：
        intimacy  身体进度七级（还差多少、下一级叫什么）
        arousal   冲动五档（她此刻想不想做）
        orgasm    高潮五档 + 单场次数（只在进行中显示）
    另有 `tiers`：羁绊的档位表，用来回答「感情到哪一档了」。
    """
    _ent, rec = _ensure(line_key)
    if not rec:
        return None
    cfg = load_config()
    card = _card_of(line_key)
    value = int(rec.get("value") or 0)
    lv = max(0, min(LEVEL_MAX, int(rec.get("level") or 0)))
    ar = int(rec.get("arousal") or 0)
    ex = int(rec.get("excite") or 0)
    oh = orgasm_hints(cfg)

    # 身体进度：七级，从「还没碰过」往上排
    levels = levels_of(cfg)
    int_rows = []
    for i, l in enumerate(levels):
        gate = int(l.get("gate") or 0)
        agate = l.get("arousal_gate")
        int_rows.append({
            "key": str(l.get("key") or ""), "name": level_name(i, cfg, card),
            "gate": gate,
            "arousalGate": None if agate is None else int(agate),
            "state": "current" if i == lv else ("reached" if i < lv else "locked"),
        })

    # 羁绊档位：正向的几档（负向是事故，不进面板）
    tier_rows = []
    for t in sorted([x for x in (cfg.get("tiers") or [])
                     if isinstance(x, dict) and int(x.get("min") or 0) >= 0],
                    key=lambda x: int(x.get("min") or 0)):
        mn = int(t.get("min") or 0)
        tier_rows.append({
            "key": str(t.get("key") or ""),
            "name": _tier_view(t, lv).get("name") or str(t.get("name") or ""),
            "gate": mn,
            "state": "current" if mn == (tier_for(value, cfg) or {}).get("min")
                     else ("reached" if value >= mn else "locked"),
        })

    # 冲动：五档，从低到高。`_index_at` 要拿**展示顺序**的列表去查，
    # 不能用配置里的原始顺序（那是高→低，下标正好反过来）。
    ar_sorted = sorted(arousal_levels(cfg), key=lambda x: int(x.get("min") or 0))
    ar_rows = _panel_rows(ar_sorted, _index_at(ar_sorted, ar), "name",
                          extra=("short",))
    # 高潮：五档（只在进行中有意义）
    ex_sorted = sorted(orgasm_levels(cfg), key=lambda x: int(x.get("min") or 0))
    ex_rows = _panel_rows(ex_sorted, _index_at(ex_sorted, ex), "name",
                          extra=("prompt",))

    # 堕落 / 屈服（2026-11-04 深夜）：常驻两段，场景外也在。
    co = int(rec.get("corrupt") or 0)
    su = int(rec.get("submit") or 0)
    co_sorted = sorted(corrupt_levels(cfg), key=lambda x: int(x.get("min") or 0))
    co_rows = _panel_rows(co_sorted, _index_at(co_sorted, co), "name",
                          extra=("prompt",))
    su_sorted = sorted(submit_levels(cfg), key=lambda x: int(x.get("min") or 0))
    su_rows = _panel_rows(su_sorted, _index_at(su_sorted, su), "name",
                          extra=("prompt",))

    dv, da = _need_to_cross(value, lv, ar, cfg)

    # 开发度（2026-11-05）：五个部位各一行，**带 /500 的上限**给玩家看。
    # 用户原话：「你可以把那个上限 500 给它展示出来」。所以这一块不是
    # 档位表，是五条独立的进度条：名字 + 当前值 + 档名。
    dev = rec.get("dev") if isinstance(rec.get("dev"), dict) else {}
    dmax = dev_cfg(cfg)["max"]
    dev_rows = []
    for k in DEV_KEYS:
        v = max(0, int(dev.get(k) or 0))
        dev_rows.append({
            "key": k,
            "name": DEV_LABELS.get(k, k),
            "value": v,
            "max": dmax,
            "stage": dev_stage(v, cfg),
            "stageName": dev_name(v, cfg),
            "pct": int(round(v / float(dmax) * 100)),
        })
    dev_tot = dev_total(dev)
    dev_full = dev_all_full(dev, cfg)

    return {
        "value": value,
        "max": bounds(cfg)[0],
        "min": bounds(cfg)[1],
        "level": lv,
        "levelMax": LEVEL_MAX,
        "levelName": level_name(lv, cfg, card),
        "arousal": ar,
        "arousalName": arousal_name(ar, cfg),
        "excite": ex,
        "exciteName": excite_name(ex, cfg),
        "orgasms": int(rec.get("orgasms") or 0),
        "orgasmMax": oh["max_orgasm"],
        "inScene": bool(rec.get("scene")),
        "tiers": tier_rows,
        "intimacy": int_rows,
        "arousals": ar_rows,
        "orgasms_rows": ex_rows,
        "corrupt": co,
        "corruptName": corrupt_name(co, cfg),
        "corrupts": co_rows,
        "submit": su,
        "submitName": submit_name(su, cfg),
        "submits": su_rows,
        # 开发度（2026-11-05）：五条独立进度，上限 500 外露。
        "dev": dev_rows,
        "devMax": dmax,
        "devTotal": dev_tot,
        "devTotalMax": dmax * len(DEV_KEYS),
        "devPct": int(round(dev_tot / float(dmax * len(DEV_KEYS)) * 100)),
        "devAllFull": dev_full,
        "sensitivity": round(sensitivity(dev, cfg=cfg), 2),
        # 戒备（2026-11-05）：只露档名不露数字——露数字会变成
        # 「刷警戒-消警戒」的循环攻略，玩家该看到的是她的态度。
        "suspicion": int(rec.get("suspicion") or 0),
        "suspicionName": suspicion_name(rec.get("suspicion"), cfg),
        "canCross": _can_cross(value, lv, ar, cfg),
        "needValue": dv,
        "needArousal": da,
        # 压迫模式（2026-11-05）：玩家要看得见「再推几次就降门槛了」，
        # 否则递减对他来说是不存在的机制。`gap` 为 0 表示现在就能进。
        "forced": forced_progress(co, ar, cfg),
        "crossN": int(rec.get("cross_n") or 0),
        "ending": str(rec.get("ending") or ""),
        "mode": str(rec.get("mode") or MODE_FREE),
    }


def _index_at(levels, v):
    """值落在第几档，返回**在给定（已按低→高排好的）列表里的下标**。

    规则是「取满足 min ≤ v 的**最后**那一个」——不是第一个。
    踩过两次：
      1. 配置里的 `arousal_levels` 是高→低序，直接查出来下标整体翻转；
      2. 改成低→高之后仍写 `第一个满足`，那列表第一个是 0，永远命中，
         于是「冲动 52」被标成 current 在「没什么」。
    升序列表要的是**从后往前找第一个满足的**，也就是最大的那个门槛。
    """
    lst = list(levels or [])
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    cur = 0
    for i, l in enumerate(lst):
        if v >= int(l.get("min") or 0):
            cur = i
    return cur


def panel_text(line_key):
    """「状态」指令的纯文本面板（QQ 聊天记录卡片 / 协议端发不了卡片时用）。

    排版照用户的原话：**左边当前值，斜杠，右边满值，后面挂状态词**。
    对齐用全角空格补，QQ 卡片的等宽效果比空格稳。
    """
    p = panel(line_key)
    if p is None:
        return None
    cfg = load_config()
    rows = ["📊 你们现在到哪了", ""]
    # 负分时不画「当前值/门槛」（2026-11-04 深夜修）：羁绊 -20 会写成
    # `-20/10`、而且每一档都这么写，读起来像「负 20 比 10 大」。
    # 负分区间根本不在正向档位的坐标系里，只报「欠多少」。
    neg = int(p["value"]) < 0

    # 一、身体进度：七行，到了的打勾，没到的挂门槛
    rows.append("— 身体进度 —")
    for r in p["intimacy"]:
        mark = {"reached": "✔", "current": "▶", "locked": "·"}[r["state"]]
        gate = r["gate"]
        # 第一级没有门槛（0），写「42/0」只会让人以为满了；
        # 直接从这一行开始报「离这一级还差多少」。
        if gate <= 0:
            line = "%s %s" % (mark, r["name"])
        elif neg:
            line = "%s %s" % (mark, r["name"])
        else:
            line = "%s %s  %s/%s" % (mark, r["name"], p["value"], gate)
        ag = r.get("arousalGate")
        if ag:
            line += "  冲动≥%d" % ag
        if r["state"] == "current":
            line += "   ←现在这里"
        rows.append(line)

    # 二、羁绊档位（感情那一轴）
    rows += ["", "— 感情 —"]
    for r in p["tiers"]:
        mark = {"reached": "✔", "current": "▶", "locked": "·"}[r["state"]]
        if r["gate"] <= 0 or neg:
            rows.append("%s %s" % (mark, r["name"]))
        else:
            rows.append("%s %s  %s/%s" % (mark, r["name"], p["value"], r["gate"]))

    # 三、冲动：她此刻想不想。这一档给的是「离下一个词还差多少」，
    # 不是「离 100 还差多少」——玩家要看的是下一格在哪。
    rows += ["", "— 冲动 %d/100 —" % p["arousal"]]
    for r in p["arousals"]:
        mark = {"reached": "✔", "current": "▶", "locked": "·"}[r["state"]]
        line = "%s %s" % (mark, r["name"])
        if r["gate"] <= 0:
            pass
        elif r["state"] == "current":
            line += "  %d/%d" % (p["arousal"], r["gate"])
        else:
            line += "  ≥%d" % r["gate"]
        rows.append(line)

    # 四、高潮：只在进行中有意义。
    # 2026-11-05 取消 5 次封顶，次数只报「第 N 次」，不再挂分母——
    # 挂着「/5」会让人以为到 5 就收场，而那个收场已经拿掉了。
    if p["inScene"]:
        rows += ["", "— 高潮 %d/100 · 第 %d 次 —"
                  % (p["excite"], p["orgasms"])]
        for r in p["orgasms_rows"]:
            mark = {"reached": "✔", "current": "▶", "locked": "·"}[r["state"]]
            line = "%s %s" % (mark, r["name"])
            if r["gate"] > 0:
                line += ("  %d/%d" % (p["excite"], r["gate"])
                         if r["state"] == "current" else "  ≥%d" % r["gate"])
            rows.append(line)

    # 五、堕落 / 屈服（2026-11-04 深夜）：常驻两段，场景外也在。
    # 堕落只涨不掉——它是「她变成了什么样」；屈服会回涨——它是「此刻听不听话」。
    rows.append("")
    rows.append("— 堕落 %d/100 —" % p["corrupt"])
    for r in p["corrupts"]:
        mark = {"reached": "✔", "current": "▶", "locked": "·"}[r["state"]]
        line = "%s %s" % (mark, r["name"])
        if r["gate"] > 0:
            line += ("  %d/%d" % (p["corrupt"], r["gate"])
                     if r["state"] == "current" else "  ≥%d" % r["gate"])
        rows.append(line)

    rows.append("")
    rows.append("— 屈服 %d/100 —" % p["submit"])
    for r in p["submits"]:
        mark = {"reached": "✔", "current": "▶", "locked": "·"}[r["state"]]
        line = "%s %s" % (mark, r["name"])
        if r["gate"] > 0:
            line += ("  %d/%d" % (p["submit"], r["gate"])
                     if r["state"] == "current" else "  ≥%d" % r["gate"])
        rows.append(line)

    # 六、开发度（2026-11-05）：五处各自一条，**上限 500 直接写出来**。
    # 和上面几段不同，这里不按档位画勾：玩家要的是「这一处到了多少 /500」，
    # 档名只挂在后面当注释。
    rows.append("")
    rows.append("— 开发度 · 共 %d%% —" % p["devPct"])
    for d in p.get("dev") or []:
        mark = "✔" if d["value"] >= d["max"] else ("▶" if d["value"] > 0 else "·")
        line = "%s %s  %d/%d  %s" % (mark, d["name"], d["value"], d["max"],
                                     d["stageName"])
        rows.append(line)
    if p.get("devAllFull"):
        rows.append("开发满了：碰一下就溃")

    # 七、结论：能不能往前一步
    rows.append("")
    if p["ending"]:
        rows.append("结局已定格。想翻过去发「自由模式」。")
    elif neg:
        # 负分区间没有「下一级」可言——她还没消气，先回到 0 再说。
        rows.append("她还在算账（羁绊 %d）。想往前推，先把这 %d 点的账还上。"
                    % (p["value"], -p["value"]))
    elif p["needValue"] or p["needArousal"]:
        gap = []
        if p["needValue"]:
            gap.append("感情还差 %d" % p["needValue"])
        if p["needArousal"]:
            gap.append("气氛还差 %d（冲动每轮回落，得自己拱）" % p["needArousal"])
        rows.append("再往前一步（%s）：%s"
                    % (level_name(p["level"] + 1, cfg, _card_of(line_key)),
                       "、".join(gap)))
    else:
        rows.append("再往前一步：两样都够了，就差那一件事。")
    return "\n".join(rows)


def panel_brief(line_key):
    """每轮自动跟在角色回复后面的「此刻」窄条（2026-11-05 用户要求）。

    和 `panel_text` 的分工：那边是**完整阶梯表**（手动发「状态」才看）；
    这边每轮都附着，所以只报**箭头 ▶ 指着的那一格**，不铺整张表——
    铺了会把卡片正文淹掉。取不到线（没在演）返回 ""。
    """
    p = panel(line_key)
    if p is None:
        return ""

    def cur(rows, value, fallback):
        """当前那一格的「档名 当前值/门槛」；门槛为 0 只报档名。"""
        for r in rows or []:
            if r.get("state") == "current":
                gate = int(r.get("gate") or 0)
                name = r.get("name") or fallback
                if gate > 0 and int(value) >= 0:
                    return "%s %d/%d" % (name, int(value), gate)
                return name
        return fallback

    rows = ["📊 此刻（发「状态」看完整面板）"]
    b1 = "身体 ▶ %s" % cur(p["intimacy"], p["value"], p["levelName"])
    heart = cur(p["tiers"], p["value"], "")
    if heart:
        b1 += " ｜ 感情 ▶ %s" % heart
    rows.append(b1)
    b2 = "冲动 ▶ %s" % cur(p["arousals"], p["arousal"], p["arousalName"])
    if p["inScene"]:
        b2 += " ｜ 高潮 ▶ %s（第 %d/%d 次）" % (
            cur(p["orgasms_rows"], p["excite"], p["exciteName"]),
            p["orgasms"], p["orgasmMax"])
    rows.append(b2)
    b3 = "堕落 ▶ %s ｜ 屈服 ▶ %s" % (
        cur(p["corrupts"], p["corrupt"], p["corruptName"]),
        cur(p["submits"], p["submit"], p["submitName"]))
    if p.get("suspicionName"):
        b3 += " ｜ 戒备 ▶ %s" % p["suspicionName"]
    rows.append(b3)
    if p["ending"]:
        rows.append("结局已定格。想翻过去发「自由模式」。")
    return "\n".join(rows)


# ── 模式切换 / 重开 ────────────────────────────────────

def _lift_ending(rec, cfg=None):
    """把结局这一页翻过去：清标记，好感往线内挪**一格**。

    为什么是挪一格而不是拉回起点：结局有两种触发方式——分数越线，或者
    越界次数到顶（`force_ending`）。后者可能停在 -30 这种远没到线的地方，
    把 -30 拉回起点等于抹掉玩家一晚上的账。「挪一格」两种情形都成立：
    进局子 -50 → -49、报警时 -30 → -29。
    挪完必须在区间内，不然切回攻略模式的第一轮结算会拿越线值再判一次结局。

    2026-11-04 晚：`graduated`（毕业）退休，现在只剩进局子这一种结局，
    所以「往上挪一格」那个分支没有实际用途了——但**老档里可能还留着**
    `ending="graduated"`（`_migrate` 会顺手解锁），这里照样兜住。

    2026-11-04 用户明确：这条和「再给我一次机会」是**同一个效果**，
    区别只在「再给我一次机会」是明面上的按钮、要扣次数。
    返回被解除的结局 key（本来没结局就是空串）。
    """
    if not isinstance(rec, dict):
        return ""
    ended = str(rec.get("ending") or "")
    if not ended:
        return ""
    hi, lo = bounds(cfg)
    v = int(rec.get("value") or 0)
    # 进局子往上挪到线内；老档的 graduated 往下挪一格（那是高线）
    v += -1 if ended == "graduated" else 1
    rec["value"] = max(lo + 1, min(hi - 1, v))
    rec["ending"] = ""
    return ended


def set_mode(line_key, raid):
    """开/关攻略模式。返回给用户看的确认话术。

    开：有封存记录就原值解冻，没有就归零新开。需要已在扮演态（有角色线）。
    关：mode 改 free，数值**封存不删**——玩崩了缓一缓还能接着来。
    ⚠️ 首次开启必须在判断「是不是已经开着」**之前**落一次盘——
    `_blank()` 的默认 mode 就是 raid，先落盘再判断，不然第一次永远命中
    「已经是攻略模式」分支，记录根本不落（实测踩过）。
    """
    ent, existing = _ensure(line_key)
    if ent is None:
        return None           # 没在演谁：当普通消息，不认
    if raid:
        rec = existing
        lv = int(rec.get("level") or 0)
        if rec.get("ending"):
            rec["mode"] = MODE_RAID
            ent["affection"] = rec
            _save_entry(line_key, ent)
            ename = str(ending_cfg(str(rec["ending"]), load_config())
                        .get("name") or "结局")
            return ("这条线还停在结局（%s），数值锁定，她留在那天的情绪里。"
                    "想从那页翻出来接着聊就发「自由模式」，想彻底删档发「清空存档」。"
                    % ename)
        if rec.get("mode") == MODE_RAID:
            return ("现在就是攻略模式，你们到【%s】，好感 %d。"
                    % (level_name(lv, card=_card_of(line_key)) or "认识",
                       int(rec.get("value") or 0)))
        was = int(rec.get("value") or 0)
        rec["mode"] = MODE_RAID
        ent["affection"] = rec
        _save_entry(line_key, ent)
        return ("攻略模式已恢复：你们到【%s】，好感 %d，接着上次来。"
                % (level_name(lv, card=_card_of(line_key)) or "认识", was))
    # → 自由模式：封存
    rec = existing
    # 结局不是死路（2026-11-04 用户提：报警结局之后想接着聊）。
    # 切自由模式 = 这一页翻过去：结局标记清掉、好感往线内挪一格，她回到平时的样子。
    # 排在 `mode == free` 判断**之前**：已经切成自由的旧线（free + ending=jail）
    # 结局还挂着，就是这个顺序卡住了玩家。
    if rec.get("ending"):
        ended = str(rec["ending"])
        ename = str(ending_cfg(ended, load_config()).get("name") or "结局")
        _lift_ending(rec)
        rec["mode"] = MODE_FREE
        ent["affection"] = rec
        _save_entry(line_key, ent)
        return ("【%s】这一页翻过去了，切回自由模式：她不再按结局那天的样子对你，"
                "数值封存着（好感 %d）。想接着攻略就发「攻略模式」，"
                "想彻底删档重来就发「清空存档」。"
                % (ename, int(rec.get("value") or 0)))
    if rec.get("mode") == MODE_FREE:
        return "现在就是自由模式（好感 %d 封存中）。发「攻略模式」接着玩。" % int(
            rec.get("value") or 0)
    rec["mode"] = MODE_FREE
    ent["affection"] = rec
    _save_entry(line_key, ent)
    return ("已切回自由模式（好感 %d 封存，她不再按档位演戏）。"
            "想接着攻略就发「攻略模式」，好感原样恢复。" % int(
                rec.get("value") or 0))


def wipe_numbers(line_key, keep_mode=True):
    """把好感记录整份换新，重置成**这张卡的起点**，供「清空存档」调用。

    2026-11-04 用户拍板：存档既然是真删，就没有「经历过的事」可言——
    好感、关系层级、结局、成就、连续天数、机会次数（mercy）全部回到初始，
    连 `wipe` 之前攒的勋章一起清掉。

    和改动前的 `restart()` 的区别：那个只删数值、留着成就、聊天记忆也不动。
    玩家删完发现她还记得上周聊的，惩罚感是假的。

    只负责好感这一层；聊天历史与扮演态由 `persona.wipe()` 删。
    返回新记录（`persona` 拿它写进状态），没开过攻略时返回 None。
    """
    ent, old = _ensure(line_key)
    if ent is None or old is None:
        return None           # 没在演谁：当普通消息
    fresh = _blank(line_key)
    if keep_mode and old.get("mode"):
        fresh["mode"] = old["mode"]
    ent["affection"] = fresh
    _save_entry(line_key, ent)
    return fresh


def mercy(line_key):
    """「再给我一次机会」：结局之后明面上的重来按钮，每人 MERCY_MAX 次。

    效果和「切自由模式再切回攻略」**完全一样**——都是 `_lift_ending`：
    结局解除、好感往线内挪一格（-50 → -49，报警时 -30 → -29）。
    区别只有两点：
      - 它留在攻略模式里，点完就能接着玩，不用来回切两次；
      - 它扣次数；那 3 次用完，就只剩自由模式那条不扣次数的路（不对外宣传）。

    用户 2026-11-04 定的：进局子那一刻得有个看得见的出口，不然玩家会以为
    「存档要没了」，索性不玩了。
    """
    ent, rec = _ensure(line_key)
    if ent is None or rec is None:
        return None           # 没在演谁：当普通消息
    if rec.get("mode") != MODE_RAID:
        return "现在是自由模式，没有被结局锁住。想接着攻略发「攻略模式」。"
    if not rec.get("ending"):
        return "现在还没到结局，用不上这个。"
    left = mercy_left(rec)
    if left <= 0:
        return ("三次机会都用完了，这次没有了。发「自由模式」能接着聊（数值封存），"
                "发「清空存档」是把这条线彻底删掉（会要你二次确认）。")
    _lift_ending(rec)
    rec["cross_n"] = 0        # 案子了结：再犯一次不会立刻又报警
    rec["arousal"] = 0
    rec["mercy"] = left - 1
    ent["affection"] = rec
    _save_entry(line_key, ent)
    return ("她留下了，接着攻略：好感 %d。这条机会还剩 %d 次。"
            % (int(rec.get("value") or 0), left - 1))


# ── 结算 ──────────────────────────────────────────────

def scoring_ctx(line_key):
    """给第一段打分提示词的「当前关系」上下文。

    返回 dict：value / tier（档位名）/ recent（最近几条变化，拼好的多行文本）。
    没在攻略时返回 None（调用方就不用打分版提示词）。
    """
    _ent, rec = _ensure(line_key)
    if not rec or rec.get("mode") != MODE_RAID:
        return None
    cfg = load_config()
    value = int(rec.get("value") or 0)
    # 最近发生（2026-11-05 扩到 8 条）：打分 prompt 的「重复衰减」要靠它数
    # 「同一类讨好他做过几次」，3 条不够看出模式。
    recent_rows = []
    hist = rec.get("hist") if isinstance(rec.get("hist"), list) else []
    for h in reversed(hist[-8:]):
        recent_rows.append("· %s（%+d）" % (h.get("r") or "", int(h.get("d") or 0)))
    recent = ("最近发生：\n" + "\n".join(recent_rows)) if recent_rows \
        else "（你们还没什么交集）"
    lv = int(rec.get("level") or 0)
    tv = _tier_view(tier_for(value, cfg), lv)
    return {"value": value,
            "tier": tv["name"] or "?",
            "level": level_name(lv, cfg, _card_of(line_key)) or "?",
            "levelN": lv,
            "arousal": int(rec.get("arousal") or 0),
            # 堕落 / 屈服（2026-11-04 深夜）：打分模型要知道她现在被弄成什么样，
            # 否则「同一句话」在堕落 0 和堕落 80 时会给出一样的 delta。
            "corrupt": int(rec.get("corrupt") or 0),
            "submit": int(rec.get("submit") or 0),
            # 戒备（2026-11-05）：模型要看到她现在起没起疑，才知道这轮放行
            # 还是往回打。
            "suspicion": int(rec.get("suspicion") or 0),
            # 开发度（2026-11-05）：编剧要看到五处各到哪一档，才判得出
            # 「这一轮该不该给她一个身体先动的勾子」。
            "dev": rec.get("dev") if isinstance(rec.get("dev"), dict) else dev_blank(),
            "devText": dev_text(rec.get("dev"), cfg),
            "devMax": dev_cfg(cfg)["max"],
            "recent": recent}


def sleep(line_key):
    """过夜：把这一夜的场面收掉，返回结算后的状态（没在演返回 None）。

    2026-11-05 修（用户：「睡到第二天」剧情断片 + 状态条跨夜不对）：
    `skip_day` 以前**只推游戏日，完全不碰好感记录**。于是：
      · 第二天早上面板还挂着昨晚的高潮值和「进行中」的场景，冲动也停在
        睡前那个数——玩家看到的是「睡了一觉什么都没变」；
      · 模型拿到的状态行也是昨晚的（衣着、位置、接触点全是床上的），
        它自然接着昨晚写，白天该有的「醒来、换衣、出门」全没有。
    这里按「过夜」处理：场景结束、高潮值归零、单场次数清零、冲动回落。
    **不清零冲动**——刚醒来还记得昨晚，只是凉下去了。
    好感 / 堕落 / 屈服 / 戒备一律不动：那些是关系事实，不该因为睡一觉消失。

    和 `settle` 一样留 `_prev` 快照，回退上一轮时能整体换回去。
    """
    ent, rec = _ensure(line_key)
    if not rec:
        return None
    _snap = copy.deepcopy(rec)
    _snap.pop("_prev", None)
    rec["_prev"] = _snap
    prev_aro = int(rec.get("arousal") or 0)
    rec["arousal"] = max(0, prev_aro - AROUSAL_DECAY * 2)
    rec["excite"] = 0
    rec["scene"] = False
    rec["orgasms"] = 0
    rec["idle"] = 0
    ent["affection"] = rec
    _save_entry(line_key, ent)
    return {"arousal": rec["arousal"],
            "arousalName": arousal_name(rec["arousal"]),
            "excite": 0, "in_scene": False,
            "value": int(rec.get("value") or 0)}


def _as_bool(v):
    """把模型可能给的几种写法统一成 bool。

    2026-11-05：实测打分模型会把 `false` 写成字符串 `"false"`，`bool("false")`
    是 True，场景判定直接翻车。这里只认「明确表示否」的那几种写法。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "是", "对", "on")


def settle(line_key, score):
    """把第一段的打分结算进状态。返回结果 dict（调用方拼注入和尾行）。

    `score`：第一段模型输出的 {"delta": int, "turn_type": "...", "reason": "..."}。
    没交作业 / 解析失败时传 None——按 +0 处理，**不让整轮回不了话**。
    """
    ent, rec = _ensure(line_key)
    if not rec or rec.get("mode") != MODE_RAID:
        # **mode 必须带上**：网页端拿这个回包直接渲染开关状态，
        # 缺了就会被判成「自由模式」——聊天一轮后控件自己跳到自由模式
        # （2026-11-03 群友实测：好感 +20 时突然变自由模式，其实数值没变）。
        return {"applied": 0, "active": False, "mode": MODE_FREE}
    value = int(rec.get("value") or 0)
    cfg = load_config()

    # 结局已触发：数值锁定，不再结算
    # （2026-11-04 深夜起 `ending_for` 恒返回 None，这条分支只服务老存档里
    #   已经定格在 jail / graduated 的线——那些线还得能正常显示。）
    if rec.get("ending"):
        return {"applied": 0, "active": True, "mode": MODE_RAID, "value": value,
                "tier": rec.get("tier") or "",
                "tierName": str((tier_for(value, cfg) or {}).get("name") or ""),
                "ending": str(rec.get("ending")), "just_ended": False,
                "mercy": mercy_left(rec), "mercyMax": MERCY_MAX,
                "corrupt": int(rec.get("corrupt") or 0),
                "corruptName": corrupt_name(rec.get("corrupt"), cfg),
                "corruptDelta": 0,
                "submit": int(rec.get("submit") or 0),
                "submitName": submit_name(rec.get("submit"), cfg),
                "submitDelta": 0,
                "reason": ""}

    # ── 回退快照（2026-11-04）────────────────────────
    # 「回退上一轮」的账本基础：动手改任何字段**之前**，把整份记录原样
    # 拷一份挂在 `_prev` 里。回退时（`rollback_last`）整体还原，不用逐字段
    # 逆运算——settle 动的字段太多（value/tier/level/arousal/excite/scene/
    # corrupt/submit/suspicion/cross_n/hist…），逐个倒推漏一个就是脏账。
    # 只保一级：新快照盖旧快照时把旧的 pop 掉，不然 deep copy 会把上上次
    # 的快照也带上，存档无限膨胀。
    _snap = copy.deepcopy(rec)
    _snap.pop("_prev", None)
    rec["_prev"] = _snap

    # 跨天：把隔夜会散掉的东西散掉。**不再有额度要重置**——聊天和剧情
    # 都不按天算次数（2026-11-04 晚），这里只管气氛和警惕。
    if str(rec.get("day") or "") != _today():
        prev_day = str(rec.get("day") or "")
        rec["day"] = _today()
        rec["cross_n"] = max(0, int(rec.get("cross_n") or 0) - 1)
        rec["arousal"] = 0
        # 连续陪伴（2026-11-04）：昨天刚来过就 +1，断过就从 1 重算。
        # 首聊当天不算跨天（day 就是今天），streak 在 `_blank` 里已经是 1。
        yday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        rec["streak"] = (int(rec.get("streak") or 0) + 1) if prev_day == yday else 1
    rec["turns"] = int(rec.get("turns") or 0) + 1

    score = score if isinstance(score, dict) else {}
    try:
        delta = int(score.get("delta") or 0)
    except (TypeError, ValueError):
        delta = 0
    delta = max(-ROUND_CAP, min(ROUND_CAP, delta))
    ttype = str(score.get("turn_type") or "chat").strip().lower()
    reason = str(score.get("reason") or "").strip()[:40]

    # 聊天和剧情同权（2026-11-04 晚）：不再按「当天第几次」打折，也不再限制
    # 剧情型每天几次大额。单轮只受 ROUND_CAP 约束——那是防一句话刷满，
    # 不是时间闸。`ttype` 仍然带下来，只用于记录和展示，不参与额度计算。
    applied = delta

    # ── 戒备折减（2026-11-05，用户拍板的「付出代价」）──────
    # 她正戒备着的时候，好话和动作的收益减半。prompt 层已经压过一轮
    # （模型看得到戒备值和规则），这里是代码保底——「模型给分太慷慨」
    # 正是这套系统被用户点名的问题，不能全押在模型自觉上。
    # 负分不打折：惩罚不因为她在戒备就缩水，越界该掉还是掉。
    sus_prev = int(rec.get("suspicion") or 0)
    if applied > 0 and sus_prev >= SUSPICION_DISCOUNT_AT:
        applied = applied // 2

    # 冲动预测挪到前面（原在下面的冲动段）：这一次算出来给下面几处共用，
    # 避免同一套公式写两遍。
    try:
        arose = int(score.get("arousal") or 0)
    except (TypeError, ValueError):
        arose = 0
    arose = max(-60, min(60, arose))
    _aro_prev = int(rec.get("arousal") or 0)
    _aro_pred = (_aro_prev + arose if arose > 0
                 else _aro_prev + arose - AROUSAL_DECAY)
    _aro_pred = max(0, min(AROUSAL_MAX, _aro_pred))

    prev_value = value
    value = max(MIN_V, min(MAX_V, value + applied))
    applied = value - prev_value      # 夹到边缘后以实际变动为准

    # ── 越界与冲动（2026-11-04）────────────────────────
    # 判定权在第一段模型：它说越界了就是越界了，说她不答应就是不答应。
    # 代码只按「越界了几次」施加代价——**不冻结、不降级**，扣完自己哄回来。
    cross = str(score.get("cross") or "").strip().lower()
    if cross not in ("mild", "hard"):
        cross = ""
    allow = str(score.get("allow") or "").strip().lower()

    # 冲动是一次性的机会：本轮场景给的增量 + 回合结束回落。
    #
    # 2026-11-05 修（用户：网页右栏「冲动值一直不变」）：原来**每轮无条件
    # -15**，而打分模型给的常态增量只有 +10~+15（情话 +10、独处 +15，
    # 身体接触 +20 但不是每轮都有）→ 净值长期为负，夹在 0 上就再也不动。
    # 玩家看到的确实是「一条死掉的进度条」。
    # 现在只有「这一轮没在拱气氛」（arose <= 0）才回落：有人在推就攒得住，
    # 没人管会凉——「得重新拱气氛」的本意保住了，但不再是把所有加分吃掉。
    # 计算已在上面完成（`_aro_pred`），这里只落账。
    rec["arousal"] = _aro_pred

    # ── 高潮值（2026-11-04 深夜改版，2026-11-05 用户再改两次）──────
    # 这一版的规则：
    #   1. **涨多少由场景 AI 判**（它给 `excite` 增量）；它没给才按动作档位兜底。
    #      上限还是 100——只是同一步的幅度听这一轮的戏。
    #   2. **中断不清零**。一轮没有身体接触 = 空闲轮，只自然往下走
    #      （AI 给负值，没给就减 `idle_decay`）；连续两轮空闲才算真收场。
    #      上一轮还 70、这一轮聊了两句话就变 0 —— 那是线上被反复投诉的那个 bug。
    #   3. **取消 5 次封顶**（2026-11-05 下午用户拍板）：`orgasms` 只记次数，
    #      到几次都不会强制收场。原来那句「第 6 次是水词」是设计者的偷懒，
    #      玩家在开发度高了之后本来就会连续到，硬砍反而不爽。
    #   4. **敏感系数**：开发度越高，同样的动作涨得越多（见 `sensitivity`）。
    #
    # 到 100 → 触发一次高潮：记进 `orgasms`、回落到余韵值（不是回 0）。
    prev_scene = bool(rec.get("scene"))
    oh = orgasm_hints(cfg)
    act_key, act_raise = act_base(score.get("act"), cfg)
    # 2026-11-05 修（用户：网页「高潮值消失了」）：模型给的两个字段经常打架
    # ——`act` 明明写了在做什么（foreplay/hand/…），`in_scene` 却填 false，
    # 或者把 false 写成字符串。按原逻辑这一轮会被判成「场景已结束」。
    # 以**动作**为准：它更具体，也更难随手填错。
    #
    # `act=none / stop` 不是「结束」而是「这一轮没有接触」：走空闲分支，
    # 不清零。所以这里只用它判「这一轮算不算有身体接触」。
    # 模型**漏填** act（字段缺失，不是 none）时按它给的 in_scene 判——
    # 一次字段缺失不该让记账断掉，这时按动作表兜底（老行为）。
    _raw_act = str(score.get("act") or "").strip().lower()
    _has_act = bool(_raw_act) and _raw_act not in ("none", "stop", "pause", "停", "中断")
    _flag = _as_bool(score.get("in_scene"))
    _contact = _has_act or (not _raw_act and _flag)
    in_scene = _flag or _has_act
    # 压迫模式（2026-11-05）：这一轮结算里要用两处——开发度涨得更快、
    # `res["forced"]` 给演员层和笔法层挑写法。**必须在 settle 里写进 res**，
    # `injection_block` 是纯函数不写回。
    forced_now = is_forced(value, int(rec.get("corrupt") or 0),
                           _aro_pred, int(rec.get("excite") or 0), cfg)
    # 模型给的增量：这一轮涨/落多少，由它按当下的戏判（-40 ~ +40）
    try:
        ex_mod = int(score.get("excite") or 0)
    except (TypeError, ValueError):
        ex_mod = 0
    ex_mod = max(-40, min(40, ex_mod))

    just_orgasm = 0
    total_orgasm = int(rec.get("orgasms") or 0)
    idle = int(rec.get("idle") or 0)
    # 开发度（2026-11-05）：先取旧值算敏感系数，再叠加这一轮的增量。
    # **先算后加**是有意的：这一轮给的开发度影响的是下一轮，不然她这一轮
    # 刚被开发、同一轮就变敏感，读起来像数值跳帧。
    dev_prev = norm_dev(rec.get("dev"))
    sens = sensitivity(dev_prev, cfg=cfg)
    after_now = after_value(dev_prev, cfg)
    dev_gain = 0
    dev_hit = ""
    if _contact:
        d = score.get("dev")
        raw_dev = norm_dev(d)
        if str(score.get("spot") or "").strip():
            dev_hit = _DEV_ALIAS.get(str(score.get("spot") or "").strip().lower(), "")
        dc = dev_cfg(cfg)
        # 压迫场里开发得更快（2026-11-05）：那些事本来就不是她同意的。
        mult = 1.5 if forced_now else 1.0
        for k in DEV_KEYS:
            try:
                dv = int(raw_dev.get(k) or 0)
            except (TypeError, ValueError):
                dv = 0
            dv = max(0, min(dc["step"], dv))
            if dv:
                dv = int(round(dv * mult))
            if dv:
                dev_prev[k] = int(dev_prev.get(k) or 0) + dv
                dev_gain += dv
        rec["dev"] = dev_prev

    # 勾子（2026-11-05）：编剧给的那一句「身体先失控」。**不消费数值**，
    # 只当这一轮的开场素材带回 res，`injection_block` 会把它交给演员。
    # 开发度还没到 300 却给了勾子：模型自己加了戏，这里截掉——低于这条线
    # 就发作会显得数值和剧情对不上。不用 `_contact` 判：场景外也会发作，
    # 那正是「离不开」这一档该有的样子。
    dev_hook = str(score.get("hook") or "").strip()[:120]
    if dev_hook and not dev_hook_ready(dev_prev, cfg):
        dev_hook = ""

    if _contact:
        # 有身体接触：在场景里，按增量累积
        idle = 0
        base = int(rec.get("excite") or 0) if prev_scene else oh["start"]
        # AI 给了就用它的；没给（0）才按动作档位记——表只做兜底
        step = ex_mod if ex_mod else act_raise
        # 敏感系数（2026-11-05）：同一个动作，开发度越高涨得越多。
        # 只有正增量乘系数——回落的时候不能也放大，不然开发度高反而更容易
        # 断崖式掉下去。
        if step > 0:
            step = int(round(step * sens))
        rec["excite"] = max(0, min(oh["max"], base + step))
        # 触发高潮：到顶就落一次，然后回到余韵值。
        # **2026-11-05 取消 5 次封顶**（用户拍板）：只记次数，不收场。
        # 收场只由「连续两轮没接触」决定——那条在下面的空闲分支里。
        if rec["excite"] >= oh["max"]:
            total_orgasm += 1
            rec["orgasms"] = total_orgasm
            just_orgasm = total_orgasm
            rec["excite"] = after_now
    elif prev_scene:
        # 空闲轮：**不清零**。这一轮只是在说别的、在缓、在穿衣服。
        # 数值按 AI 判的负增量往下走；它没给就减默认值。
        idle += 1
        if idle >= ORGASM_IDLE_END:
            # 连续两轮都没有接触：这一场真的结束了，次数归零
            in_scene = False
            rec["excite"] = 0
            rec["orgasms"] = 0
            total_orgasm = 0
            idle = 0
        else:
            decel = ex_mod if ex_mod < 0 else -oh["idle_decay"]
            rec["excite"] = max(0, int(rec.get("excite") or 0) + decel)
            in_scene = True        # 还在这场里，只是这一轮没动
    else:
        # 本来就不在场景里：保持 0
        in_scene = False
        rec["excite"] = 0
        rec["orgasms"] = 0
        total_orgasm = 0
        idle = 0
    rec["idle"] = idle
    rec["scene"] = in_scene

    force_ending = ""
    if cross:
        if cross == "hard":
            rec["cross_n"] = int(rec.get("cross_n") or 0) + 1
            extra = -CROSS_HARD
        elif allow == "no":
            n = int(rec.get("cross_n") or 0)
            extra = -CROSS_PENALTY[min(n, len(CROSS_PENALTY) - 1)]
            rec["cross_n"] = n + 1
        else:
            extra = 0             # 半推半就 / 她答应了：这一步不罚
        if extra:
            prev_value = value
            value = max(MIN_V, min(MAX_V, value + extra))
            applied += value - prev_value
        # 2026-11-04 深夜：**不再判结局**。原来 hard 累计 2 次就进局子、锁死
        # 数值——用户要在胁迫/半推半就这条线上玩下去，那条规则等于玩到一半
        # 把桌子掀了。现在越界的代价只有：掉羁绊（上面）+ 推高堕落（下面）。
        #
        # `force_ending` 这个变量留着，方便以后加真的（非报警的）结局分支；
        # 现在是空串，`ending_for` 也恒返回 None，所以结局永远不会触发。

    # ── 堕落 / 屈服（2026-11-04 深夜）──────────────────
    # 两根轴的分工见 `corrupt_levels` 上面的注释。这里只做记账：
    #   `corrupt` 模型给增量，**负值不生效**——堕落是只涨不掉的，
    #             唯一的例外是 `soothe`（事后安抚），由模型显式给。
    #   `submit`  模型给增量，正负都收：她可以被压服，也可以顶回去。
    # 代码不做「越界自动 +N」的硬编码——涨多少交给模型判，它读得到上下文。
    # 唯一例外是 `cross=hard` 时给一个**下限**：那种事不可能毫无后果。
    ch = corrupt_hints(cfg)
    prev_corrupt = int(rec.get("corrupt") or 0)
    try:
        cor_delta = int(score.get("corrupt") or 0)
    except (TypeError, ValueError):
        cor_delta = 0
    if cross == "hard":
        # 她说过不要、还被继续了：至少按「她明确说过不要」那一档记账。
        # 这是**下限**不是加成——模型给了更多的照它给的来。
        #
        # 下限取 `她明确说过不要`（10）和 `越界被拒还继续`（12）里大的那个，
        # 再乘 2：单纯 hard 是「他越界了」，而 hard + allow=no 是「她说了不
        # 他还做」——这两件事的量级不该一样。《2026-11-04 深夜：原来只给
        # 10，用户要的三次越界连「开始松」都推不到，太慢。》
        floor = max(ch["up"].get("她明确说过不要", 10),
                    ch["up"].get("越界被拒还继续", 12))
        if str(allow or "") == "no":
            floor *= 2
        if cor_delta < floor:
            cor_delta = floor
    if cor_delta < 0 and str(score.get("soothe") or "") not in ("1", "true", "True"):
        # 模型没显式说「这是安抚」，就不许往下扣。不设这个口子的话，
        # 「堕落」会随着模型心情来回涨跌，那就不叫堕落了。
        cor_delta = 0
    # 高潮也算一次堕落（2026-11-04 深夜，用户追加）。
    # 为什么要有这一条：在此之前堕落**只有越界能涨**——玩家要是规规矩矩地
    # 顺着气氛做到底，堕落永远停在 0，两轴四格里只用得上左边一列。
    # 「她在这场里到了」本身就是一种往下陷：身体先于心意投降，这是这套设定里
    # 最不该白给的一次。
    # 只在她**已经到了**的那一拍加，不是每轮加；而且只在正常场景里加——
    # 越界那轮已经按 hard 的下限记过账了，别叠两次。
    if just_orgasm and not cross:
        cor_delta += ch["up"].get("她在这场里到了", 6)
    rec["corrupt"] = max(0, min(CORRUPT_MAX, prev_corrupt + cor_delta))

    prev_submit = int(rec.get("submit") or 0)
    try:
        sub_delta = int(score.get("submit") or 0)
    except (TypeError, ValueError):
        sub_delta = 0
    sub_delta = max(-40, min(40, sub_delta))
    rec["submit"] = max(0, min(SUBMIT_MAX, prev_submit + sub_delta))

    # ── 戒备记账（2026-11-05）────────────────────────────
    # 涨落全由打分模型判（判据在 prompt 的 suspicion 字段），代码只做两件
    # 事：clamp 到 -20~+20，和给「越界被拒」兜下限——同 corrupt 的 hard
    # 下限同构：她说过不要还被继续，这种事不可能不起戒心，模型手松也不能
    # 让它记成 0。
    try:
        sus_delta = int(score.get("suspicion") or 0)
    except (TypeError, ValueError):
        sus_delta = 0
    sus_delta = max(-20, min(20, sus_delta))
    if cross == "hard":
        sus_delta = max(sus_delta, SUSPICION_HARD_FLOOR)
    elif cross == "mild" and allow == "no":
        sus_delta = max(sus_delta, SUSPICION_NO_FLOOR)
    rec["suspicion"] = max(0, min(SUSPICION_MAX, sus_prev + sus_delta))

    # ── 距离升级 ─────────────────────────────────────
    # 升一级要两个条件：模型判定「这轮做成了那件事」+ 好感够这一级的门票。
    # 缺一个都不升，**只升不降**（身体和记忆不会因为分数掉回去而复原）。
    cfg_levels = levels_of(cfg)
    lv = int(rec.get("level") or 0)
    rose = 0
    if bool(score.get("beat")) and lv < LEVEL_MAX:
        nxt = lv + 1
        gate = int(level_cfg(nxt, cfg).get("gate") or 0)
        if value >= gate:
            lv = nxt
            rose = nxt
            rec["level"] = lv

    # 越线结局：一次就触发
    just_ended = ""
    ending = force_ending or ending_for(value, cfg)
    if ending:
        rec["ending"] = ending
        just_ended = ending

    # 里程碑（2026-11-04 晚）：和结局长得很像，但**不锁数值**。
    # 跨过 50 的那一刻给一段叙事（「在一起」），记下来，回头不再重复。
    # 结局回合不再判里程碑——那一轮要说的是收场，不是开工。
    just_milestone = ""
    if not ending:
        mk = milestone_for(value, rec, cfg)
        if mk:
            rec["milestone"] = mk
            just_milestone = mk

    # 档位：升档立即，降档连续 DOWN_CONFIRM 轮才认
    cur_key = str(rec.get("tier") or "")
    new_t = tier_for(value, cfg) or {}
    changed = False
    if new_t.get("key") != cur_key:
        cur_t = next((t for t in (cfg.get("tiers") or [])
                      if t.get("key") == cur_key), None)
        cur_min = int(cur_t.get("min") or 0) if cur_t else 0
        new_min = int(new_t.get("min") or 0)
        if new_min > cur_min:                 # 升档（或升回来）：立即
            rec["tier"] = str(new_t.get("key") or "")
            rec["down"] = 0
            changed = True
        else:                                 # 掉档：要连续确认
            rec["down"] = int(rec.get("down") or 0) + 1
            if rec["down"] >= DOWN_CONFIRM:
                rec["tier"] = str(new_t.get("key") or "")
                rec["down"] = 0
                changed = True
    else:
        rec["down"] = 0

    hist = rec.get("hist") if isinstance(rec.get("hist"), list) else []
    hist.append({"d": applied, "r": reason,
                 "ts": datetime.datetime.now().strftime("%m-%d %H:%M")})
    rec["hist"] = hist[-HIST_KEEP:]
    rec["value"] = value

    # 对外报**生效档位**（滞回扣着的那个），不是按值现算的——不然注入和
    # 尾行会先一步换档，玩家看到她态度跳来跳去。
    eff_key = str(rec.get("tier") or "")
    eff_t = next((t for t in (cfg.get("tiers") or [])
                  if t.get("key") == eff_key), None) or new_t
    tv = _tier_view(eff_t, lv)
    gauge = intimacy_gauge(lv, value, _card_of(line_key), cfg)
    res = {"applied": applied, "active": True, "mode": MODE_RAID, "value": value,
           "tier": eff_key, "changed": changed,
           "tierName": tv["name"] or str(eff_t.get("name") or ""),
           "level": lv, "levelMax": LEVEL_MAX,
           "levelName": level_name(lv, cfg, _card_of(line_key)),
           "intimacy": gauge,
           "milestone": str(rec.get("milestone") or ""),
           "just_milestone": just_milestone,
           "excite": int(rec.get("excite") or 0),
           "exciteName": excite_name(rec.get("excite"), cfg),
           "in_scene": bool(rec.get("scene")),
           "act": act_key, "just_orgasm": just_orgasm,
           "orgasms": total_orgasm,
           "orgasmMax": oh["max_orgasm"],
           "sceneCapped": False,
           # 开发度（2026-11-05）：五部位原值 + 每部位档名 + 总百分比。
           # 面板要「当前/500」，演员要「她身上哪里被开发到什么程度」。
           "dev": {k: int(rec.get("dev", {}).get(k) or 0) for k in DEV_KEYS},
           "devNames": {k: dev_name(rec.get("dev", {}).get(k), cfg)
                        for k in DEV_KEYS},
           "devDelta": dev_gain,
           "devHit": dev_hit,
           "devHook": dev_hook,
           "devTotal": dev_total(rec.get("dev")),
           "devMax": dev_cfg(cfg)["max"] * len(DEV_KEYS),
           "devPct": int(round(dev_total(rec.get("dev"))
                               / float(dev_cfg(cfg)["max"] * len(DEV_KEYS)) * 100)),
           "devAllFull": dev_all_full(rec.get("dev"), cfg),
           "sensitivity": round(sens, 2),
           "forced": bool(forced_now),
           "rose": rose, "arousal": int(rec.get("arousal") or 0),
           "arousalName": arousal_name(rec.get("arousal"), cfg),
           "cross": cross, "allow": allow, "crossN": int(rec.get("cross_n") or 0),
           "corrupt": int(rec.get("corrupt") or 0),
           "corruptName": corrupt_name(rec.get("corrupt"), cfg),
           "corruptDelta": int(rec.get("corrupt") or 0) - prev_corrupt,
           "submit": int(rec.get("submit") or 0),
           "submitName": submit_name(rec.get("submit"), cfg),
           "submitDelta": int(rec.get("submit") or 0) - prev_submit,
           "suspicion": int(rec.get("suspicion") or 0),
           "suspicionName": suspicion_name(rec.get("suspicion"), cfg),
           "suspicionDelta": int(rec.get("suspicion") or 0) - sus_prev,
           "ending": str(rec.get("ending") or ""),
           "mercy": mercy_left(rec), "mercyMax": MERCY_MAX,
           "just_ended": just_ended, "reason": reason}

    # 成就（2026-11-04）：只认确定性事实，判定在 `achievements` 里。
    # 放在最后——升级、结局、越界次数这一刻才都定下来了。
    from app import achievements
    fresh = achievements.check(rec, res, cfg, _card_of(line_key))
    if fresh:
        achievements.mark(rec, fresh)
        res["ach"] = fresh

    ent["affection"] = rec
    _save_entry(line_key, ent)
    return res


def rollback_last(line_key):
    """把最近一次 settle 的效果整体退回去（2026-11-04 回退功能）。

    实现靠 settle 动手前的 `_prev` 快照：整份记录原样换回去，不逐字段
    倒推。**只支持一级**——再上一轮的快照早被这轮盖掉了，这是设计：
    回退是「发错了救一下」，不是时光机。

    返回还原后的状态 dict（和 settle 的返回同构，调用方可直接回给前端
    刷新状态条）；没有可退的（自由模式没结算过 / 结局定格轮 / 已经退过）
    返回 None。历史和归档的删除不归这里管——在 `persona.rollback_last_turn`。
    """
    ent = _entry(line_key)
    if not ent:
        return None
    rec = ent.get("affection")
    if not isinstance(rec, dict) or "_prev" not in rec:
        return None
    prev = rec.pop("_prev")
    ent["affection"] = prev
    _save_entry(line_key, ent)
    cfg = load_config()
    card = _card_of(line_key)
    lv = int(prev.get("level") or 0)
    value = int(prev.get("value") or 0)
    return {"applied": 0, "rolled_back": True, "active": True,
            "mode": str(prev.get("mode") or MODE_RAID), "value": value,
            "tier": str(prev.get("tier") or ""),
            "tierName": str((tier_for(value, cfg) or {}).get("name") or ""),
            "level": lv, "levelMax": LEVEL_MAX,
            "levelName": level_name(lv, cfg, card),
            "arousal": int(prev.get("arousal") or 0),
            "arousalName": arousal_name(prev.get("arousal"), cfg),
            "excite": int(prev.get("excite") or 0),
            "in_scene": bool(prev.get("scene")),
            "corrupt": int(prev.get("corrupt") or 0),
            "corruptName": corrupt_name(prev.get("corrupt"), cfg),
            "corruptDelta": 0,
            "submit": int(prev.get("submit") or 0),
            "submitName": submit_name(prev.get("submit"), cfg),
            "submitDelta": 0,
            "suspicion": int(prev.get("suspicion") or 0),
            "suspicionName": suspicion_name(prev.get("suspicion"), cfg),
            "suspicionDelta": 0,
            "ending": str(prev.get("ending") or ""),
            "milestone": str(prev.get("milestone") or ""),
            "just_ended": "", "just_milestone": ""}


# ── 注入与展示 ────────────────────────────────────────

# 场外能翻到多深（2026-11-03）：给主模型一个**事实**——她现在让你看到哪一层。
# 代码只报事实，写不写、写多深、翻到什么，全交给模型判断。
# 2026-11-04：从「绑好感档位」改成「绑距离层级」。以前好感一过 20 就说
# 「几乎不再藏了」，可她连你的手都没碰过——场外那段当场穿帮。
_DEPTH_BY_LEVEL = {0: "low", 1: "low", 2: "mid", 3: "mid", 4: "high", 5: "high"}
# 老调用（传档位 key）的映射，保留兼容
_DEPTH_LEVEL = {
    "miffed": "low", "cold": "low", "forced": "low", "fallen": "low",
    "normal": "mid", "touch": "mid",
    "start": "high", "care": "high", "full": "high",
}
_DEPTH_TEXT = {
    "low": "她**不打算让你看到什么**——你能翻到的只有她无所谓的东西"
           "（外卖单、没关的屏幕、随手扔的衣服）。往深里翻，她会当成冒犯。",
    "mid": "她**允许你看到一部分**——衣柜、书桌、写了一半的东西会自然落进"
           "你眼里；真正私密的还锁着。",
    "high": "她**几乎不再藏了**——速写本、旧照片、她偷偷留着的东西，"
            "都摆在你翻得到的地方。",
}


def depth_hint(level):
    """按**距离层级**给出「她现在让你看到多深」。未知返回空串——
    多注入一句反而会被模型当成硬规则。"""
    if isinstance(level, str) and not level.strip().isdigit():
        return _DEPTH_TEXT.get(_DEPTH_LEVEL.get(level.strip(), ""), "")
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return ""
    return _DEPTH_TEXT.get(_DEPTH_BY_LEVEL.get(lv, ""), "")


def _tier_view(t, level):
    """这一档在你现在这个距离上该怎么念。

    前四档的原文案写的是**身体进度**（「界限已经松了」「开始凿」），
    那是距离 4~5 级才成立的画面。距离还没到的时候只有感情，
    用 `prompt_soft`（用户随时可以改），没有 soft 版就退回原文。
    """
    t = t if isinstance(t, dict) else {}
    try:
        lv = int(level or 0)
    except (TypeError, ValueError):
        lv = 0
    if lv >= 4:
        return {"name": str(t.get("name") or ""),
                "prompt": str(t.get("prompt") or "")}
    soft_name = str(t.get("name_soft") or "").strip()
    soft_txt = str(t.get("prompt_soft") or "").strip()
    return {"name": soft_name or str(t.get("name") or ""),
            "prompt": soft_txt or str(t.get("prompt") or "")}


def _can_cross(value, level, arousal, cfg=None):
    """**双轴门槛**：以你们现在的进度，她接不接得住。

    2026-11-04 深夜改法——这是整套机制最重要的一处。旧版是单轴的：

        value >= guard - arousal // 5

    也就是说**冲动从来不是门槛**，只是给好感打个 -5~-20 的折扣。结果就是
    用户实测到的那个毛病：好感 40、冲动 0，`40 >= 15` 成立，她「挡不住」，
    玩家可以直接推倒——「好感够了就能做爱」，中间没有气氛这一环。

    现在两条都要过：

        value >= 这一级的 gate   （羁绊够不够）
        arousal >= 这一级的 arousal_gate   （她此刻想不想要）

    想推倒她得多堆几轮气氛：喝酒、独处、触碰、情话、深夜，每轮还会自然
    回落 15——所以「今天晚上推不了」是真的推不了，得先把气氛拱起来。

    这两个都成立只是「她能接得住」，**不是「她一定答应」**：她还是可以
    装没听懂、可以绕、可以嘴上说不要身体不躲。判定权始终在模型手里。

    **压迫模式（2026-11-05）**：`_ensure` 之后 `settle` 会把 `forced` 标进
    `res`，注入层据此换一套说法。这里不掺和——好感门槛照旧是门槛，
    压迫戏也不该白给。
    """
    lc = level_cfg(level, cfg)
    gate = int(lc.get("gate") or 0)
    agate = lc.get("arousal_gate")
    if agate is None:
        # 老配置没这个字段：退回旧单轴判法（至少不要让老配置炸掉）
        guard = int(lc.get("guard") or 0)
        return int(value or 0) >= guard - int(arousal or 0) // 5
    return (int(value or 0) >= gate
            and int(arousal or 0) >= int(agate))


# ── 压迫模式（2026-11-05）──────────────────────────────
#
# 用户原话：「好感度低 + 堕落值 + 高潮，就可以写这种有点屈辱的、有点强迫的
# 感觉，因为都是不同的玩法」。
#
# 为什么需要它单独一层，而不是让模型自己看着办：原来的注入是**双轴门槛**
# 的教学式写法——「感情够 + 气氛够才接得住」，「接不住」时注入会说「她挡得
# 住，越界她躲得开，也说得出口」。这句话和「她堕落 60」同时出现时，模型
# 手里是两条互相打架的指令，最后写出来的通常是半推半就的温吞水。
#
# 所以这里给一道**显式覆盖**：三件事同时成立时，机制层面承认这一场是压迫戏，
# 注入换成那一套说法，并把笔法层切到 `forced` 轴。
#
# 三个条件缺一不可：
#   · 好感低  —— 「她愿意」这个托词不成立，她要的不是你
#   · 堕落高  —— 她已经被弄坏了一截，不会真的翻脸走人
#   · 气氛/身体在动 —— 这一拍真的会发生（光有前两个只是「她怕你」）
#
# 缺一样都不算：好感低 + 堕落也低 → 她挡得住，就是普通的越界被拒；
# 堕落高但气氛没起来 → 她还没到那一步，硬写就是无来由的崩塌。

def forced_cfg(cfg=None):
    """压迫模式的门槛配置。配置坏了就用兜底值，机制不能因配置挂掉。"""
    cfg = cfg or load_config()
    raw = cfg.get("forced")
    if not isinstance(raw, dict):
        raw = DEFAULT_CONFIG.get("forced") or {}

    def _i(k, dv):
        try:
            return int(raw.get(k, dv))
        except (TypeError, ValueError):
            return dv

    steps = raw.get("corrupt_steps")
    if not isinstance(steps, list) or not steps:
        steps = (DEFAULT_CONFIG.get("forced") or {}).get("corrupt_steps") or []
    try:
        steps = sorted(int(x) for x in steps)
    except (TypeError, ValueError):
        steps = [25, 50, 70, 90]
    return {"value_max": _i("value_max", 20),
            "corrupt_min": _i("corrupt_min", 45),
            "arousal_min": _i("arousal_min", 30),
            "discount": _i("discount", 0),
            "corrupt_steps": steps}


def corrupt_stage(corrupt, cfg=None):
    """堕落跨到第几档（0~4）。跨一档，压迫模式的门槛降一次。"""
    co = int(corrupt or 0)
    n = 0
    for step in forced_cfg(cfg)["corrupt_steps"]:
        if co >= int(step):
            n += 1
    return n


def forced_arousal_min(corrupt, cfg=None):
    """这一档堕落对应的冲动门槛。第一次要堆满气氛，第二次只要一半。"""
    fc = forced_cfg(cfg)
    floor = max(0, fc["arousal_min"] - corrupt_stage(corrupt, cfg) * fc["discount"])
    return floor


def is_forced(value, corrupt, arousal=0, excite=0, cfg=None):
    """这一拍是不是压迫戏。**只在攻略模式里判**（调用方保证）。"""
    fc = forced_cfg(cfg)
    return (int(value or 0) < fc["value_max"]
            and int(corrupt or 0) >= fc["corrupt_min"]
            and (int(arousal or 0) >= forced_arousal_min(corrupt, cfg)
                 or int(excite or 0) > 0))


def forced_progress(corrupt, arousal=0, cfg=None):
    """压迫模式给面板看的进度：她已经被推到哪一档、还差多少冲动。

    返回 `{on, stage, need, gap}`——`gap` 为 0 表示现在就能进这一场。
    """
    fc = forced_cfg(cfg)
    stage = corrupt_stage(corrupt, cfg)
    need = forced_arousal_min(corrupt, cfg)
    gap = max(0, need - int(arousal or 0))
    return {"on": int(corrupt or 0) >= fc["corrupt_min"],
            "stage": stage, "need": need, "gap": gap}


def forced_gap(corrupt, arousal=0, cfg=None):
    """压迫模式还差多少冲动。已经够了返回 0。

    单独给一个函数是因为注入里那句要**说具体**（「还差 8 点」），
    但只用得上一个数，不用把整个 dict 搬过去。
    """
    return forced_progress(corrupt, arousal, cfg)["gap"]




def _cross_gap(value, level, arousal, cfg=None):
    """**当前这一级**还差多少才接得住。返回 `(缺羁绊, 缺冲动)`。

    和 `_need_to_cross` 的分工（2026-11-04 深夜拆开）：
      - `_cross_gap`    —— 「这一轮她挡不挡得住」。`_can_cross` 的判据，
                          缺口也是按**当前级**算，因为问的是「现在能不能做」。
      - `_need_to_cross` —— 「升到下一级还差什么」。面板和结论句用，
                          按**下一级**门槛算，因为问的是「再往前一步」。
    这两个数字一样，语义差一整级。混用一次就会让注入说错话。
    """
    lc = level_cfg(level, cfg)
    dv = max(0, int(lc.get("gate") or 0) - int(value or 0))
    ag = lc.get("arousal_gate")
    da = 0 if ag is None else max(0, int(ag) - int(arousal or 0))
    return dv, da


def _need_to_cross(value, level, arousal, cfg=None):
    """离「再往前一步」还差什么。返回 `(缺羁绊多少, 缺冲动多少)`，都够是 (0, 0)。

    2026-11-04 深夜修正：这里原来算的是**当前这一级**的门槛，
    也就是「以现在的进度她接不接得住」。但面板和注入里那句话说的是
    「再往前一步」——玩家想知道的是**升到下一级**还差什么。
    两者差整整一级：站在 lv1、羁绊 15、冲动 12 时，
    按当前级算是「差 0 / 差 8」（看着快到了），按下一级算才是
    「差 5 / 差 23」——这才是真实的距离。

    已经到顶（lv 是最高一级）时没有「下一级」，退回按当前级算，
    免得报一个不存在的门槛。
    """
    lv = int(level or 0)
    try:
        lv = max(0, min(LEVEL_MAX, lv))
    except (TypeError, ValueError):
        lv = 0
    target = lv + 1 if lv < LEVEL_MAX else lv
    return _cross_gap(value, target, arousal, cfg)



def injection_block(res, cfg=None):
    """主对话（第二段）系统头里追加的那段。自由模式返回空串。"""
    if not isinstance(res, dict) or not res.get("active"):
        return ""
    cfg = cfg or load_config()
    lines = ["## 你们现在到这一步了（系统按状态结算的事实，照这个演，不要自己改）", ""]
    value = int(res.get("value") or 0)
    if res.get("ending") and not res.get("just_ended"):
        # 结局已定格（切模式回来接着演的线）：维持结局情绪，不再结算
        ec = ending_cfg(res["ending"], cfg)
        lines.append("剧情已经停在结局【%s】，关系不会再变。" % (ec.get("name") or ""))
        txt = str(ec.get("prompt") or "").strip()
        if txt:
            lines += ["", txt]
        return "\n".join(lines)
    if res.get("just_ended"):
        ec = ending_cfg(res["just_ended"], cfg)
        lines.append("**剧情到达结局：【%s】**" % (ec.get("name") or ""))
        txt = str(ec.get("prompt") or "").strip()
        if txt:
            lines += ["", txt]
        lines += ["", "这是收场回合：把这段结局剧情演完、收住。"]
        return "\n".join(lines)

    # 里程碑（2026-11-04 晚）：不是收场。**必须和历史结局撇干净**——
    # 模型见过太多「关系确立 = 全文完」的收尾，这里要把话说死。
    if res.get("just_milestone"):
        mc = milestone_cfg(res["just_milestone"], cfg)
        lines.append("**关系走到里程碑：【%s】**" % (mc.get("name") or ""))
        txt = str(mc.get("prompt") or "").strip()
        if txt:
            lines += ["", txt]
        lines += ["", "这一轮把这一刻演出来。**这不是收场**：故事接着往下走，"
                      "数值照常结算，后面还有日子要过——别写「全剧终」那种收尾。"]

    lv = int(res.get("level") or 0)
    # 不报好感数字：好感和「进度」本来就可以不同步（好感够了但没做成事，
    # 你们还是原来那一步）。两个数字一起摆出来，模型会纠结该信哪个。
    lines.append("关系：【%s】  她对你的感觉：【%s】"
                 % (res.get("levelName") or "?", res.get("tierName") or "?"))
    lp = str(level_cfg(lv, cfg).get("prompt") or "").strip()
    if lp:
        lines += ["", lp]

    # 这一刻的气氛：冲动决定「她此刻想不想」，双轴门槛决定「接不接得住」
    ar = int(res.get("arousal") or 0)
    ex = int(res.get("excite") or 0)
    can = _can_cross(value, lv, ar, cfg)
    co = int(res.get("corrupt") or 0)
    # 压迫模式（2026-11-05）：三件事同时成立才算——好感低、堕落高、气氛在动。
    # 判定放在注入里做而不是 settle 里，是因为它**随这一轮的气氛变**：
    # 同一段堕落，冲动掉下来这一场就不是压迫戏了，下一轮拱起来又是。
    forced = is_forced(value, co, ar, ex, cfg)

    # 档位文案按**生效档位**取（res["tier"]），不能用 tier_for(value)——
    # 降档滞回期间两者不一致，会提前把新档位的语气漏给模型。
    #
    # 2026-11-04 深夜：**在场景里（高潮值 > 0）且这个档位有 `prompt_intimate`，
    # 就改用做爱版文案**。这是「露骨方向」最关键的一处改动——旧版只按距离
    # 挑 soft/荤两版，导致「关系冷着她还让你做」这件事根本没有文案支撑，
    # 模型只能自己往「她不情愿→你该停」的方向找补。现在 cold / fallen 都有
    # 自己那一版：做了，但冷着做、麻木地做。
    t = next((x for x in (cfg.get("tiers") or [])
              if x.get("key") == res.get("tier")), None) or tier_for(value, cfg) or {}
    # 压迫戏的档位文案另取一套（2026-11-05）：`prompt_intimate` 是写给
    # 「她其实愿意」的那条线的（「她什么都要，也什么都敢要」），这一场用那句
    # 就没有屈辱可言了。有 `prompt_forced` 就用它，没有退回原文案——
    # 老配置不改也能跑。
    intimate = str(t.get("prompt_intimate") or "").strip()
    t_forced = str(t.get("prompt_forced") or "").strip()
    if ex > 0 and forced and t_forced:
        tv = {"name": str(t.get("name") or ""), "prompt": t_forced}
    elif ex > 0 and intimate:
        tv = {"name": str(t.get("name") or ""), "prompt": intimate}
    else:
        tv = _tier_view(t, lv)
    txt = tv["prompt"].strip()
    if txt:
        lines += ["", txt]
    hint = depth_hint(lv)
    if hint:
        lines += ["", "（她现在让你看到多深：%s）" % hint]

    # 门槛这一句要**说具体**：只写「她挡得住」等于什么都没说，
    # 模型不知道差的是感情还是气氛，只能瞎猜。
    dv, da = _need_to_cross(value, lv, ar, cfg)
    if ar >= 35 and value < 0:
        # 负分档（2026-11-04）：她这时候不可能「有点乱到会绕」——气还没消，
        # 冲动高也只是把火拱得更旺。原来的暧昧句会让模型演成「半推半就」，
        # 和这几档「一路挡掉身体靠近」的文案打架。这里只纠方向，不判结果。
        #
        # 2026-11-04 深夜补：负分档**不是清教徒**。cold / fallen 有
        # `prompt_intimate` 那一版——如果已经在场景里，就是「做了，但冷着做 /
        # 麻木地做」。这里只纠正「被动情」的误读，不禁止场景本身。
        if ex > 0:
            lines += ["", "（她心里是有波动的，但那不是心动——是气，是失望，是还没算完的账。"
                          "这一场可以是她让了、认了、或者根本不在乎了；但别写成她动情。）"]
        else:
            lines += ["", "（她心里是有波动的，但那不是心动——是气，是失望，是还没算完的账。"
                          "别把这点波动演成愿意。）"]
    elif forced:
        # 压迫模式（2026-11-05）：**顶掉**「她挡得住」和「她接得住」两句。
        # 这两句和「她已经堕落成这样」同时出现时，模型只能写半推半就，
        # 那正好是最没劲的写法。这里只陈述事实，不催它写，写多狠仍由它判。
        gap = forced_gap(co, ar, cfg)
        tail = ("门槛还差 %d 点冲动" % gap) if gap else "这一场现在就能开"
        lines += ["", "（这一拍不是她愿意的。她心里清楚你不是她想要的那种人——"
                      "可她已经被弄成现在这样了，拦不住的是她自己的反应。%s。"
                      "写的时候把这两件事分开：嘴上、眼里在拒，身上在应，"
                      "中间那道裂缝才是这一场的看点。）" % tail]
    elif can:
        # 已经接得住了（当前这一级门槛过了）。这里**不再报「还差多少」**——
        # 那句数字现在算的是「升到下一级还差什么」（见 `_need_to_cross`），
        # 和「这一轮能不能做」不是一回事，混在一起说模型会以为还不能。
        lines += ["", "（她这会儿接得住：感情够，气氛也到这儿了。"
                      "这个状态下的她可能主动——先开口、先靠近、先留下你。）"]
    else:
        # 只在「接不住」的时候报缺口。数字是当前这一级的门槛，
        # 也就是「她要答应，你还得补多少」。
        cdv, cda = _cross_gap(value, lv, ar, cfg)
        gap = []
        if cdv:
            gap.append("感情还差 %d 点" % cdv)
        if cda:
            gap.append("这会儿的气氛还差 %d 点（冲动会每轮回落，得自己拱）" % cda)
        lines += ["", "（她挡得住——%s。这个状态下越界，她躲得开，也说得出口。"
                      "她不会一上来就把门关死：多半是先按住你的手、说一句「等一下」，"
                      "把话头岔开，或者干脆装作没听懂。）" % "、".join(gap or ["两样都还差点"])]


    if res.get("cross"):
        if res.get("cross") == "hard":
            lines += ["", "（她刚才明确表示不愿意，你还是继续了。她现在的反应不是害羞，"
                          "是怕，或者恼。）"]
        elif str(res.get("allow") or "") == "no":
            lines += ["", "（她拒绝了。这一轮她不会松口——除非你先退回到安全的位置。）"]
        elif forced:
            # 压迫场里「没明确答应也没走开」不是暧昧，是**拦不住**。
            # 同一句原文在别的场合最好看，在这里会把屈辱改写成半推半就。
            lines += ["", "（她没答应，也没能走开。这一刻她脸上的东西不是欲拒还迎，"
                          "是「怎么会这样」——写她的表情和呼吸，别写她配合。）"]
        else:
            lines += ["", "（她没有明确答应，也没有走开。这个状态最难看也最好看。）"]

    # 一条硬约束：不许每轮都给干脆答案
    lines += ["", "（怎么反应由你演，但**别每轮都给干脆的答案**：她可以装没听懂、可以反问、"
                  "可以嘴上说不要但没躲、可以答应完自己后悔。真实的犹豫比干脆的拒绝好看。）"]

    # ── 压迫戏的主旋律（2026-11-05）────────────────────────
    # 上面那句「别给干脆答案」在压迫场里方向是反的：那一场**必须要有一个
    # 干脆的拒绝**——她得真的不愿意，屈辱才立得住。所以这里给它一道覆盖，
    # 并把「她不知道自己要什么」改写成「她知道自己不要什么」。
    if forced:
        lines += ["", "（**这一场的主旋律是屈辱，不是甜。** 和上面那句相反："
                      "这一场**必须有一个明确的、具体的拒绝**——她别过脸、她说「不要」、"
                      "她推着你的胸口说「松开」，而且要写实，不要写成「她挣扎了一下」。"
                      "她清楚自己在被怎么对待，别让她中途变成心甘情愿。"
                      "快感是加在她身上的，不是她要的：她会为自己的反应感到羞耻，"
                      "这个羞耻比快感更该被写出来。）"]
        lines += ["", "（事后必须留痕：她躲开、不说话、去洗手、第二天不敢看你，"
                      "或者把被弄乱的衣服一件件穿回去、穿得很慢。这一场不会因为结束就不存在。"
                      "**别写事后温存**——那是另一条线的写法。）"]

    # ── 反客为主（2026-11-04 晚，用户要的；深夜调阈值）────────
    # 冲动和**高潮值**都会把她往「自己先动」的方向推：冲动是「她此刻想要」，
    # 高潮值是「这一场里她已经到了」。两个都高的时候，她不该还躺在那里
    # 等他下一步——那样再高的数值也只是个装饰。
    #
    # 深夜调整：旧阈值（冲动 75 / 高潮 60 就「可能会」）太松——她第一次做
    # 的时候不该就自己坐上来。现在分两档，而且**必须先过了双轴门槛**。
    #
    # 2026-11-05 压迫场里**整段跳过**：那一场她不是「想要」，是自己先动
    # 就等于把屈辱线写成了甜线。她能做的极限是身体先贴上去再自己停下、
    # 骂自己一句——这个张力放在 `forced` 那段里写，不走「反客为主」。
    if can and value >= 0 and not forced:
        lead = ""
        if ar >= 88 or ex >= 85:
            lead = "will"
        elif ar >= 65 or ex >= 55:
            lead = "may"
        if lead == "will":
            lines += ["", "（她这会儿不是在等你。**这一轮大概率由她先动**——先开口、先凑过来、"
                          "先伸手，动作比她平时快，也比平时不客气。她已经顾不上谁先谁后了，"
                          "你别抢着替她做决定；她要什么就让她自己说出来、自己做到。）"]
        elif lead == "may":
            lines += ["", "（她有可能会自己先动一下——不保证，但别把她写成只能被动等着。"
                          "她伸手、她凑近、她说出那句平时不会说的话，都算。）"]

    # 高潮值（2026-11-04 深夜）：只在场景里报。报的是「她身上正发生到什么
    # 程度」，五档正文直接是身体信号，写的时候照抄那个层次就行。
    if ex > 0:
        el = orgasm_level(ex, cfg)
        en = str(el.get("name") or "")
        ep = str(el.get("prompt") or "").strip()
        if en:
            body = "（这一场进行到现在，她已经到【%s】了。" % en
            if ep:
                body += " " + ep
            lines += ["", body + "）"]

    # 刚高潮（2026-11-04 深夜）：这一拍是**余韵**，不是重新开始
    jo = int(res.get("just_orgasm") or 0)
    if jo:
        lines += ["", "（她刚刚到了一次（第 %d 次）。**这一轮写的是余韵**：人还软着、"
                      "抖还没停、说不出完整的话。别立刻接着往下推——她想缓，"
                      "或者嘴上说着不行了身体却还缠着你不放，两种都行。"
                      "剩下的空间留到下一轮。）" % jo]
    if res.get("sceneCapped"):
        lines += ["", "（这一场到这儿收住。不是她不想了，是已经累到没力气再要。"
                      "**这一轮要写收尾**：擦、抱、缓过来，或者干脆让她睡过去。"
                      "别再接着写这一场。）"]

    # ── 堕落 / 屈服（2026-11-04 深夜）────────────────────
    # 这两段注入**不写数字**，只写「她现在是个什么状态」——数字是给玩家看的，
    # 模型该看到的是人。堕落那段尤其重要：它是**越界之后唯一的叙事回报**，
    # 拿掉「进局子」之后，戏的张力全落在这里。
    #
    # `co` 开头那段几行前已经取过一次（压迫模式判定要用），这里不再重取。
    su = int(res.get("submit") or 0)
    co_d = int(res.get("corruptDelta") or 0)
    su_d = int(res.get("submitDelta") or 0)
    if co > 0:
        cp = str(corrupt_level(co, cfg).get("prompt") or "").strip()
        if cp:
            lines += ["", "（她被你弄成这样了：%s）" % cp]
    if co_d > 0:
        lines += ["", "（这一轮她又碎了一点。**别写成她突然乐意了**——堕落不是喜欢，"
                      "是她越来越挡不住自己、越来越不想去想了。写那个过程。）"]
    if co_d < 0:
        lines += ["", "（这一轮你把她往回拉了一点。她会缓过来，但那道裂缝还在。）"]
    if su > 0:
        sp = str(submit_level(su, cfg).get("prompt") or "").strip()
        if sp:
            lines += ["", "（她现在对你是什么态度：%s）" % sp]
    if su_d > 0:
        lines += ["", "（她这一轮服软了。服软不等于心甘情愿——把她心里那点不服气留着。）"]
    if su_d < 0:
        lines += ["", "（她这一轮顶回来了。让她硬气一回，别下一句又软下去。）"]
    if str(res.get("cross") or "") == "hard":
        # 越界那一轮专门说一句：堕落是拿关系换来的，不是白得的
        lines += ["", "（她说了不要，你还是继续了。这件事会记在她身上——她也许当下没反抗，"
                      "但那不是同意。堕落值越高，她越不会为了这个跟你吵，可那反而更糟。）"]

    # ── 开发度（2026-11-05）──────────────────────────────
    # 给演员的只有两件事：**她身上哪一处已经被开发到什么程度**，以及
    # **这一档的身体会自己背叛到什么地步**。不给数字当门槛——数字是给玩家
    # 看进度用的，演员要的是画面。
    dev = res.get("dev") if isinstance(res.get("dev"), dict) else {}
    dev_names = res.get("devNames") if isinstance(res.get("devNames"), dict) else {}
    if isinstance(dev, dict) and any(int(dev.get(k) or 0) for k in DEV_KEYS):
        parts = []
        for k in DEV_KEYS:
            v = int(dev.get(k) or 0)
            if v <= 0:
                continue
            parts.append("%s %d（%s）" % (DEV_LABELS.get(k, k), v,
                                          dev_names.get(k) or dev_name(v, cfg)))
        if parts:
            lines += ["", "（她身上被开发到的程度：%s。）" % "／".join(parts)]
        # 取最高的那一处说「这一档会发生什么」——五处分开列太啰嗦，
        # 而演员需要的正是最强的那个信号。
        top_k = max(DEV_KEYS, key=lambda k: int(dev.get(k) or 0))
        top_v = int(dev.get(top_k) or 0)
        tp = str(dev_level(top_v, cfg).get("prompt") or "").strip()
        if tp and top_v >= 100:
            lines += ["", "（拿%s来说：%s）" % (DEV_LABELS.get(top_k, top_k), tp)]
        if res.get("devDelta"):
            lines += ["", "（这一轮她的身体又往那边走了一步。写的时候要让人看出来"
                          "和上一轮不一样——不是靠她说，是靠那一处的反应变快了。）"]
        if res.get("devAllFull"):
            # 五处全满 = 身体开发 100%。这是攻略模式压过自由模式最直接的一段：
            # 没有数值体系的地方，演不出「碰一下就不行了」这件事。
            lines += ["", "（**她身上已经没有一处是没被开发过的了。** 这一档碰一下就会"
                          "起反应：皮肤先热，水自己出来，腰跟着手走。她会主动——"
                          "不是装出来的主动，是被身体推着走，自己都拦不住。"
                          "写的时候让这个「一碰就溃」的落差被看见：她嘴上还可以骂你，"
                          "身体已经先答应了。）"]
        elif dev_stage(top_v, cfg) >= 3:
            lines += ["", "（这一档她会在**不相干的时候发起作来**：看到别人亲近、"
                          "闻到你的味道、坐得久了一点，都能让她当场软掉。"
                          "这种勾子不用等他先动手，她自己会找个由头凑过来。）"]
        # 编剧给的勾子（2026-11-05）：这一轮她身体先失控的那个瞬间。
        # 直接当成「这一拍要写的事」交给演员——不是建议，是素材。
        hook = str(res.get("devHook") or "").strip()
        if hook:
            lines += ["", "（这一拍她的身体先了一步：%s。写出来，别总结成"
                          "「她有点动情」——要写她在什么场合、身体怎么先背叛、"
                          "她怎么试图掩饰或者掩饰不住。）" % hook]

    # ── 戒备（2026-11-05，模糊档，不报数字）────────────────
    # 玩家在面板上看到的是档名（犯嘀咕/起疑/戒备），模型看到的这句也一样
    # 不带数字——「她开始琢磨你图什么」比「戒备 23」更像一个人。
    # 降价台词要和好感档位的文案叠着读：戒备是「他图什么」，好感是「她烦不烦他」，
    # 两件事可以同时成立。
    su_v = int(res.get("suspicion") or 0)
    su_d = int(res.get("suspicionDelta") or 0)
    if su_v >= 30:
        lines += ["", "（她现在对你有明显的戒备：你的好她接着，但心里一直挂着「他到底想干什么」。"
                      "眼神会躲、话题会岔、身体接触会退。这时候他说什么她都先过一遍脑子——"
                      "他得用**行动**给她一个说法，说破嘴皮子没用。）"]
    elif su_v >= 20:
        lines += ["", "（她开始琢磨你对她的好到底图什么了。对你的投入她会迟疑着收下，"
                      "收下之后反而不自在。她想听你亲口把事情说清楚——或者看你做不做得到。）"]
    elif su_v >= 10:
        lines += ["", "（她有点在意你为什么对她这么好。没到反感的程度，甚至有点受用——"
                      "但那点「无事献殷勤」的嘀咕压不下去了，问出来的话会带刺。）"]
    if su_d >= 8:
        lines += ["", "（你这轮的做法让她心里那根弦绷紧了一截。她会记住这件事——"
                      "下次再这样，就不是嘀咕了。）"]
    elif su_d <= -8:
        lines += ["", "（你这一轮把她的戒备卸下去了一点。她自己也说不清为什么松了口气。）"]

    if res.get("rose"):
        lines += ["", "（你们的关系刚刚往前跨了一步，到了【%s】。这一步要有实感——"
                      "她的某个反应、某句话，得和之前不一样。）"
                  % (res.get("levelName") or "")]
    if res.get("changed"):
        lines += ["", "（你们的关系刚刚跨进【%s】这一档——这一轮开始，"
                      "她对你的态度要和之前**明显不一样**，转折要自然。）"
                  % (res.get("tierName") or "")]
    reason = str(res.get("reason") or "").strip()
    applied = int(res.get("applied") or 0)
    if applied and reason:
        lines += ["", "（刚才那件事：%s。）" % reason]

    # 压迫模式的进度（2026-11-05）：**给玩家看的那句**不走这里，这里只
    # 告诉演员「这一条路还没关」。跨一档堕落，门槛降一次——她越是被弄成
    # 这样，下一次越容易，这是这套玩法能反复玩下去的核心。
    if co > 0:
        fp = forced_progress(co, ar, cfg)
        if fp["on"]:
            stage_word = ("第一次" if fp["stage"] == 0
                          else "已经走过 %d 次" % fp["stage"])
            lines += ["", "（她能被你推到这一步，%s。堕落每往上走一档，"
                          "下一回需要的气氛就少一截——这个门槛不该被说出来，"
                          "但你的演法该让它自己显出来。）" % stage_word]
    return "\n".join(lines)


def tail_line(res):
    """QQ 回复尾部追加的状态块。

    2026-11-04 早先的版本**不报数字**（怕玩家算账）；当天晚些时候用户拍板
    反过来要数字：「看到数值浮动才有成就感」。所以数值**一行一个**，
    正数不带加号、负数才带负号。

    格式（当日四次修订）：`第 3 级 / 共 5 级` 也是行话——玩家不懂「级」是量什么，
    要的是能直接读懂的词。关系行只留**关系叫什么**；第四次修订（同日）把
    「好感 21」换成 **`亲密 21/30 · 还差 9`**——用户报「看不出怎么触发后续
    剧情」，只给档名不够，得让「离下一档多远」直接读得出来：

        ❤ 关系 家里的人
        气氛 平静
        亲密 21/30 · 还差 9
        冲动 0

    越界那一轮气氛让位给「她退开了」——那轮的信号是「她退开了」，不是气氛；
    数字照给，因为这一轮的掉分正是要看的东西。
    """
    if not isinstance(res, dict) or not res.get("active"):
        return ""
    ending = str(res.get("ending") or "")
    if ending:
        # 只剩进局子一种结局了（毕业 2026-11-04 晚退休）；老档若还留着
        # graduated，照样给个标记，别让它变成「❤ 已结局【】」这种空壳。
        mark = {"graduated": "🎓", ENDING_JAIL: "🚨"}.get(ending, "❤")
        name = (ending_cfg(ending, load_config()) or {}).get("name") or "结局"
        return "%s 已结局【%s】" % (mark, name)
    lname = res.get("levelName") or level_name(int(res.get("level") or 0)) or "?"
    head = "❤ 关系 %s" % lname
    if res.get("rose"):
        head += " ↑"
    # 第二行：气氛。越界那一轮不是重点——那轮要说的是「她退开了」。
    if res.get("cross") == "hard":
        mood = "她退开了"
    else:
        ar = arousal_name(res.get("arousal") or 0)
        mood = ("气氛 " + ar) if ar else ""
    if int(res.get("crossN") or 0) > 0:
        part = "被拒 %d 次" % int(res.get("crossN") or 0)
        mood = (mood + " · " + part) if mood else part
    rows = [head]
    if mood:
        rows.append(mood)
    g = res.get("intimacy") or intimacy_gauge(
        res.get("level"), res.get("value"))
    rows.append("亲密 " + str(g.get("label") or int(res.get("value") or 0)))
    rows.append("冲动 %d" % int(res.get("arousal") or 0))
    # 高潮值（2026-11-04 深夜，原「兴奋」）：**只在**场景内出现。
    # 没在进行时这一行不该有——它不是常驻状态条，是「这一场里她到哪了」。
    # 2026-11-05：取消 5 次封顶，次数只报「第 N 次」，不再报分母。
    ex = int(res.get("excite") or 0)
    if ex > 0:
        n = int(res.get("orgasms") or 0)
        rows.append("高潮 %d · 第 %d 次" % (ex, n))
    # 堕落 / 屈服（2026-11-04 深夜）：常驻两行。堕落**只在动了之后**才出现——
    # 「堕落 0」天天挂着像在提醒玩家「你还没下手」，那是压力不是信息。
    co = int(res.get("corrupt") or 0)
    if co > 0:
        rows.append("堕落 %d · %s" % (co, res.get("corruptName") or ""))
        # 压迫模式的进度（2026-11-05）：玩家要看得见「再推几次就降门槛了」，
        # 否则那套递减对他是隐藏机制，等于没用。
        fp = forced_progress(co, res.get("arousal") or 0)
        if fp["on"]:
            if fp["gap"] > 0:
                rows.append("压迫 差 %d 冲动 · 已降 %d 档" % (fp["gap"], fp["stage"]))
            else:
                rows.append("压迫 现在就能开 · 已降 %d 档" % fp["stage"])
    su = int(res.get("submit") or 0)
    if su > 0:
        rows.append("屈服 %d · %s" % (su, res.get("submitName") or ""))
    # 开发度（2026-11-05）：五处被开发过的才报，一行报全，末尾挂总进度
    # 「开发 62%」。全满时单独说一句——那是攻略模式最吃的一段数值。
    dev = res.get("dev") if isinstance(res.get("dev"), dict) else {}
    dev_names = res.get("devNames") if isinstance(res.get("devNames"), dict) else {}
    bits = []
    for k in DEV_KEYS:
        v = int(dev.get(k) or 0)
        if v > 0:
            bits.append("%s %d" % (DEV_LABELS.get(k, k), v))
    if bits:
        mx = int(res.get("devMax") or dev_cfg()["max"] * len(DEV_KEYS))
        pct = int(res.get("devPct") or 0)
        rows.append("开发 " + " · ".join(bits)
                    + "（%d%% / %d）" % (pct, mx))
        if res.get("devAllFull"):
            rows.append("开发满了 · 碰一下就溃")
    return "\n".join(rows)


def detail_nodes(line_key):
    """「好感」指令的聊天记录卡片：档案页 + 最近变化页。没开过返回 None。"""
    ent = _entry(line_key)
    rec = record_of(ent)
    if not rec:
        return None
    from app import persona
    cfg = load_config()
    card = persona.active_card(line_key) or {}
    name = card.get("name") or "她"
    value = int(rec.get("value") or 0)
    mode = str(rec.get("mode") or MODE_FREE)
    ending = str(rec.get("ending") or "")

    head = ["❤ 攻略档案 · %s" % name, ""]
    if mode == MODE_FREE:
        head.append("状态：自由模式（进度封存中）")
    elif ending:
        head.append("状态：已结局【%s】" % (ending_cfg(ending, cfg).get("name") or ""))
    else:
        head.append("状态：攻略中")
    lv = int(rec.get("level") or 0)
    # 只写关系叫什么（2026-11-04 三次修订：「第 3 级 / 共 5 级」是行话，
    # 玩家看不懂级是在量什么。叫法本身就会随关系往前而变）。
    # 2026-11-04 深夜：级名改成身体进度（能抱了 / 亲过了 / 做过了），
    # 这一行现在直接回答「我们能到哪一步」。
    head.append("你们：%s" % (level_name(lv, cfg, card) or "?"))
    head.append("她对你的感觉：%s"
                % (_tier_view(tier_for(value, cfg), lv).get("name") or "?"))
    # 气氛（2026-11-04）：冲动是一次性的，查档案这一刻看到的就是「此刻」。
    # 卡片第一页给词，第二页不重复——重复就变成刷状态了。
    # 深夜改：这行改成「她现在有多想」，因为冲动值的新语义就是「想不想做」。
    ar = int(rec.get("arousal") or 0)
    an = arousal_name(ar, cfg)
    if an:
        head.append("此刻的气氛：%s（%d）" % (an, ar))
    # 卡在哪一步要说清楚：两条门槛都过才进得去（2026-11-04 深夜）
    dv, da = _need_to_cross(value, lv, ar, cfg)
    if not ending and lv < LEVEL_MAX and (dv or da):
        need = []
        if dv:
            need.append("感情还差 %d" % dv)
        if da:
            need.append("气氛还差 %d" % da)
        head.append("再往前一步：「%s」——%s"
                    % (level_name(lv + 1, cfg, card) or "下一级", "、".join(need)))
    if int(rec.get("cross_n") or 0) > 0:
        head.append("越界被拒：%d 次" % int(rec.get("cross_n") or 0))
    if not ending:
        # 2026-11-04 第四次修订：原来只报「好感 21」，看不出还差多远。
        # 现在和尾行同一句（`亲密 21/30 · 还差 9`），下面那句解释照旧——
        # 分数只是门票，升一级还得有一件能让她愿意往前走的事。
        head.append("亲密 %s"
                    % intimacy_gauge(lv, value, card, cfg).get("label", value))
        if lv < LEVEL_MAX:
            head += ["", "再往前一步不靠攒分——得有一件能让她愿意往前走的事。"]
        else:
            head += ["", "她已经不设防了。剩下的只看这一刻的气氛。"]
    page1 = "\n".join(head)

    hist = rec.get("hist") if isinstance(rec.get("hist"), list) else []
    rows = ["📋 最近的变化", ""]
    if hist:
        for h in reversed(hist[-6:]):
            rows.append("%s  %+d  %s" % (h.get("ts") or "", int(h.get("d") or 0),
                                         h.get("r") or ""))
    else:
        rows.append("还没有记录，多陪她聊聊？")
    # 成就（2026-11-04）：只在解到过东西时追问一句，没解到不提——
    # 每次都写「成就 0/12」等于提醒玩家「你还没解锁」，是负担不是奖励。
    got = [a for a in (rec.get("ach") or []) if isinstance(a, dict)]
    if got:
        from app import achievements
        rows += ["", "🏅 成就 %d 个（发「成就」看全部）" % len(got)]
        for a in got[-3:]:
            # 名字要跟着这张卡的叫法走——同名成就换张卡就不一样了
            rows.append("%s %s" % (achievements.icon(a.get("k")),
                                   achievements.name(a.get("k"), cfg, card, lv)))
    page2 = "\n".join(rows)
    return [page1, page2]


def detail_text(line_key):
    """「好感」指令的纯文本兜底（协议端发不了卡片时用）。没开过返回 None。"""
    nodes = detail_nodes(line_key)
    if nodes is None:
        return None
    return "\n\n".join(nodes)


def achievement_nodes(line_key):
    """「成就」指令的卡片。没开过攻略返回 None（当普通消息）。"""
    rec = record_of(_entry(line_key))
    if not rec:
        return None
    from app import achievements
    return [achievements.nodes(rec, load_config(), _card_of(line_key))]


def achievement_text(line_key):
    """「成就」的纯文本兜底。"""
    nodes = achievement_nodes(line_key)
    if nodes is None:
        return None
    return "\n\n".join(nodes)

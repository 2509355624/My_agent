# -*- coding: utf-8 -*-
"""写作规范切片（2026-11-05，第一步）。

`skills/writing/` 是给长篇小说用的规范库，一共约 30 万字符。**整段塞进每轮
提示词是不可能的**：上下文会直接被打满，模型还会抓不住重点（它一次能用的
也就两三千字）。所以这里只做一件事：

    按「她此刻的状态」挑**一小段**规范原文，压到 1,400 字以内，交给演员层。

## 为什么不用向量库

`writing_18`（59 条范文）是范文，`skills/writing/` 是规则表格。规则表格
切块入库之后，「### ② 器官直呼，温度阶梯」这种标题会和正文被切散，检索
回来的是一张缺了表头的表。所以这里**按文件 + 起止标记硬切**：切点是人写
的小标题，语义天然完整，也不会出现「查到一半」的碎片。

## 分两层

- `_CORE`：只要判成亲密就拼，永远同一份（器官用词 + 动词 + 声音 + 身体信号）。
  这是「怎么说」，不分状态。
- `_BY_AXIS`：按当前哪根轴最高挑**一段**。这是「这一拍该往哪儿使劲」。

两段合起来再压到 `MAX_CHARS`。宁短勿全：一段 400 字的真规范，比三段各
400 字的规范有用——后者的注意力被摊平了。

## 和 `persona.intimate_guide()` 的分工

| 层 | 谁写的 | 管什么 |
|----|--------|--------|
| `_INTIMATE_GUIDE` | 代码里写死 | 保底：一分规范都没命中时也够用 |
| `intimate_guide()` | 按高潮值档位 | 这一拍**走到哪一步**（还没起来 / 临界 / 到了） |
| 本模块 | 按数值轴挑规范原文 | 这一拍**该怎么写才够劲**（用词、动词、反差） |

三者不重复：前者管「剧情节奏」，本模块管「笔法」。叠一起是有意的——
节奏和笔法本来就得同时到位。
"""

import logging
import os

log = logging.getLogger(__name__)

# 规范库根目录。和 app/config.py 的 BASE_DIR 同一个锚，别各自写一份。
_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skills", "writing")

# 拼进提示词的总字数上界。为什么是 1400：一段亲密场景单轮回复通常在
# 800~1200 字，规范超过这个量级，模型就会开始「照着规范写」而不是
# 「照着人写」——表现是正文里冒出指南腔。
MAX_CHARS = 1400
# 单段上限。挑两段的时候各压到 700，比一段 1400 更平衡。
SLICE_MAX = 720
# 分轴段能占的总预算比例。剩下的是常驻层（用词纪律 + 动词），
# 这部分哪根轴都要用，所以不能被轴段挤没。
AXIS_SHARE = 0.62
# 压迫模式的总上限（2026-11-05）：那一场要「权力语言 + 许可制度 + 身体诚实」
# 三份同时到位，1400 字按 62% 分给轴段只够塞一份半。这里放到 1800，
# 依然远小于 `_INTIMATE_GUIDE` 本身（约 1900 字），不至于把历史挤掉。
FORCED_CHARS = 1800
# 开发度轴（2026-11-05）：五处里最高的一处到这个值就切到 `dev` 轴。
# 取 300（「离不开」）——那一档开始她会在不相干的场合自己发作，
# 那才是这套轴真正有别于其他轴的写法。
DEV_AXIS_AT = 40
_DEV_KEYS = ("mouth", "nipple", "pussy", "anus", "skin")

_H_WRITING = "02-h-tech/h-writing.md"
_CONQUEST = "05-style/conquest-style.md"
_DOMINATION = "01-structure/references/domination-training-techniques.md"
_QUIET = "01-structure/references/quiet-exposure-techniques.md"
_CONTENT = "03-content/成人情色写作指导.md"
_TECH = "04-liveness/refs-打磨/techniques.md"

# 高潮值到这个档位（rising，55+）才认为「已经进入」。
# 之前用涩气层：停、差一点、身体泄露、环境见证；之后才切到器官/动作的
# 露骨规则。直接在 low 档塞用词表，模型会把前戏写成正戏——这是 2026-11-05
# 用户反馈「太直白、没意思」的主要来源。
EXPLICIT_AT = 55

# ── 常驻层：判成亲密就拼 ────────────────────────────────
# (名字, 文件, 起始标记, 结束标记, 截到哪个标记为止)
# 第五项 `until`：规范库是**给人看的**，有些段尾挂着「用户裁决」「生成前
# 内部规划」这类作者笔记。原样塞进提示词，模型会把它当成指令照做
# （实测会把「用户裁决删除『逼』」读成一条禁令）。所以切片从 `until`
# 那里就断，笔记不进正文。
_CORE = (
    ("用词纪律", _H_WRITING, "## 【用词纪律】", "## 深度范文库",
     "**用户裁决**"),
    ("动词", _H_WRITING, "### 2.1 强动词替换", "### 2.3 感官描写要求", None),
)

# ── 涩气层：还没到露骨阶段时拼这一段 ─────────────────────
#
# 素材来自 style-book 的②③⑤⑥、quiet-exposure 的「差一步」、techniques 的
# 情绪体感化/感官偏见、以及 03-content 的对象感/每段变化。这里压成一段
# 可直接执行的短规则：**先写欲望怎么被藏住，不写器官。**
_SULTRY_GUIDE = (
    "### 笔法：先写「差一点」，别急着写「做了什么」\n"
    "\n"
    "（这一拍以暗示为主：上面那套器官/动作的露骨用词规则**先不要用**；"
    "等真正进入之后再切过去。）\n"
    "\n"
    "- 一场只抓一个「不能 / 不该 / 差一点」（门没锁、外面有人、她还在生气、"
    "你马上要走），每段都让它发挥作用。\n"
    "- 还没到那一步，就别写器官。写距离、衣物、呼吸、声音、温度、物件；"
    "允许程度变了，再升级用词。\n"
    "- 每段至少一个「停」：手停在衣领前一厘米、扣子解一颗又停、吻到嘴角移开、"
    "话说一半。**没发生的事比发生了的更涩。**\n"
    "- 情绪不写判断，写身体泄露：耳根、指节、膝盖、杯壁上的水珠、衣料摩擦。\n"
    "- 环境当见证：外面那个正常世界越具体，室内越失控。\n"
    "- 三轨分开，而且不要一致：叙述只写看得见的；她说的话放「」；"
    "她没说出口的那一句单独一行、第一人称、短。嘴上说「不用」，手没抽回去，"
    "心里是「再近一点」——色气就在这条缝里。\n"
    "- 结尾留未完成：一个画面、一个没做完的动作，不总结、不升华。\n"
)

# 规范库里的**内部笔记行**：作者裁决、给作者看的检查项、生成前规划。
# 这些不是写法，塞进提示词模型会当成指令（实测把「用户裁决删除『逼』」
# 读成一条禁令）。切片前先按行剔掉。
_NOTE_MARKS = (
    "### 原则（18-2）",
    "（已按用户裁决定稿）",
    "**用户裁决**",
    "生成前内部规划",
    "**版本**",
    "**更新**",
)


def _drop_notes(text):
    """剔掉规范库里的内部笔记行，保留正文。"""
    keep = []
    for ln in text.split("\n"):
        if any(m in ln for m in _NOTE_MARKS):
            continue
        keep.append(ln)
    # 连续空行压成一个，别在剔完的地方留一片空白
    out, blank = [], False
    for ln in keep:
        if not ln.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    return "\n".join(out).strip()


# ── 分轴层：挑最高的一根轴 ───────────────────────────────
# 键和 `stateBehavior` 的四个轴一一对应。多给一个 "intimate" 键：四根轴
# 都不高但已经在场景里时用，管「怎么把肉感写出来」。
_BY_AXIS = {
    "corrupt": (
        ("嘴硬身体诚实", _CONQUEST, "### ④ 强制与崩坏并重", "### ⑥ 高潮链写足", None),
        ("器官温度阶梯", _CONQUEST, "### ② 器官直呼，温度阶梯", "### ③ 对白压迫",
         None),
    ),
    "submit": (
        ("权力语言", _DOMINATION, "## 二、权力语言体系", "## 三、高潮许可制度", None),
        ("许可与断崖", _DOMINATION, "## 三、高潮许可制度", "## 四、惩罚与奖励的循环",
         None),
    ),
    "suspicion": (
        ("捂不住，也躲不开", _DOMINATION, "## 六、被调教者的心理弧线",
         "## 七、公开/旁观场景的调教", None),
        ("嘴硬身体诚实", _CONQUEST, "### ④ 强制与崩坏并重", "### ⑥ 高潮链写足", None),
    ),
    "affection": (
        # 好感是**关系轴**，它自己不会让肉体戏变好看——它决定「她怎么看他」。
        # 所以这一段只挑「对白压迫」，另一半给器官阶梯：关系够深了，称谓
        # 就该跟着换档，这是 `_INTIMATE_GUIDE` 里那张表管不到的细处。
        ("对白压迫", _CONQUEST, "### ③ 对白压迫，命令与宣言", "### ④ 强制与崩坏并重",
         None),
        ("器官温度阶梯", _CONQUEST, "### ② 器官直呼，温度阶梯", "### ③ 对白压迫",
         None),
    ),
    "intimate": (
        ("声音与液体", _CONQUEST, "### ⑤ 声音 + 液体成对", "### ⑦ 破瓜见血", None),
        ("高潮控制", _H_WRITING, "### 4.4 高潮控制", "## 五、体位描写", None),
    ),
    # 压迫模式（2026-11-05）：低好感 + 高堕落 + 气氛在动。
    # 这三份材料合起来是同一件事：**权力被服从**——命令、许可、心理弧线。
    # 和 `corrupt` 那根轴的区别在「她愿不愿意」：那根轴默认她是被说动的，
    # 这一根默认她从没同意过，张力全在「她拦不住自己」。
    "forced": (
        ("权力语言", _DOMINATION, "## 二、权力语言体系", "## 三、高潮许可制度", None),
        ("许可与断崖", _DOMINATION, "## 三、高潮许可制度", "## 四、惩罚与奖励的循环",
         None),
        ("嘴硬身体诚实", _CONQUEST, "### ④ 强制与崩坏并重", "### ⑥ 高潮链写足", None),
    ),
    # 开发度（2026-11-05）：身体被开发透之后，戏的位置从床上挪到了日常里。
    # 这两份材料管的就是那种「不相干的地方突然发作」——车、饭桌、人群里。
    "dev": (
        ("差一步被发现", _QUIET, "## 二、暴露永远差一步（核心张力）",
         "## 四、主动设计露出（进阶手法）", None),
        ("公共场合", _H_WRITING, "## 九、特殊场景", "## 十、心理刺激层", None),
    ),
}

# 涩气层按轴挑的那一小段。这里只放**不靠器官、也不带「他」**的材料：
#   感官偏见 / 对象感 / 特殊场景。
# 停、身体泄露、环境见证已经全部写在 `_SULTRY_GUIDE` 里，不再重复切片。
# 同一根轴在 high 档（excite>=55）会走上面的 `_BY_AXIS`，换成露骨规则。
_SLOW_AXIS = {
    "suspicion": (
        ("感官偏见", _TECH, "### 第21招：感官偏见化（反通用正确）",
         "### 第22招：主角必须犯错（反全知视角）", None),
    ),
    "affection": (
        ("对象感", _CONTENT, "## 十、写出“对象感”",
         "## 十一、甜言蜜语与强烈动作可以并存", None),
    ),
    "dev": (
        ("特殊场景", _H_WRITING, "## 九、特殊场景", "## 十、心理刺激层", None),
    ),
}

# 全部标记都在这些文件里，启动时校一遍——改规范断了切片就早炸，
# 别等上线了才发现某根轴永远返回空串。
_CHECK = [(_H_WRITING, a, b) for _n, _f, a, b, _u in _CORE]
for _rows in _BY_AXIS.values():
    _CHECK += [(f, a, b) for _n, f, a, b, _u in _rows]
for _rows in _SLOW_AXIS.values():
    _CHECK += [(f, a, b) for _n, f, a, b, _u in _rows]


def _slice(rel, start, end, until=None):
    """切一段规范原文。找不到标记就返回空串（**不抛**，演员层没规范也能演）。"""
    path = os.path.join(_ROOT, *rel.split("/"))
    try:
        with open(path, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        log.warning("写作规范读取失败：%s", path)
        return ""
    i = txt.find(start)
    if i < 0:
        log.warning("写作规范切片标记缺失：%s → %r", rel, start)
        return ""
    j = txt.find(end, i + len(start))
    body = txt[i:(j if j > 0 else len(txt))]
    if until:
        k = body.find(until)
        if k > 0:
            body = body[:k]
    body = _drop_notes(body)
    if len(body) > SLICE_MAX:
        # 按行截，别把一句话砍成半截
        body = body[:SLICE_MAX].rsplit("\n", 1)[0] + "\n…"
    return body.strip()


def _top_axis(aff_res):
    """挑这一拍最该使劲的那根轴。四根都不高就返回 "intimate"。

    **只比大小、不比阈值**：谁最高就用谁。多给一条 `corrupt` 的加成是
    因为它是唯一「只涨不掉」的轴，一旦起来了就是这场戏的主旋律，
    和屈服/戒备同分时它该先说话。

    **压迫模式优先**（2026-11-05）：`affection.injection_block()` 判出这一场
    是压迫戏时，会在 res 里标 `forced=True`。那时候不管哪根轴最高，都走
    `forced` 那一套——压力戏的写法不是「堕落轴的高配版」，是另一套材料。

    **开发度优先**（2026-11-05，仅次于压迫）：五处里任何一处越过
    `DEV_AXIS_AT`，说明她的身体已经不受控到能在日常里发作，这一拍该写的是
    「差一步被发现」那种张力，不是床上那套。压迫场里开发度让位——那是
    另一条主轴，两套叠在一起会互相稀释。
    """
    if aff_res.get("forced"):
        return "forced"
    dev = aff_res.get("dev") if isinstance(aff_res.get("dev"), dict) else {}
    try:
        dev_top = max([int(dev.get(k) or 0) for k in _DEV_KEYS] or [0])
    except (TypeError, ValueError):
        dev_top = 0
    if dev_top >= DEV_AXIS_AT:
        return "dev"

    def n(k):
        try:
            return int(aff_res.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    co = n("corrupt")
    su = n("submit")
    # 戒备满值只有 40，按比例提到和另两根同一量级再比
    sus = n("suspicion") * 2
    # 好感在 aff_res 里叫 `value`（可以是负数，负分档不算「关系好」）
    ar = max(0, n("value"))
    if co <= 0 and su <= 0 and sus <= 0 and ar < 50:
        return "intimate"
    scores = {"corrupt": co + 8, "submit": su, "affection": ar, "suspicion": sus}
    return max(scores, key=lambda k: scores[k])


def _explicit_stage(aff_res):
    """这一拍已经进入正戏了吗？

    高潮值 < 55（cold / awake）时先用涩气层：停、差一点、身体泄露、环境见证。
    55+（rising / edge / over）才切到器官/动作/高潮的露骨规则。
    """
    try:
        ex = int(aff_res.get("excite") or 0)
    except (TypeError, ValueError):
        ex = 0
    return ex >= EXPLICIT_AT


def guide(aff_res=None, chars=MAX_CHARS):
    """按状态拼一段「这一拍该怎么写」的规范原文。拿不到状态就返回空串。

    `aff_res` 是 `affection.settle()` 的返回值。不是攻略模式（自由模式）
    时它是 `{"active": False}` 或者 None，这时整段不拼——自由模式没有轴，
    挑轴就是瞎猜。

    **两段式（2026-11-05）**：
      · 前中期（excite < 55）→ 涩气层 + 一根非露骨轴段；
      · 正戏期（excite >= 55）→ 原来的露骨层（用词纪律/强动词 + 轴段）。

    预算按**拼进提示词后的整段长度**算（含小标题和说明句），不是只算
    规范原文的长度。只算原文的话，标题一加就超，`…` 会出现在尾巴上。
    """
    if not isinstance(aff_res, dict) or not aff_res.get("active"):
        return ""
    # **没进场景、轴也没动** → 这一拍没有该写的笔法。原来的语义（2026-11-04）：
    # 高潮值 0 不是「还没被带起来」，是「根本没开始」；四根轴都是 0 时
    # 也不该凭空给一段肉戏指南，那只会让演员把戏往床上拽。
    # 轴只要有一根起来了（堕落/屈服/戒备/好感），就算不在场景里也拼——
    # 那时候她这一拍的状态本身就值得细写。
    def _n(k):
        try:
            return int(aff_res.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    in_scene = _n("excite") > 0
    has_axis = (_n("corrupt") or _n("submit") or _n("suspicion")
                or max(0, _n("value")) >= 50)
    if not in_scene and not has_axis:
        return ""
    # 轴段先占位，再填常驻层——不然常驻层的器官表会把轴段挤没，等于白挑。
    # 压迫模式另给一份更大的预算表（2026-11-05）：那一场要三份材料同时到位，
    # 三份各分一点，总字数得跟上；不然轴段被截成半张表，比不给还差。
    axis = _top_axis(aff_res)
    if axis == "forced":
        chars = max(chars, FORCED_CHARS)
    explicit = _explicit_stage(aff_res)
    rows = _BY_AXIS.get(axis, ()) if explicit else _SLOW_AXIS.get(axis, ())
    head = "\n## 这一拍的笔法（从写作规范里挑的，**学写法和用词，不要抄情节**）\n\n"
    tail = "\n\n（上面是笔法参考。她的性格和这条线怎么走，以人设和前面的状态为准。）"
    budget = chars - len(head) - len(tail)
    if budget <= 0:
        return ""

    parts, used = [], 0
    if not explicit:
        # 涩气层先占位：它管「这一拍怎么藏住欲望」，轴段只是补充。
        parts.append(_SULTRY_GUIDE)
        used = len(_SULTRY_GUIDE)

    axis_parts = []
    for name, rel, a, b, u in rows:
        if used >= budget * AXIS_SHARE:
            break
        s = _slice(rel, a, b, u)
        block = "### 规范·%s（她现在的状态最吃这一口）\n%s" % (name, s) if s else ""
        if block and used + len(block) + 2 <= budget:
            axis_parts.append(block)
            used += len(block) + 2

    core_parts = []
    if explicit:
        for name, rel, a, b, u in _CORE:
            if used >= budget:
                break
            s = _slice(rel, a, b, u)
            block = "### 规范·%s\n%s" % (name, s) if s else ""
            if block and used + len(block) + 2 <= budget:
                core_parts.append(block)
                used += len(block) + 2

    parts += core_parts + axis_parts
    if not parts:
        return ""
    return head + "\n\n".join(parts) + tail

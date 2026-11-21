# -*- coding: utf-8 -*-
r"""双 Agent 链路实测：编剧 AI → MOSS 编译 AI → 3 页 Layout JSON。

用法：
    D:\AI\confyui_env\Scripts\python.exe D:\AI\agent_my_test\tools\test_moss_two_agent.py

产物落在 tools/moss_two_agent_out/：
    script.md      编剧产出的分幕剧本 + 英文台账
    page1-3.json   每页 Layout JSON（可直接喂 MOSS 出图）
    check.txt      代码闸门校验结果
    usage.json     每步 token 用量

设计要点（和主对话里定下的口径一致）：
1. 编剧只写到「幕级」，不许写格子数——格级演绎归 MOSS 侧。
2. 台账（cast / outfit / state）全英文，因为要拼进 Layout JSON 的 caption。
3. 每幕 MOSS 是一次**无状态调用**：固定前缀=skill 全文+契约（可被服务端缓存），
   变的是 user 里的「该幕剧本 + 前情摘要 + 上一幕状态」。
4. 一致性靠代码闸门校验，不靠 LLM 读六万字 JSON 自查。
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

ROOT = r"D:\AI\agent_my_test"
SKILL_PATH = os.path.join(ROOT, "skills", "moss-comic-script", "SKILL.md")
ENV_PATH = r"D:\AI\my_agent_restructure\.env"
OUT_DIR = os.path.join(ROOT, "tools", "moss_two_agent_out")

PAGES = 3


# ────────────────────────── 基础 ──────────────────────────
def load_env(path):
    """读 .env（只取需要的几个键，不打印任何 key）。"""
    d = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def skill_body(path):
    """去掉 frontmatter，返回 SKILL.md 正文。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


class LLM:
    """最小 DeepSeek 客户端：直连官方，不走系统代理。"""

    def __init__(self, base, key, model):
        self.url = base.rstrip("/") + "/chat/completions"
        self.key = key
        self.model = model
        self.usage = []

    def __call__(self, system, user, max_tokens=8000, temperature=0.7,
                 timeout=300, tag=""):
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
            # DeepSeek V4 系默认开思维链，会把 thinking token 也计入 max_tokens，
            # 结果 content 为空、finish_reason=length。这里显式关掉。
            "thinking": {"type": "disabled"},
        }
        t0 = time.time()
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            raise RuntimeError("HTTP %s: %s" % (e.code, detail))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        fin = (data.get("choices") or [{}])[0].get("finish_reason")
        reasoning = msg.get("reasoning_content") or ""
        u = data.get("usage") or {}
        rec = {
            "tag": tag, "seconds": round(time.time() - t0, 1),
            "prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens"),
            "cache_hit": u.get("prompt_cache_hit_tokens"),
            "finish": fin, "chars": len(content), "reasoning_chars": len(reasoning),
        }
        self.usage.append(rec)
        print("  [llm] %-12s %5.1fs  prompt=%s out=%s cache_hit=%s finish=%s 正文%d字 思考%d字"
              % (tag, rec["seconds"], rec["prompt"], rec["completion"],
                 rec["cache_hit"], fin, rec["chars"], rec["reasoning_chars"]))
        if fin == "length":
            print("  ⚠ 被 max_tokens 截断，需调大")
        return content


def pick_json(text):
    """从模型回复里抠出 JSON 对象（容忍 markdown 围栏与前后废话）。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"```\s*$", "", t).strip()
    a = t.find("{")
    if a == -1:
        raise ValueError("回复里没有 JSON 对象：" + t[:200])
    # 从第一个 { 开始取**一个完整对象**。模型偶尔连着给两版 JSON（先写一版、
    # 觉得不对又重写一版），用「第一个 { 到最后一个 }」会把两版拼在一起，
    # json.loads 必然报 Extra data —— 那不是模型的错，是解析太粗。
    obj, _end = json.JSONDecoder().raw_decode(t[a:])
    return obj


# ────────────────────── 剧本 AI 提示词 ──────────────────────
SCRIPT_SYSTEM = """你写条漫分镜稿。任务只有一个：把这一段写成**每一格都能直接照着画、每一句都值得读**的稿子。

稿子里有两条线，缺一不可：
- **叙事**：由旁白框承担。交代时间、地点、人物来历、发生了什么、人物心里在想什么。读者不靠台词也能读懂这一页在讲什么。
- **对话**：由气泡承担。写人物真正会说出口的话——语气、口癖、没说破的意思都在句子里。

**画面描述要写满，落进画面的文字框要短。** 这是两回事，别搞反：
- 给编译 AI 看的「画面：」——地点、时间、站位、动作接点，写得越足越好，它全靠这个画。
- 给读者看的文字框（台词 / 旁白）——**每框就一句短话**，十来个字说完一个意思就停，
  像参考图那样「我回来了ー」「等我做完作业就吃ー」「好ー」，一段一个框，各自分开。
  不要把三四件事揉进一个框、不要写成长长的一大段塞在格子角落——
  这种挤成一坨的大框正是要避免的。**短不是碎**：不要「啊——」「是…」这种没内容的语气字，
  每句都要有实在信息，只是信息一句一框、别贪多。

=== 格号 = 阅读顺序（从上到下、从左到右）===
格号就是**读者看这一页的先后顺序**，不是画面重要程度的排名。
格1 是读者第一眼看到的那一格（页面最上排最左），格2 是它右边那格；
这一排从左到右写完，再写下一排，每排都从左到右。
所以叙事也按这个顺序推进：先发生的事写进小号格，后发生的写进大号格。
**不许**把最抓人的大场面编成格1、把前因塞进格5——那会让读者倒着读。
一格里有好几句话时，也按从上到下的顺序：先说的写前面，后说的写后面。

但每一页怎么接下一页，要写进稿子里——不是另写一条「页尾钩子」笔记，
而是让**最后一格的画面本身就是没做完的那一下**，下一页第一格从那一下接着画。
页与页断不开，靠的是画面接画面，不是靠笔记提醒。

=== 一格写四样 ===

格1｜远景｜街角
画面：冬日傍晚，街灯刚亮，女孩背着包沿人行道走，呼出的白气留在身后；画面里只有她一个人。
台词：女「说好今天去的，你可别忘了。」
旁白：「她在那个街角等了很久。」
       「书包里装着他送的那本书。」

- 格头：`格号｜景别｜这一格的主体`。景别只用 远景 / 全景 / 中景 / 近景 / 特写 / 过肩。
- 画面：谁在画面里、在哪、朝向哪、手上有什么、正在做什么，
  **写成一个能照着画的完整句子，信息给足**（地点、时间、光、动作接点都写进去）。
  **第一人称 POV（这是硬约束，每一格都算）**：镜头就是男主自己的眼睛，画面里**只有女生**。
  绝不写 `1boy` / `boy` / 男孩 / 男人 / 他的脸、身体、衣服——写了就把男主画进画面了。
  男主需要入画时只写手：`pov hands` 或 `disembodied hand`（镜头外伸进来的一只手），
  并写清这只手在做什么（递东西 / 接过 / 扶住 / 敲桌）；不写「他的手」「男孩的手」。
  多女生同框时逐个写清每个女生的站位和朝向，各自气泡各指各人。
  位置要挂靠具体参照物（门内 / 门外 / 台阶上 / 铁门里侧 / 灶台左边），
  别只写「站在一边」「走过去」——参考物没写死，下一页就会把人画到另一边去。
  相邻两格人物的相对位置不许变，除非这一格写明他是怎么走过去的。
  两人同框时，**只写谁身上有随身物**；另一个人一个字都别提这件东西——
  写「没有包」和写「有包」一样会把它画出来。
  不写「气氛」「心情」「仿佛」「好像」「似乎」这类画不出来的词。
- 台词：写成 `说话人「说的话」`，说话人只写 女 / 男。
  **默认让画面里的女生自己说话**——参考图那种整页都是女生之间的对话就很好，
  不需要男主插话。男主的话**只有剧情真的需要时才写**一句，别每页都给他安排台词。
  **看谁在开口，不看话里提到了谁。** 真要写男主（镜头后面不出镜的「我」），标 `男「…」`；
  不能因为句中有个「她」就把女生的话标成男，也不能反过来。
  **标 `男` 只是给这句话定归属，绝不代表画面里要出现一个男人**——男主永远只有画外的手。
  男主的话出图时画成白边裸字（不进气泡），写在他说那句的格子下面；女生的话照常用气泡，
  写在她在的那格下面。**全页没有 `男「…」` 时，编译尾句不许提 boy，一个字都不许留。**
  **一句写完整的话，十来个字是常态**，人物要交代背景、讲经过、说心里话，
  就让他好好说完——「我低估了这座山，现在迷路了，手机也没电」这种就是好台词。
  **一句一个气泡**：每句落一个气泡，别把两三句揉进同一个气泡里。一格写 1~2 句就够，
  别把一句完整的话切碎成语气词，也别一句塞两件不相干的事。
  这一格真没人说话才写「台词：无」。
- 旁白：台词讲不出来、但读者需要知道的那一件事——时间、前因、人物来历、没说出口的念头。
  **一个文字框只放一句短话**（十来个字说完就停），用「」括起来；
  要交代两三件事就写成两三句、各占一格或各占一个框，**绝不把好几句堆进同一个白框**。
  参考图那种「标签」就是一句一行的小竖条，不是一大段。
  不需要叙事的格就不写这一行。
- 音效：风声 / 门铃 / 脚步 / 熊吼 / 手机震动。不需要就不写。
- **状态：每一页最后一格下面写一行**，格式固定 `状态：谁在哪 / 谁身上有什么 / 什么成了事实`。
  这是下一页的入场凭据：下一页第一格从这份状态接着写，人的位置不许换、
  随身物不许凭空多或少，除非这一页明确写了谁把东西交给谁、谁走进或走出。
  例：`状态：他在木屋里，手里拿着火把；熊在屋外；火堆还烧着。`、
     `状态：他在门外街上，背黑包；她在门里，空手；门推开一条缝，人还没出来。`
  只写事实，不写情绪、不写「气氛变了」。

=== 一页几格：默认一格，分镜只在必要时用 ===

**默认一页就是一格。** 整页一个画面：在场的人、各自站在哪、谁在说话、此刻在做什么，
一起画进这一个画面里，各人的话各指各人。多人同框是这种漫画最擅长的事——
两个人面对面说话、三个人围一张桌子，一个画面就能演完，不要因为「是漫画」就切格子。

只有这几种情况才拆成多格，其余一律单格：
- **两格**：一件事必须拆成「因 → 果」两个瞬间才看得懂（他递出钱包 → 钱包里是空的），
  或两人正面对峙、一问一答各占一格。
- **三格**：三个人接连说话，一人一格，气泡只指说话的那个人。
- **四格**：一段连着的分解动作（拿鸡蛋 → 敲壳 → 下锅 → 装盘），这是分镜真正该用的地方。
  一件事一格讲得完，就别动用四格。

**不要五格、六格。** 一格装不下，就写到下一页去，不要靠多切格子把页面排满。
格子是为了把一件事讲清楚，不是为了看起来「像漫画」。

用户或需求里写明了这一页要几格，就照那个数写，一格都不许少——
那是他已经想好的节奏，你只负责把每格写满，不替他删格、合并格。

多格时相邻两格的景别不要一样：远景 → 中景 → 近景 → 特写 是自然的走法；
连着两张同样大小的脸，读者看不出谁在变。

=== 页和页要接起来 ===

这是**一条连续的镜头**，不是几页各讲各的。上一页读到哪一瞬间，下一页就从那一瞬间往下走。

- **每页最后一格停在一个没做完的动作上**：手停在半空、话说到一半、刚要转身、
  门推开一条缝人还没进来、手机震了一下屏幕还没看。别写成一个已经收尾的句号
  （「两人挥手告别」「她走远了」「喝完了」都是收尾，不能当页尾）。
- **下一页第一格直接接住那个动作**：上一页手停在半空，这一页手就落下来；
  上一页话没说完，这一页把话说完。人物位置和朝向也接着上一页，不重新起头。
- **地点只往前走，不许折返**：人已经从店里出来了，后面就走在店外，
  不要再「走向那家店」；已经在街角了，就不要又回到街角。
  换地点要写清是怎么过去的（「她拉着我拐进街角」），不能凭空换。
- **上一页已经发生的事，下一页默认成立**，不要重演一遍（她已经蹦出来打过招呼，
  下一页不要再写一次她蹦出来）。

一个自检：把相邻两页的第一格和最后一格连起来读——如果中间要补一句「然后呢」才讲得通，
就是断了；如果上一页最后一格的动作在下一页第一格原样重演，也是断了。

=== 一整段 ===

用户给的是这一段要发生什么，不是一整部作品。
把这一段写完就行：不必从前情讲起，也不必替它收尾。

页数是上限，不是任务量。素材只够三页就写三页，不够就更少。
不要补头补尾：「放学回家 → 相遇 → 交谈 → 喝可可 → 告别」这种把一段摊成五个地点的写法，就是补出来的。

=== 角色与场景（英文，会被原样拼进出图 prompt）===

`## 角色` 这一节全英文，**只写女生，绝不写 boy / 1boy 那一行**：
- girl: 1girl, <外形串>

**这是第一人称 POV：镜头后面的人就是男主本人（也就是「我」）。** 画面里出现的所有人都是女生；
「我」不出镜、没有脸、没有身体、没有衣服，绝不能给「我」写 1boy 或任何男性外形。
我需要伸手或接东西时，画面里只写 `pov hands` / `disembodied hand`（镜头外伸进来的一只手），
不写「他的手」「男孩的手」。写 `1boy` / `boy` / 男人的脸或身体，就是事故——男主被拽进画面了。
多女主同框时每人各写一行（girl / girl2 / girl3），都带 pov。

用户需求里写明的外形词（金发、双马尾、绿眼、遮眼发、开衫、耳钉……）必须逐字出现在这一行，
一个词都不许删、不许换、不许因为「黑白页画不出颜色」改成明度词（blonde 不许写成 light）。
他没写的部分，用明度与材质补：dark / light / mid-grey / washed-out / patched / denim / linen。

`## 场景 1` 一句英文：地点 + 两三个全程不挪动的固定物件 + 光源从哪个方向来、软硬。
不写时间词（evening / night）、不写色温词（warm / golden）、不写氛围词（soft / dreamy）、不写镜头词。
换地点才写 `## 场景 2`，最多两个地点。

=== 输出格式（照这个结构，前后不要加解释）===

# 标题四到八个字
## 角色
- girl: 1girl, blonde twin tails, hair over one eye, green eyes, light linen blouse, mid-grey knit cardigan
## 场景 1
A winter street corner in front of a convenience store, a frosted glass door, a low metal railing, a street lamp above, hard light from the lamp.
## 页 1
场景：1
格1｜中景｜女孩站在路灯下
画面：傍晚便利店门口，女孩抱着纸袋站在路灯下偏头看向镜头这一侧，路灯光把她的影子拉在身后；画面里只有她一个人，镜头是男主自己的眼睛。
台词：女「你不是说请我吃饭吗？」
      女「电影票我买好了。」
旁白：「她攒了两周的零用钱。」
状态：女孩在便利店门外路灯下，抱着纸袋；两张票已经拿到她手里。
## 页 2
场景：1
格1｜特写｜递出的电影票
画面：镜头外伸进来一只手（pov hands），把一盒爆米花递到女孩面前；女孩低头看着，画面里除了这只手只有她一个人。
旁白：「爆米花是刚买的。」
格2｜近景｜女孩的脸
画面：女孩接过爆米花愣了一秒，随即捂嘴笑出声，肩膀微微发抖；背景是便利店的玻璃门。
台词：女「那说好了，下次你请。」
状态：女孩还在店门外，爆米花已经接到她手里；约好下次再来。
"""


# 用户 brief 里可能写明的外观词（英文 tag / 中文描述）。
# 抽出来单独列给编剧，明确说明「必须原样进台账」——否则它会为了配黑白画风
# 把 blonde 洗成 light、把 green eyes 洗成 light-grey eyes（2026-11-21 用户实测踩到）。
LOOK_TAG = re.compile(
    r"\b(\d+\s?girl|\d+\s?boy|blonde|golden|silver|platinum|white|black|brown|red|green|"
    r"blue|grey|gray|amber|purple|twin\s?-?tails|twintails|ponytail|braid|pigtails|"
    r"bob\s?cut|long\s?hair|short\s?hair|medium\s?hair|bangs|hair\s?over\s?one\s?eye|"
    r"single\s?eye\s?covered|glasses|freckles|scar|tan\s?skin|pale\s?skin|"
    r"school\s?uniform|blazer|cardigan|hoodie|sailor|ribbon|choker|earring|hair\s?tie|"
    r"skirt|trousers|jeans|sneakers|loafers|boots)\b", re.I)

LOOK_TAG_CN = ("金发", "银发", "白发", "黑发", "棕发", "亚麻", "双马尾", "单马尾",
               "马尾", "长发", "短发", "齐肩", "刘海", "遮眼", "绿眼", "蓝眼", "红眼",
               "金瞳", "碧眼", "灰眼", "眼镜", "校服", "耳钉", "耳环", "发绳", "发带",
               "开衫", "卫衣", "百褶裙", "长裤", "运动鞋")


def _brief_look_tags(brief):
    """抽出用户在 brief 里写明的外观词（英文 tag + 中文描述）。"""
    out = []
    for m in LOOK_TAG.finditer(brief or ""):
        t = re.sub(r"\s+", " ", m.group(0).lower()).strip()
        if t not in out:
            out.append(t)
    cn = [w for w in LOOK_TAG_CN if w in (brief or "")]
    # 「马尾」是「双马尾」的子串，只留长的那个，别在清单里重复一遍
    cn = [w for w in cn if not any(w != o and w in o for o in cn)]
    for w in cn:
        if w not in out:
            out.append(w)
    return out


def build_script_user(brief, pages):
    """只给人给的那句话和页数上限。

    2026-11-21 删掉了原来注入的 3 幕骨架（`FRAME`）：用户跑 10 幕时反馈「各讲各的、
    看不懂」——根因就是这段写死的事件顺序（我有喜欢的人 → 你猜 → 是你）每页都被
    塞进去。幕数一多，前 3 幕被它绑架、后 7 幕自由发挥，前后必然断层。
    编剧该自己从一句话里长出整条因果链，不给它现成剧情。

    同日补：把 brief 里的外观词单独列一条「必须逐字进台账」。用户明明写了金发，
    出来的台账是 light-light twin tails——不是它没看见，是它按「黑白页不能有颜色」
    把用户的指令翻译掉了。这条清单把优先级摆明。

    2026-11-21 二次改（A 层）：页数从「任务量」改成「上限」。
    原来写「一共 N 幕 = N 页」，逼它把一段素材摊成 N 个等长小节——「偶遇→喝可可」
    这种片段被摊成「放学回家→相遇→交谈→喝可可→告别」，每页都成立，整段没有一件事
    真的发生。现在只说上限，并要求它先出节拍表、由节拍定页数；不够就少写页。
    """
    tags = _brief_look_tags(brief)
    look = ""
    if tags:
        look = ("\n\n用户写明的外观词（**必须逐字出现在角色台账里**）：\n"
                + ", ".join(tags) + "\n"
                "这些是他的指令，不是参考。这些词要逐字留在 `girl:` 那一行，"
                "不许删、不许换成明度词、不许翻译成黑白。"
                "**角色台账只写女生、不写 boy/1boy——这是第一人称 POV，"
                "画面里只能有女生，男主是镜头后的人、不出镜。**")
    return """需求（人给的，只有这些）：

%s%s

    把这一段写成 %d 页以内的分镜稿。**%d 页是上限，素材不够就少写页。**

    每一格照这个格式写：`格K｜景别｜主体`，下面接 `画面：` / `台词：` / `旁白：` / `音效：`。
    **台词写完整的话，一句十来个字是常态；旁白承担叙事，一格可以写两三句**——
    要让人读下来知道时间地点、发生了什么、人物在想什么，不要省字。
    每页最后一格下面再写一行 `状态：谁在哪 / 谁身上有什么 / 什么成了事实`。

    稿子就是角色台账、场景台账和逐格稿这三样，按格式写满即可。
    **第一人称 POV（硬约束）**：男主是镜头后的人、就是「我」，画面里只能有女生，
    角色台账只写 girl 行、绝不写 boy/1boy；我需要入画时只写 pov hands / disembodied hand（伸进来的一只手），
    绝不写男孩的脸、身体、衣服。男主说的话标 `男「…」` 只是定归属，画成画外白边裸字。
    **一页默认就一格**：一个画面把在场的人、站位、谁说什么都装进去，多女生同框就画在一格里。
    只有「因果两瞬间 / 三人接连发言 / 连着的分解动作」才拆成 2~4 格，不要五格六格。
    用户已经写明这一页几格的，照那个数写，不许删格合并格。

页和页要接成一条线：每页最后一格停在没做完的动作上，下一页第一格从那儿接着画；
人的相对位置和随身物照上一页的 `状态：` 继承，不许凭空换边或多出东西；
地点只往前走，已经发生过的事不要重演。""" % (
        brief, look, pages, pages)


# 旧的事件顺序骨架，已停用（见 build_script_user 的说明）。留着只为对照历史产物。
FRAME = """- 第 1 幕：她说起自己有喜欢的人了，男生愣住
- 第 2 幕：男生用玩笑的口吻试探是谁
- 第 3 幕：她说那个人其实是他"""


# ────────────────────── MOSS 编译契约 ──────────────────────
LAYOUT_TABLE = """L0 · 右通高主格 + 左栏三小 + 顶部条（5 格，日常默认）
  顶条(建场空镜)  [-768, -494, -692, 494]
  右通高主格      [-692, -262, 754, 494]
  左上小格        [-378, -494, 117, -272]
  左中小格        [130, -494, 540, -272]
  左下小格        [553, -494, 754, -272]

L1 · 左通高主格 + 右栏三小 + 顶部条（L0 镜像，5 格）
  顶条            [-768, -494, -692, 494]
  左通高主格      [-692, -494, 754, 262]
  右上小格        [-378, 272, 117, 494]
  右中小格        [130, 272, 540, 494]
  右下小格        [553, 272, 754, 494]

L2 · 上主格 + 下排三小（4 格，大全景开场）
  上主格          [-750, -494, 61, 494]
  左下小格        [74, -494, 750, -165]
  中下小格        [74, -165, 750, 165]
  右下小格        [74, 165, 750, 494]

L3 · 等权 2×3 六格（六步连续动作，无主次）
  上左 [-750, -494, -260, -7]   上右 [-750, 7, -260, 494]
  中左 [-246, -494, 244, -7]    中右 [-246, 7, 244, 494]
  下左 [258, -494, 748, -7]     下右 [258, 7, 748, 494]

L6 · 规整五格（5 格默认，格号严格 = 从上到下、从左到右）
  上左 [-750, -494, -8, -8]     上右 [-750, 8, -8, 494]
  下左 [8, -494, 750, -172]     下中 [8, -160, 750, 160]   下右 [8, 172, 750, 494]
  格1 上左 → 格2 上右 → 格3 下左 → 格4 下中 → 格5 下右

L4 · 三条横带（3 格，三视角并列，不做推进）
  上带 [-750, -494, -250, 494]
  中带 [-236, -494, 236, 494]
  下带 [250, -494, 750, 494]

L5 · 左通高 + 右上下二切（3 格，双人主次/对峙）
  左通高 [-750, -494, 750, -7]
  右上   [-750, 7, 61, 494]
  右下   [74, 7, 750, 494]

T3 · 上大横格 + 下两小（3 格，一页一个主画面 + 两个补充）
  上主格 [-768, -494, 96, 494]
  下左   [112, -494, 768, -8]
  下右   [112, 8, 768, 494]

S1 · 满版单格（1 格，一页一拍：一张脸、一双手、一个对视）
  整页   [-768, -494, 768, 494]

D1 · 上下二格（2 格，同一空间里前后两个动作：递出去 → 接住）
  上格   [-768, -494, -8, 494]
  下格   [8, -494, 768, 494]

D2 · 左右二格（2 格，双人对峙、左右并置）
  左格   [-768, -494, 768, -8]
  右格   [-768, 8, 768, 494]
"""

MOSS_CONTRACT = """

═══════════════════════════════════════════════
【本次任务契约 · 优先级高于上文任何示例】
═══════════════════════════════════════════════

你现在只做一件事：把「某一幕的剧本」编译成**一页** Layout JSON。

一、输出
- 只输出一个 JSON 对象，无 markdown 围栏、无解释文字。
- 字段固定：summary{meta, canvas, caption} / items[] / bbox_coord_space / bbox_format。

二、版式（必须从下表选一个，bbox 原样照抄，一个字符都不许改）
__LAYOUTS__

选法：**版式不是你来选，是这一页的拍数决定的**，上游已经指定好并写进用户消息，
你只负责原样照抄格子坐标。三件事要记住：

- **格号 = 阅读顺序**：格1 是读者第一眼看到的那一格（最上排最左），格2 是它右边那格；
  同一排从左到右数完，再数下一排。**bbox 数组里格子的先后就是阅读先后**，
  不许自己把某一格挪到前面、不许按画面大小重排；先发生的事在小号格，后发生的在大号格。

- **S1（满版一格）是正式页型，不是残缺页**。一页只有一格时，整页就是一张脸、
  一双手或一个对视，把所有画面信息都放进这一格，**不许自己加格**。
- 2 格（D1 上下 / D2 左右）用于「前后两个动作」或「双人对峙」；一格可以装一个完整小回合（2~4 句）。
- 相邻页不许用同一骨架（会读腻）；但连续两页都是 S1 是允许的——
  那是刻意的连拍，不是撞版式。

三、summary.caption 的固定顺序（不许换序）
1. 分格句：说清一页几块、谁大谁小、白沟与墨框。**这一句丢了模型会画成无边框大图。**
2. 画风锁：`Manga page, monochrome, greyscale screentones, dense vertical wall screentone, inked lineart, bold black ink outlines, pen crosshatching.`
3. 主格人物的**完整外形串**（从台账逐字抄，不许改写、不许缩写）。
   **这是第一人称 POV：画面里只有女生，绝不出现 1boy / boy / 男人 / 男性的脸、身体、衣服。**
   男主是镜头后的人，只需要入画时写 `pov hands` / `disembodied hand`（镜头外伸进来的一只手），
   不写他的脸和身体。caption 里绝不许出现 `1boy`、`a boy`、`a man`、`male`。
4. 场景串：地点 / 时间 / 光源 / 两三个具体物件。
5. 尾句（代码按本页台词自动写死，你不用自己拼）：
   - 只有女生说话：`Flat screentone shading, clear Chinese text: the girl's words in sharp speech bubbles with short pointer tails; no bare text, no off-screen speaker.`
   - 剧本写了 `男「…」`（画外男主）时，才换成：`Flat screentone shading, clear Chinese text: the girl's words in sharp speech bubbles, the off-screen boy's words as white-outlined bare text with no bubble.`
   - 本页无任何文字：`no text and no speech bubbles anywhere on the page.`
   **男主画外裸字不是每页标配**：用户没写男主台词时，绝不许自己加一段画外字、也不许在右下角安排男主的话。

四、items
- 每个角色 / 空镜 / 文字各自一条，bbox 钉死。
- label：character（有人物）/ object（空镜道具）/ text（台词旁白拟声）。
- 人物的 caption 结尾必须再重复一次画风锁短句：
  `monochrome greyscale screentone, bold black ink outline, flat shading, no color cast.`
- 文字 item 的 caption 只描述容器（形状+描边+竖排列数+尾巴指向），台词内容只进 text 字段。
  **女生的话**（user 标 女 / who=girl）：圆角气泡 + `short pointer tail pointing toward the girl's face`。
  **男生的话**（user 标 男 / who=boy）：**不许画任何气泡**——写 `Boy plain text: bare Chinese
  dialogue overlaid directly on the panel, thick white outline around every character,
  NO speech bubble, no bubble tail, no pointer tail, no enclosing frame`。男主不出镜，
  给他画气泡，尾巴就只能指向女生，读者会当成女生说的——这是本次要修的归属事故。
  旁白写 `(no bubble tail)`；拟声写 `no bubble`。
- 空镜格在 caption 尾部声明 `No speech bubble and no text in this panel.`
  **有字的格子绝不许出现这句**。

五、文字框 bbox 公式（格子 [y0,x0,y1,x1]，h=y1-y0，w=x1-x0）
- 气泡从下往上叠，最多 4 条，每条高 0.21h、间隔 0.03h：
  第 1 条（最下）：[y0+0.74h, x1-0.49w, y0+0.95h, x1-0.07w]
  第 2 条：[y0+0.50h, x1-0.49w, y0+0.71h, x1-0.07w]
  第 3 条：[y0+0.26h, x1-0.49w, y0+0.47h, x1-0.07w]
  第 4 条（最上）：[y0+0.02h, x1-0.49w, y0+0.23h, x1-0.07w]
- 男主白边裸字（第 1 条，贴右上，与气泡上下错开）：[y0+0.04h, x0+0.55w, y0+0.26h, x1-0.07w]
- 男主白边裸字（第 2 条，往下叠）：[y0+0.28h, x0+0.55w, y0+0.48h, x1-0.07w]
  同格既有裸字又有气泡时：裸字占上半（0.04~0.48h）、气泡只排最下两条
  （0.50~0.95h），绝不叠在一起；一格最多 2 裸字 + 2 气泡。
- 旁白（贴左上，一格多句往下叠，最多 4 框，每框高 0.21h、间隔 0.02h）：
  第 1 框：[y0+0.04h, x0+0.07w, y0+0.25h, x0+0.56w]
  第 2 框：[y0+0.27h, x0+0.07w, y0+0.48h, x0+0.56w]
  第 3 框：[y0+0.50h, x0+0.07w, y0+0.71h, x0+0.56w]
  第 4 框：[y0+0.73h, x0+0.07w, y0+0.94h, x0+0.56w]
- 拟声（居中）：[y0+0.34h, x0+0.24w, y0+0.66h, x1-0.24w]
**文字框离格边至少留 2%，绝不贴画面边缘（贴边会生前缀乱码）。**

六、同格多文字的上下顺序
**先说的在最上、后说的往下排**（中文阅读顺序，从上往下）：
先发生的放上，后发生的放下。
同一格既有旁白又有气泡时，按「起念 → 落定」排，不要机械套「旁白在左上」。

七、硬禁项
- 不写采样参数、不写负向池、不写画师名。
- **人物镜头最紧只到「中近景」**：`medium close-up with head and shoulders in frame`。
  不许写 `face filling the panel` / `extreme close-up of the face` / `tight close-up`
  —— 脸占满画面时模型会把下巴拉成尖三角，同一个角色翻一页就换了一张脸
  （2026-11-21 用户实拍：第 2 页男生当主体时脸变尖）。
- 正文不出现色相词（red/blue/brown/pink/green/yellow/orange/purple），只写明暗与材质。
- 衣物动作写成**已完成的静态结果**（`her hand resting on the notebook`），
  不写拉扯过程动词。
- 台词按编剧稿原样落字，**不许为凑短而删字或改写成碎句**；列数按字数自动给足
  （十来字一列，长了排两列），别把长句挤成一列小字。全页拟声最多 1 个；
  一格最多四条气泡，同格有男主裸字时气泡退到两条。
- 一页只有一个地点。
- 双人同框时，第二个人的句首必须显式互斥（`In sharp contrast, the boy has NO long hair, NO glasses, ...`），
  切断第一个人的特征往下传。**互斥只列外形与衣服**（头发 / 眼睛 / 裙子 / 开衫这一类），
  随身物件（包 / 伞 / 杯子 / 手机……）一个字都不许写——`NO backpack` 和
  `with empty hands` 都会把这件东西请进画面，另一个人就会被画上同款。
  物件只在它主人那一个人的描述里出现一次，另一个人的描述里绝不再提。

八、坐标与顶层字段
- bbox_coord_space 固定 "abs_centralized"，bbox_format 固定 "yxyx"。
- `canvas`：**这一页只有一格时写 `"single"`，两格以上写 `"multi_panel"`**
  （官方 schema 只认 single / multi_panel / collage；写错的后果是模型自己猜整图还是分格）。
- 画布 1024×1536：x ∈ [-512,512]，y ∈ [-768,768]，点均为整数。
- summary.caption **必须全英文**，一个汉字都不许有（中文 Mix 进去整页 prompt 会废）。

八·补：分格 bbox 必须整组照抄，不许自拼
- 一页只能用**同一套**骨架的全部格子，不许从 L0 拿两格、再从 L1 拿三格拼在一起。
- 格子之间绝不允许重叠，也不允许少一格或多一格。
- 场景只需要 3 格时就用 L4 或 L5，不要用 L0 硬凑。

九、text 字段（漏了这一页就是无字页，最致命的错）
- 每个 label=text 的 item **必须**带 `text` 字段，内容是该句台词原文，
  一字不改、不加标点、不许留空、不许把台词塞进 caption 代替。
- caption 只描述气泡容器，永远不承载台词内容。
- 台词归属：女生说的话 → 女生所在那格，画带尾巴气泡；男生说的话 → 落在他那格，
  画无气泡的白边竖排裸字（第四节），绝不给男主画指向女生的气泡。
  user 里给了几句台词，就必须产出几条带 text 的 item，数量对得上。

十、外形串的粘贴方式
- 人物 caption 结构 = 镜头与动作描述 + 该角色**整段外形串（原样连续粘贴）** + 画风锁短句。
- 外形串必须连续整段出现，不许拆开、不许调序、不许省略任何子句、不许改写用词。
- 同页同一角色出现多次，每次都要重新完整粘贴一遍，不做「同上省略」。

十一、否定句的适用范围
- 判据只看「这一格里有没有 text item」，跟格里有没有人无关。
- 格里没有 text item → 该格 caption 末尾必须写
  `No speech bubble and no text in this panel.`
- 格里有 text item → 必须删除这句，两者绝不同时出现。

十二、气泡不许自己估位置
- 一律用第五节的公式算，不许目测。
- 一条气泡时贴下半部；同格多条时按第五节公式从下往上叠，最多四条；
  气泡宽度约为格宽的 42%，绝不许占满整格宽。

十三、服装只许写「变动」，不许新增
- 衣物以角色台账为唯一基准，台账里写了什么就只能画什么。
- 每格只允许写**已经发生的变化**（`cardigan buttoned`、`sleeves rolled up`、
  `jacket already open`、`cardigan slipped off one shoulder`），
  不写拉扯过程，也不重新描述整身衣服。
- 台账里没有的衣物一律不许出现。下半身（裤／裙）在任何镜头、任何格子都不许改。
- 同一个人在同页出现多次，衣服必须完全相同，只有上述「变动」可以不同。
"""

MOSS_CONTRACT = MOSS_CONTRACT.replace("__LAYOUTS__", LAYOUT_TABLE)

SKELETONS = {
    "L0": [[-768, -494, -692, 494], [-692, -262, 754, 494],
           [-378, -494, 117, -272], [130, -494, 540, -272],
           [553, -494, 754, -272]],
    "L1": [[-768, -494, -692, 494], [-692, -494, 754, 262],
           [-378, 272, 117, 494], [130, 272, 540, 494], [553, 272, 754, 494]],
    "L2": [[-750, -494, 61, 494], [74, -494, 750, -165],
           [74, -165, 750, 165], [74, 165, 750, 494]],
    "L3": [[-750, -494, -260, -7], [-750, 7, -260, 494],
           [-246, -494, 244, -7], [-246, 7, 244, 494],
           [258, -494, 748, -7], [258, 7, 748, 494]],
    # 2026-11-21（阅读顺序）：规整 5 格，格号严格 = 从上到下、从左到右的阅读顺序。
    # 上排两格、下排三格；L0/L1 的「顶条 + 通高主格」按格号读会跳，保留给老剧本，
    # 新剧本 5 格默认走这套。坐标与 L0–L5 同系（y[-750,750] x[-494,494]）。
    "L6": [[-750, -494, -8, -8], [-750, 8, -8, 494],
           [8, -494, 750, -172], [8, -160, 750, 160],
           [8, 172, 750, 494]],
    "L4": [[-750, -494, -250, 494], [-236, -494, 236, 494], [250, -494, 750, 494]],
    "L5": [[-750, -494, 750, -7], [-750, 7, 61, 494], [74, 7, 750, 494]],
    # 2026-11-21（B 层）：补 1 格 / 2 格。用户给的素材只有一个 beat 时，
    # 原来的 L0–L5 全是 3~6 格，编剧想「一页只画这一瞬」也没有骨架可用，
    # 只能往空格子里派内容——这就是「为了 12345 分格子」的机械根因。
    # 坐标与 L0–L5 同一套坐标系（abs_centralized yxyx，y∈[-768,768]，x∈[-494,494]）。
    "S1": [[-768, -494, 768, 494]],
    "D1": [[-768, -494, -8, 494], [8, -494, 768, 494]],
    "D2": [[-768, -494, 768, -8], [-768, 8, 768, 494]],
    # 2026-11-21（三格）：L5「左通高 + 右两小」左栏比例 1:3.1，人塞进去只能画成竹竿
    # （用户原话「角色突然变得很高」）；L4 三条等宽横带又没有主次。
    # T3 改成「上大横格 + 下两格」：三格都不窄，且上格天然是这一页的主画面。
    "T3": [[-768, -494, 96, 494], [112, -494, 768, -8], [112, 8, 768, 494]],
}

# ── 画风与分格句：由代码写死，不让模型每页自己措辞 ──
# 三页画风漂移的根因就是这段每页文字不同，等于每页重新抽一次画风。
STYLE_LOCK = (
    "manga page, monochrome, greyscale, screentones, inked lineart, "
    "bold black ink outlines, pen crosshatching, Japanese manga illustration, "
    "dense vertical wall screentone on every surface, flat two-tone shading, "
    "hard-edged shadow blocks, deep crushed blacks, uniform screentone texture, "
    # 2026-11-21 用户反馈第 8 页整页发白：建场格全白窗+白墙+白衬衫把明度拉高了。
    # 这一句是整页明度锚，防止单靠「有网点」被模型理解成「浅灰也行」。
    "whole page sits at a mid-grey overall value with large dark masses in every panel, "
    "the only near-white area allowed is the window sliver, "
    "no soft gradient, no color, no color cast."
)

STYLE_ITEM = (
    "monochrome, greyscale, screentones, inked lineart, bold black ink outlines, "
    "flat two-tone shading, hard shadow edges, mid-grey overall value, no color."
)

# 2026-11-21 拆掉：BODY_LOCK / FIXED_LOWER / stamp_cast_body。
#   BODY_LOCK 给每个角色无条件前置「bare-faced, zero makeup, visible collarbone」——
#   用户写了"涂了口红""戴项链"也照加，自相矛盾。
#   FIXED_LOWER 在台账没写下半身时替用户定一条「dark straight trousers」。
#   stamp_cast_body 还把 silver 换成 metal（银发变金属发）。
#   这些都是替用户/编剧决定外观，现在外观串一律原样使用。

LAYOUT_SENTENCE = {
    "L0": ("Manga page divided into five panels: a thin establishing strip across the top, "
           "one tall full-height main panel on the right, and three stacked small panels "
           "down the left column, separated by clean white gutters and bold ink borders."),
    "L1": ("Manga page divided into five panels: a thin establishing strip across the top, "
           "one tall full-height main panel on the left, and three stacked small panels "
           "down the right column, separated by clean white gutters and bold ink borders."),
    "L2": ("Manga page divided into four panels: one wide main panel across the top and "
           "three equal small panels in a row across the bottom, separated by clean white "
           "gutters and bold ink borders."),
    "L3": ("Manga page divided into six equal panels in two columns and three rows, "
           "separated by clean white gutters and bold ink borders."),
    "L6": ("Manga page divided into five panels read strictly top to bottom then left to "
           "right: two panels across the top row and three across the bottom row, "
           "separated by clean white gutters and bold ink borders."),
    "L4": ("Manga page divided into three full-width horizontal bands stacked top to "
           "bottom, separated by clean white gutters and bold ink borders."),
    "L5": ("Manga page divided into three panels: one tall full-height panel on the left "
           "and two stacked panels on the right, separated by clean white gutters and "
           "bold ink borders."),
    # B 层：一页一格 / 一页两格。单格页要说死「整页就是一个画面」，
    # 否则模型会按习惯自己切出格子——它是看着 "Manga page" 这个词就切格。
    "S1": ("Manga page as ONE single full-page panel with no inner borders: the whole page "
           "is one continuous image, one moment, one framing, edge to edge."),
    "D1": ("Manga page divided into two panels, one above the other, separated by one "
           "clean white gutter and bold ink borders."),
    "D2": ("Manga page divided into two panels, side by side, separated by one clean "
           "white gutter and bold ink borders."),
    "T3": ("Manga page divided into three panels: one wide main panel across the top, and two "
           "equal panels side by side across the bottom, separated by clean white gutters "
           "and bold ink borders."),
}

# 编译尾句按「本页到底有哪些人的话」生成（2026-11-21）：
# 男主画外裸字不是每页标配——只有剧本真的写了 `男「…」` 才提 boy。
# 之前写死 boy 那句，导致只有女生对话的页也被要求画一段画外男主字（右下角那句）。
TAIL_GIRL_ONLY = ("Flat screentone shading, clear Chinese text: the girl's words in sharp speech "
                  "bubbles with short pointer tails; no bare text, no off-screen speaker.")
TAIL_WITH_BOY = ("Flat screentone shading, clear Chinese text: the girl's words in sharp speech bubbles, "
                 "the off-screen boy's words as white-outlined bare text with no bubble.")
TAIL_NO_TEXT = "no text and no speech bubbles anywhere on the page."


def detect_layout(js):
    """从产出的分格 bbox 反推它实际用了哪套骨架（用于校验版式和避免相邻页撞版式）。
    要求格数与骨架完全一致——少一格会在页面上留一块空白，出图会跑偏。"""
    bs = [tuple(i["bbox"]) for i in js.get("items") or []
          if i.get("label") in ("character", "object")]
    if not bs:
        return None
    for name, sk in SKELETONS.items():
        if len(bs) != len(sk):
            continue
        skt = sorted(tuple(s) for s in sk)
        if all(all(abs(a - b) <= 5 for a, b in zip(x, y))
               for x, y in zip(sorted(bs), skt)):
            return name
    return None


def _beat_of(n, pages):
    """兜底：剧本没写「本页承载」时才用。

    2026-11-21（A 层）拆掉按页数硬分配的「起/承/转/合」。
    那条规则在 4 页时会算出「第 3 页必须翻面、第 4 页必须给出结果」——
    用户给的素材只有一个偶遇，它只能自己造一个翻面和一个结局，
    于是就有了「放学回家→相遇→交谈→喝可乐→告别」那种流水账。
    现在这一拍由编剧在节拍表里写明（`本页承载：第 X 拍`），这里只做兜底。
    """
    return ("（剧本没有标明这一页承载哪一拍：只画这一页自己要画的那一件事，"
            "不许自行补一个转折或结局）")


def build_moss_user(script, page_n, prev_state, prev_layout, must_layout):
    lineup = []
    for m in script["acts"]:
        lineup.append("%d. %s" % (m["n"], m["oneshot"]))
    act = script["acts"][page_n - 1]

    lines = act.get("lines") or []
    who1 = lines[0]["who"] if lines else next(iter(script["cast"]), "girl")
    who_other = next((ln["who"] for ln in lines if ln["who"] != who1), None)
    if lines:
        # 台词落格**由代码定死**（按骨架槽位算），模型只负责把话说对。
        slots = line_slots(lines, must_layout)
        rows = []
        for i, ln in enumerate(lines):
            # 男主不出镜：给他画带尾巴气泡，尾巴只能指向女生 → 归属就错了。
            # 男主话一律白边裸字，和女生气泡从容器上隔离。
            box = ("容器：**白边裸字**（Boy plain text），无气泡、无尾巴、无框，竖排"
                   if ln["who"] == "boy"
                   else "容器：**带尾巴气泡**，尾巴指向女生脸")
            rows.append("  第 %d 句 ｜ 说话人 %s ｜ 只放进第 %d 格 ｜ %s ｜ text 字段填「%s」"
                        % (i + 1, ln["who"], slots[i], box, ln["text"]))
        line_block = "\n".join(rows)
        has_boy_here = any(ln["who"] == "boy" for ln in lines)
        boy_note = ("**男主的话绝不许画成气泡。**\n"
                    if has_boy_here else
                    "**本页没有男主台词**：一个 boy 字都不要写进 caption，"
                    "不许自作主张加画外男主的裸字。\n")
        count_line = "\n  共 %d 句，本页必须有 %d 个带 text 的 text item。\n" % (
            len(lines), len(lines))
        line_block += (count_line + boy_note +
                       "  同一格有多句时，按**从上到下的阅读顺序**排："
                       "先说的在最上、后说的往下排（第 1 句最上、第 4 句最下），"
                       "每条按字数给足列数（十来字一列，长了排两到三列），"
                       "文字要清楚可读，不许为凑短删字改字；"
                       "同格有男主裸字时，裸字占上半、气泡只排最下两条。")
    else:
        line_block = ("  本页没有台词：**不要画任何气泡**。"
                      "需要交代的事由下面的旁白块承担；没有旁白的格写否定句。")
    # 逐格稿：编剧已经写死了每格画什么 → 只按骨架方位念一遍，
    # 不再套「第 1 格 girl 全身 / 第 2 格 girl 中近景」那套旧模板（那才是「每格长得一样」的来源）。
    # 没有 cells 的旧剧本仍走 slot_plan_text，老产物照样能跑。
    cells = act.get("cells") or {}
    slot_plan = (cell_plan_text(must_layout, cells) if cells
                 else slot_plan_text(must_layout, who1, who_other, bool(lines)))

    # 逐格稿：编剧在哪一格写了旁白，那一格就画一个 caption 框，写几句画几句。
    # 旧格式（没有 narrs）回退到单句 narr + narr_cell。
    narrs = act.get("narrs") or []
    if not narrs and (act.get("narr") or "").strip():
        slots0 = line_slots(lines, must_layout) if lines else []
        nslot = (int(act["narr_cell"]) if act.get("narr_cell") else
                 (slots0[0] if slots0 else (LAYOUT_SLOTS.get(must_layout)
                                            or LAYOUT_SLOTS["L0"])["main"]))
        narrs = [{"text": act["narr"].strip(), "cell": nslot}]
    if narrs:
        rows = []
        for e in narrs:
            rows.append("  第 %s 格左上角放一个小方角 caption 框，"
                        "text 字段填「%s」。" % (e["cell"], e["text"]))
        narr_block = ("旁白（逐格逐句列出，必须原样照抄，一个字都不许改）：\n"
                      + "\n".join(rows)
                      + "\n  同一格有多句旁白时，按列出的先后**从上往下叠**，"
                      "各占一个框，别合并、别漏句；每句列数按字数给足，"
                      "十来字一列，长了排两到三列。\n"
                      "  每个 caption 框 caption 里写 `no bubble tail`；"
                      "**旁白框不是气泡**：不加气泡尾、不挪到别的格、不改写成台词。"
                      "框里中文**竖排**（一个字一行往下叠，多列时**从左往右**排，句首在最左列），不要横排。\n")
    else:
        narr_block = "本幕无旁白：不许自己加旁白框。\n"

    prev = "（本页是第一页，无前情）"
    if page_n > 1:
        prev = ("（**接续与入场状态**：上一页最后一格停在什么瞬间，本页第一格必须"
                "**从那个瞬间接着画**，不许另起场景、不许把已经发生过的事重演、"
                "不许折返回去已经走过的地点。\n"
                "  **上一页退场状态（硬约束）**：%s\n"
                "  人物的相对位置、谁身上有什么随身物，必须与这份状态一致；"
                "  只有本页剧本明确写了交接（谁把东西递给谁、谁走进或走出）才允许变。"
                "  不许凭空增减物件，不许交换两人的内外位置。）" % (prev_state or "unknown"))

    # 整段弧线 + 本页落在哪一拍（2026-11-21）。
    # 「没头没尾」的根因是逐页编译各自为政：每页只知道自己前后，不知道整段往哪走。
    # A 层改：这一拍由编剧在节拍表里写明（`本页承载：第 X 拍`），不再由代码按页数
    # 硬分配「起/承/转/合」——那样 4 页必然逼出「第 3 页翻面、第 4 页给结果」。
    arc = script.get("arc") or {}
    # 逐格稿不再有「拍号」这一层，beat 只对旧剧本有意义；不再按页数兜底分配起承转合。
    beat = (act.get("beat") or "").strip()
    # 这一页用哪条场景串（最多两个地点）。A 层：偶遇与喝可可可以不在同一个地方，
    # 但每条场景串仍然整段共用——换地点换的是哪一句，不是每页重新措辞。
    scene_text = (act.get("scene_text") or script.get("scene") or act["visual"]).strip()
    arc_line = ""
    if (arc.get("start") or "").strip() or (arc.get("end") or "").strip():
        arc_line = ("整段走向（**这一页只是其中一段，绝对不许当独立故事写**）：\n"
                    "  段首：%s\n"
                    "  段末：%s\n\n"
                    % ((arc.get("start") or "—").strip(),
                       (arc.get("end") or "—").strip()))

    # 本页剧本块：只写有值的字段。逐格稿没有欲望/阻碍/入场状态这些栏，
    # 全空还照模板输出「欲望：」会变成一屏空行，模型得先猜这些空行是什么意思。
    sb = []
    for lab, key in (("欲望", "want"), ("阻碍", "block"), ("信息差", "gap"),
                     ("入场状态", "state_in"), ("退场状态", "state_out"),
                     ("既成事实", "fact"), ("本页服装", "outfit")):
        v = (act.get(key) or "").strip()
        if v and v.lower() not in ("none", "same as cast sheet"):
            sb.append("%s：%s" % (lab, v))
    # 逐格稿不再重复一遍整页「画面要点」：上面逐格语义已经一条条给过了，
    # 再来一段整页概述只会和格子打架（模型会拿概述去覆盖格内内容）。
    if not cells:
        v = (act.get("visual") or "").strip()
        if v:
            sb.append("画面要点：%s" % v)
    script_block = ("本页剧本：\n" + "\n".join(sb) + "\n") if sb else ""

    return """第 %d 页 / 共 %d 页（这一页 %d 格）

%s本页指定版式：**%s**。必须严格使用 %s 的全部格子，bbox 逐字照抄，
不许增减格子、不许换别的骨架、不许从别的骨架借格子。
**这一页的格数就是 %d，不许为了「看起来像漫画」自己多加格子。**

本页 %d 格语义（写死，不许调换、不许改谁在哪格）：
%s
前情（按顺序，每页一句）：
%s

%s台词（落格已由代码指定，照做；每条都必须落成一个带 text 字段的 text item）：
%s
%s
本页场景串（summary.caption 第４段必须**逐字照抄**这一句，一个字都不许改；
不许补充时间、色温、氛围词——同一场景的页共用同一句，画风才不漂）：
%s

角色外形串（**整段原样连续粘贴，不许拆开调序省略；只有女生，绝不出现 boy/1boy**）：
%s
**第一人称 POV 硬约束：画面里只有女生。绝不画 1boy / boy / 男人 / 男性的脸、身体、衣服——
男主是镜头后的人，不出镜；需要入画时只写 pov hands / disembodied hand（镜头外伸进来的一只手）。
上面角色串里若出现任何 boy/1boy 行，一律忽略，绝不照着画成画面里的人。**
%s
上一页版式：%s

请按契约输出这一页的 Layout JSON。""" % (
        page_n, len(script["acts"]), len(SKELETONS.get(must_layout) or []),
        arc_line, must_layout, must_layout,
        len(SKELETONS.get(must_layout) or []),
        len(SKELETONS.get(must_layout) or []),
        slot_plan,
        "\n".join(lineup),
        script_block,
        line_block,
        narr_block,
        scene_text,
        "\n".join(ln for ln in script["cast_text"].splitlines()
                  if not re.match(r"\s*-\s*boy\s*:", ln, re.I)),
        prev, prev_layout or "无")


# ────────────────────── 剧本解析 ──────────────────────
# 2026-11-21 拆掉的三个改写器（HUE_CLEAN / clean_hue / normalize_scene）：
#   它们把颜色词映射成明度、把编剧写的时间换成 "late afternoon"。
#   共同点都是「替 AI 决定用词」——发色事故就是这一族造成的。
#   现在用词一律交给编剧，代码只做结构和坐标。
def parse_script(md, brief=""):
    """把编剧产出的 Markdown 拆成结构化 dict。

    `brief` 只用于 `check_script`（已停用）；解析本身不改任何用词。

    2026-11-21（A 层）扩展：
      · 块标题接受 `## 页 N`（新）与 `## 幕 N`（旧产物），两种都认；
      · 场景可有两条（`## 场景 1` / `## 场景 2`），每页用 `场景：1` 指定；
      · 每页读 `格数 / 本页承载 / 核心信息 / 页尾钩子`，这四个是本轮新增的字段。
    """
    out = {"title": "", "logline": "", "cast": {}, "cast_text": "",
           "scene": "", "scenes": {}, "acts": [], "page_count": 0,
           "beats": "", "arc": {"start": "", "end": ""}}

    m = re.search(r"^# (.*)$", md, re.M)
    if m:
        out["title"] = m.group(1).strip()
    m = re.search(r"## 梗概\n+(.*?)\n", md)
    if m:
        out["logline"] = m.group(1).strip()

    # 整段弧线（2026-11-21）：起点 → 终点。它是「有没有头尾」的判据，
    # 编译时会把「本页承载哪一拍」一起带给逐页 AI。
    am = re.search(r"起点\s*[:：]\s*(.+)", md)
    bm = re.search(r"终点\s*[:：]\s*(.+)", md)
    out["arc"] = {"start": am.group(1).strip() if am else "",
                  "end": bm.group(1).strip() if bm else ""}

    # 角色台账：- girl: xxx
    for mm in re.finditer(r"^- ([a-z_]+)\s*:\s*(.+)$", md, re.M):
        out["cast"][mm.group(1)] = mm.group(2).strip()

    # 节拍表原文（保留，编译时不需要，但 artifact 里要能看到）
    bm2 = re.search(r"^## 节拍表\s*\n(.*?)(?=\n## |\Z)", md, re.S | re.M)
    out["beats"] = bm2.group(1).strip() if bm2 else ""

    # 页数：编剧自己算的实际页数（≤ 用户给的上限）
    pm = re.search(r"^## 页数\s*\n(.+)", md, re.M)
    if pm:
        # 「由节拍表第 1–5 拍推导，共 4 页」→ 抓「共 N 页」，不要抓拍号
        num = re.search(r"共\s*(\d+)\s*页", pm.group(1)) or re.search(r"(\d+)\s*页", pm.group(1))
        out["page_count"] = int(num.group(1)) if num else 0

    # 场景：可有多条（`## 场景 1` / `## 场景 2`），也兼容旧的单条 `## 场景`
    for sm in re.finditer(r"^## 场景(?:\s*(\d+))?\s*\n(.*?)(?=\n## |\Z)", md, re.S | re.M):
        key = (sm.group(1) or "1").strip()
        out["scenes"][key] = " ".join(sm.group(2).split()).strip()
    out["scene"] = out["scenes"].get("1", "")

    # 块：`## 页 N` 或 `## 幕 N`（旧剧本）
    blocks = re.split(r"^## (?:页|幕)\s*(\d+)\s*$", md, flags=re.M)
    # blocks: [前言, "1", 内容1, "2", 内容2, ...]
    for i in range(1, len(blocks) - 1, 2):
        n = int(blocks[i])
        body = blocks[i + 1]
        d = {"n": n}
        # 版式：编剧可以显式指定（L0–L5 / S1 / D1 / D2），也可以只给格数
        lm = re.search(r"(?:版式|layout)\s*[：:]\s*(S\s*1|D\s*[12]|L\s*[0-5])", body, re.I)
        d["layout"] = re.sub(r"\s+", "", lm.group(1)).upper() if lm else ""
        # 格数（1~4）：页的格数由拍数决定，这是本轮的主口径
        gm = re.search(r"格数\s*[：:]\s*([1-6])", body)
        d["grid"] = int(gm.group(1)) if gm else 0
        # 这一页用哪条场景（1 / 2）
        scm = re.search(r"场景\s*[：:]\s*([12])", body)
        d["scene_no"] = scm.group(1) if scm else "1"
        # 本页承载的拍（自由文本，如「第 3 拍」或「第 3 拍、第 4 拍」）
        d["beat"] = grab_first(body, "本页承载")
        # 拍号列表：只用来做「格数 = 拍数」的结构核对，不判剧情
        d["beat_nums"] = [int(x) for x in re.findall(r"第\s*(\d+)\s*拍", d["beat"] or "")]
        d["core"] = grab_first(body, "核心信息")
        d["hook"] = grab_first(body, "页尾钩子")

        def grab(label, nxt_list):
            pat = r"%s\s*[:：]\s*(.*?)(?=\n(?:%s)\s*[:：]|\Z)" % (
                label, "|".join(nxt_list))
            mm = re.search(pat, body, re.S)
            return mm.group(1).strip() if mm else ""

        d["want"] = grab("欲望", ["阻碍"])
        d["block"] = grab("阻碍", ["信息差"])
        d["gap"] = grab("信息差", ["入场状态"])
        d["state_in"] = grab("入场状态", ["退场状态"])
        d["state_out"] = grab("退场状态", ["既成事实", "本页服装", "本幕服装", "旁白", "台词"])
        # 既成事实（2026-11-21）：这一幕结束后世界变了什么。它是下一页的起点，
        # 也是「剧情接得上」的机械判据——不是情绪词，是事实。允许 none。
        d["fact"] = grab("既成事实", ["本页服装", "本幕服装", "旁白", "台词"])
        d["lines_text"] = grab("台词", ["画面要点"])
        d["visual"] = grab("画面要点", [])
        # 旁白（2026-11-21）：读者必须知道、人物没说出口的那一句。落成 caption 框，
        # 不是气泡。没有这一行就是空串，不是错误。
        nm = re.search(r"旁白\s*[:：]\s*(?:「(.+?)」|([^\n]+))", body)
        narr = ((nm.group(1) or nm.group(2) or "").strip().strip('「」"').strip()
                if nm else "")
        # 编剧常把格式说明里的「旁白：」前缀照抄进来，剥掉，别画进画面
        narr = re.sub(r"^(旁白|Narrator)\s*[:：]\s*", "", narr).strip()
        d["narr"] = narr

        # 退场状态里可能带中文注解，取英文短语前半段
        def en_only(s):
            s = re.sub(r"（.*?）|\(.*?\)", "", s)
            return " ".join(s.split()).strip()

        d["state_in"] = en_only(d["state_in"])
        d["state_out"] = en_only(d["state_out"])
        d["fact"] = en_only(d.get("fact") or "")
        # 逐格稿的页末状态（2026-11-21）：`状态：谁在哪 / 谁身上有什么 / 什么成了事实`。
        # 和旧格式的「退场状态」占同一个槽位，新写法优先；中文原样保留，不洗词。
        stm = re.search(r"^[ \t]*状态\s*[:：]\s*(.+?)\s*$", body, re.M)
        if stm:
            d["state_out"] = stm.group(1).strip()

        outfit_m = re.search(r"(?:本页服装|本幕服装|服装)\s*[:：]\s*(.+)", body)
        d["outfit"] = en_only(outfit_m.group(1)) if outfit_m else ""

        # 这一页用哪条场景串（没写就退回场景 1）
        d["scene_text"] = out["scenes"].get(d["scene_no"]) or out["scene"]

        # 一句话前情：优先「画面要点」首句
        oneshot = re.split(r"[。；;]", d["visual"])[0].strip() or d["want"] or d["core"]
        d["oneshot"] = oneshot

        # 台词：结构化，带说话人归属（只取「台词」栏，防止画面要点后面的溢出句混进来）
        pairs = re.findall(r"([女男])[「\"](.+?)[」\"]", d["lines_text"])
        d["lines"] = [{"who": "girl" if w == "女" else "boy", "text": t.strip()}
                      for w, t in pairs]
        d["line_count"] = len(d["lines"])

        # ── 逐格稿（2026-11-21 v2）──
        # `格1｜远景｜街角` 开头，下面依次是 画面 / 台词 / 旁白 / 音效。
        # 这是新格式的全部：一格就是一条能直接照着画的信息，不再有「本页画面要点」
        # 那种一整页一段话。解析出来以后，格数直接等于实际格数，台词自带格号。
        heads = list(re.finditer(r"^格\s*(\d+)\s*[｜|]\s*([^｜|\n]*?)\s*[｜|]\s*([^\n]*)$",
                                 body, re.M))
        if heads:
            cells = {}
            for idx, hm in enumerate(heads):
                k = int(hm.group(1))
                seg_end = heads[idx + 1].start() if idx + 1 < len(heads) else len(body)
                seg = body[hm.end():seg_end]
                f = {}
                cur = None
                for ln_ in seg.splitlines():
                    m2 = re.match(r"^\s*(画面|台词|旁白|音效)\s*[:：]\s*(.*)$", ln_)
                    if m2:
                        cur = m2.group(1)
                        f.setdefault(cur, []).append(m2.group(2).strip())
                    elif cur and ln_.strip():
                        f[cur].append(ln_.strip())
                vis = " ".join(x for x in f.get("画面", []) if x).strip()
                raw_lines = " ".join(f.get("台词", []))
                ls = [{"who": "girl" if w == "女" else "boy", "text": t.strip(),
                       "cell": k}
                      for w, t in re.findall(r"([女男])\s*「(.+?)」", raw_lines)]
                # 旁白可以一格写多句（2026-11-21 放开叙事密度）：每句「」单独一个框。
                # 原来只 re.search 第一句，第二句往后的叙事全被丢掉。
                raw_narr = " ".join(f.get("旁白", []))
                quoted = re.findall(r"「(.+?)」", raw_narr)
                if quoted:
                    nrs = [x.strip() for x in quoted]
                else:
                    nrs = [raw_narr.strip()]
                nrs = [re.sub(r"^(旁白|Narrator)\s*[:：]\s*", "", x).strip('「」"').strip()
                       for x in nrs]
                nrs = [x for x in nrs if x.lower() not in ("", "无", "none", "—", "-")]
                nr = nrs[0] if nrs else ""
                sfx = " ".join(f.get("音效", [])).strip()
                if sfx.lower() in ("无", "none", "—", "-"):
                    sfx = ""
                cells[k] = {"shot": hm.group(2).strip(), "subject": hm.group(3).strip(),
                            "visual": vis, "lines": ls, "narr": nr, "narrs": nrs,
                            "sfx": sfx}
            d["cells"] = cells
            d["grid"] = len(cells)          # 格数 = 实际写了几格，不再靠它自报
            d["lines"] = [ln for k in sorted(cells) for ln in cells[k]["lines"]]
            d["line_count"] = len(d["lines"])
            # 每格旁白各自保留：一格写几句就落几个框（2026-11-21 放开叙事后
            # 一格常有两三句）。单句 narr / narr_cell 仍填第一句，供旧调用方回退。
            d["narrs"] = [{"text": t, "cell": k}
                          for k in sorted(cells) for t in cells[k]["narrs"]]
            n1 = next((cells[k]["narr"] for k in sorted(cells) if cells[k]["narr"]), "")
            d["narr_cell"] = next((k for k in sorted(cells) if cells[k]["narr"]), None)
            if n1:
                d["narr"] = n1
            d["visual"] = d["visual"] or "。".join(
                cells[k]["visual"] for k in sorted(cells) if cells[k]["visual"])
            if cells:
                d["oneshot"] = cells[min(cells)]["visual"] or d["oneshot"]

        out["acts"].append(d)

    # 2026-11-21：cast 与英文台账栏一律原样使用。
    # 这一段原来是 clean_hue()，把颜色词确定性映射成明度词（blonde→light、green→mid-grey），
    # 结果把用户明确写的「金发绿眼」洗成了「light-light / light-grey」。
    # 用词是编剧的活，黑白是渲染的事——代码不该替它决定用哪个词。
    cast_lines = ["- %s: %s" % (k, v) for k, v in out["cast"].items()]
    out["cast_text"] = "\n".join(cast_lines)
    if not out["page_count"]:
        out["page_count"] = len(out["acts"])
    return out


def page_exit_state(act):
    """一页的退场状态：优先编剧写的 `状态：` 行，退回末格画面 + 既成事实。

    两处逐页编译都从这里取，口径只有这一份。逐格稿早期产物没有状态行，
    用末格画面兜底，至少不会把前情丢成 unknown。
    """
    st = (act.get("state_out") or "").strip()
    if not st and act.get("cells"):
        st = (act["cells"][max(act["cells"])].get("visual") or "").strip()
    fact = (act.get("fact") or "").strip()
    if fact:
        st = "%s | established: %s" % (st, fact)
    return st


def grab_first(body, label):
    """抓一行 `label：xxx`（到行尾），用于格数/拍号/核心信息这类单行字段。"""
    m = re.search(r"%s\s*[:：]\s*(.+)" % label, body or "")
    return m.group(1).strip() if m else ""


# ────────────────────── 代码闸门 ──────────────────────
HUE = re.compile(r"\b(red|blue|brown|pink|green|yellow|orange|purple)\b", re.I)
STYLE_LOCK_KEYS = ("screentone", "monochrome", "inked lineart")
NEG_SENT = "No speech bubble and no text in this panel."


def _kind(cap):
    """按 caption 判定文字元素类型：气泡 / 男主白边裸字 / 旁白 / 拟声。"""
    low = (cap or "").lower()
    if "boy plain text" in low:
        return "plain"
    if "no bubble tail" in low:
        return "caption"
    if "no bubble" in low:
        return "sfx"
    return "bubble"


def _text_columns(text):
    """竖排列数：一列约十四五个字，长了才多给列，最多三列（再多格子放不下）。

    2026-11-21 第二次收紧：多列时模型实测按日漫习惯从**右**列开始读，
    与要求的「从左往右」相反（实拍那句「热死了，洗把脸」就是右列先读）。
    既然一列不存在左右顺序问题，就把阈值抬到 14——常见台词一列到底，
    只有真长句才排两列。
    """
    n = len(text or "")
    if n <= 14:
        return 1
    if n <= 26:
        return 2
    return 3


CAPTION_BOX = ("Small rectangular caption box in the upper left corner of this panel, "
               "thin black ink outline, Chinese text written vertically with each "
               "character stacked top to bottom, the first character at the TOP of the "
               "LEFT column, reading left to right, no bubble tail, no pointer, "
               "single caption box only in this panel.")


def boy_text_cap(text):
    """男主台词的 caption：**白边裸字，无气泡无尾巴**（2026-11-21 用户要求）。

    第一人称视角下男主不出镜；带尾巴气泡只能指向画面里唯一的女生，
    读者就把他的话当成女生说的。所以他的话不套任何容器，
    直接压在画面上的白边竖排字，和女生气泡一眼分得开。
    """
    ncol = _text_columns(text)
    return ("Boy plain text: bare Chinese dialogue overlaid directly on the panel, "
            "thick white outline around every character for legibility, "
            "NO speech bubble, no bubble tail, no pointer tail, no enclosing frame, "
            "Chinese text written vertically with each character stacked top to bottom, "
            "%d column%s, first character of the sentence at the TOP of the LEFT column, "
            "reading left to right, "
            "compact plain text sized so several can share this panel." %
            (ncol, "" if ncol == 1 else "s"))




def bubble_cap(text, who="girl"):
    """气泡的 caption：**由代码统一写**，不让模型每页自己措辞。

    依据 `moss-image-prompt/SKILL.comic.md` §7：caption 只说明「有什么形状的文字容器、
    在画面哪个方位」，内容放 `text`。

    **竖排中文**（2026-11-21 用户要求）：一个字往下叠成竖列。
    多列时**从左往右**排，句首落在最左列——2026-11-21 用户第二次修正：
    「中国人习惯从左往右读」，日式从右往左会让读者按相反顺序读出台词。
    横排中文在这套画风里会被压扁、行距挤成一坨（实测那页就是这样）。
    台词一句一个意思，一列约放 10 字，超了才排两列。

    **尾巴点名说话人**（2026-11-21）：原来只写 `the speaker's face`，两人同框时
    模型自己猜，实测把女生的「我来拿」尾巴画到了男生脸上。这里点名 girl / boy。
    """
    ncol = _text_columns(text)
    target = "the boy's" if who == "boy" else "the girl's"
    return ("Vertical rounded speech bubble with a thin black ink outline, "
            "Chinese text written vertically with each character stacked top to bottom, "
            "%d column%s, first character of the sentence at the TOP of the LEFT column, "
            "reading left to right, "
            "short pointer tail pointing toward %s face, "
            "compact bubble sized so several can share this panel." %
            (ncol, "" if ncol == 1 else "s", target))


def bbox_bubble(cell, idx):
    """同格气泡从下往上叠，最多 4 条（2026-11-21 高密度小漫画）。

    每条高 0.21h、间隔 0.03h，最下一条贴 y0+0.74h、最上一条到 y0+0.02h。
    原来只有两条（贴 y1-0.27h / y1-0.56h），一格塞三句就会叠在一起。
    宽度仍是格宽 42%，不占满整格。
    """
    y0, x0, y1, x1 = cell
    h, w = y1 - y0, x1 - x0
    i = max(0, min(int(idx), 3))
    top = 0.74 - 0.24 * i
    return [int(y0 + top * h), int(x1 - 0.49 * w),
            int(y0 + (top + 0.21) * h), int(x1 - 0.07 * w)]


def bbox_caption(cell, idx):
    """旁白框：贴格左上，一格多句时往下叠，最多 4 个框。

    原来只写了 idx 0/1 两个位置，放开叙事后一格能写两三句旁白，
    第三句会和第一句重叠。改成递推：每个框高 0.21h、间隔 0.02h，
    从 0.04h 起往下排，到 0.96h 收住，绝不贴格边。
    """
    y0, x0, y1, x1 = cell
    h, w = y1 - y0, x1 - x0
    i = max(0, min(int(idx), 3))
    top = 0.04 + 0.23 * i
    return [int(y0 + top * h), int(x0 + 0.07 * w),
            int(y0 + (top + 0.21) * h), int(x0 + 0.56 * w)]


def bbox_sfx(cell):
    y0, x0, y1, x1 = cell
    h, w = y1 - y0, x1 - x0
    return [int(y0 + 0.34 * h), int(x0 + 0.24 * w),
            int(y0 + 0.66 * h), int(x1 - 0.24 * w)]


def bbox_plain(cell, idx):
    """男主白边裸字：贴右上，和贴右下的女生气泡上下错开，同格也不叠。"""
    y0, x0, y1, x1 = cell
    h, w = y1 - y0, x1 - x0
    if idx == 0:
        return [int(y0 + 0.04 * h), int(x0 + 0.55 * w),
                int(y0 + 0.26 * h), int(x1 - 0.07 * w)]
    return [int(y0 + 0.28 * h), int(x0 + 0.55 * w),
            int(y0 + 0.48 * h), int(x1 - 0.07 * w)]


# 每套骨架的「角色槽位」——台词落格和每一格的语义都从这里取，不写死格号。
# 换骨架时格号的含义全变，只要槽位表跟着换，落格规则就不用改。
#   scene 建场空镜（禁人禁字）  main 第一说话人全身
#   s1 第一说话人中近景（先说话，放上面）  s2 第二说话人中景（后说话，放下面）
#   prop 道具特写  extra 第六格的多余位（只 L3 有）
LAYOUT_SLOTS = {
    "L0": {"scene": 1, "main": 2, "s1": 3, "s2": 4, "prop": 5},
    "L1": {"scene": 1, "main": 2, "s1": 3, "s2": 4, "prop": 5},
    "L2": {"scene": None, "main": 1, "s1": 2, "s2": 3, "prop": 4},
    "L3": {"scene": 1, "main": 2, "s1": 3, "s2": 4, "prop": 5, "extra": 6},
    "L6": {"scene": None, "main": 1, "s1": 1, "s2": 2, "prop": 4},
    "L4": {"scene": None, "main": 1, "s1": 2, "s2": 3, "prop": None},
    "L5": {"scene": None, "main": 1, "s1": 2, "s2": 3, "prop": None},
    # B 层：
    #   单格页没有「第二格」可放第二个人——两个人必须同框在第一格，
    #   台词也全落第一格（一格最多四条气泡 / 有男主裸字时最多两条）。
    "S1": {"scene": None, "main": 1, "s1": 1, "s2": 1, "prop": None},
    #   两格页：第一格放第一说话人，第二格放反应/第二说话人。
    "D1": {"scene": None, "main": 1, "s1": 1, "s2": 2, "prop": None},
    "D2": {"scene": None, "main": 1, "s1": 1, "s2": 2, "prop": None},
    #   三格页：上格是主画面（第一说话人），下两格放反应 / 道具。
    "T3": {"scene": None, "main": 1, "s1": 1, "s2": 2, "prop": 3},
}


def line_slots(lines, layout="L0"):
    """每句台词的目标格号（1 基），按骨架的槽位表算。

    口径只有这一处，`build_moss_user` 和 `autofix` 都调它——写在两个地方，
    prompt 里说的和代码补的就一定会错开。

    **按说话先后从上往下排**（中文读者从上到下、从左到右）：
      第 1 句 → s1（上面那格，第一说话人中近景）
      同一个人再说 → main（他的全身主格）
      另一个人说 → s2（下面那格，第二说话人中景）
    """
    if not lines:
        return []
    # 逐格稿（v2）：编剧已经把每句台词写进了某一格，代码不再按说话人重新分配。
    # 「先说的在上、后说的在下」这套是旧格式的补救办法——那时候剧本里没有格这一层。
    if all(ln.get("cell") for ln in lines):
        n = len(SKELETONS.get(layout) or [])
        return [min(max(int(ln["cell"]), 1), n) if n else int(ln["cell"])
                for ln in lines]
    m = LAYOUT_SLOTS.get(layout) or LAYOUT_SLOTS["L0"]
    s1 = m["s1"]
    s2 = m.get("s2") or s1
    main = m["main"]
    who1 = lines[0]["who"]
    slots = []
    for i, ln in enumerate(lines):
        slots.append((s1 if i == 0 else main) if ln["who"] == who1 else s2)
    return slots


# 每套骨架里每个格的方位名，用来给模型写「第几格画谁」的指令
_CELL_ROLE = {
    "L0": {1: "顶部横条", 2: "右侧通高主格", 3: "左栏最上", 4: "左栏中间", 5: "左栏最下"},
    "L1": {1: "顶部横条", 2: "左侧通高主格", 3: "右栏最上", 4: "右栏中间", 5: "右栏最下"},
    "L2": {1: "上方宽主格", 2: "下排最左", 3: "下排中间", 4: "下排最右"},
    "L3": {1: "上排左", 2: "上排右", 3: "中排左", 4: "中排右", 5: "下排左", 6: "下排右"},
    "L6": {1: "上排左", 2: "上排右", 3: "下排最左", 4: "下排中间", 5: "下排最右"},
    "L4": {1: "上带", 2: "中带", 3: "下带"},
    "L5": {1: "左侧通高主格", 2: "右上", 3: "右下"},
    "S1": {1: "满版整页"},
    "D1": {1: "上半格", 2: "下半格"},
    "D2": {1: "左格", 2: "右格"},
    "T3": {1: "上方宽主格", 2: "下排左", 3: "下排右"},
}


def slot_plan_text(layout, who1, who_other, has_lines=True):
    """按骨架生成「第几格画谁」的硬指令；格位语义与 LAYOUT_SLOTS 同源。

    `has_lines=False` 时这一页没有台词，说话人未知：单格页不能说「只画 girl」，
    否则画面要点里明明是两个人同框，模型却只画一个人。
    """
    roles = _CELL_ROLE.get(layout) or _CELL_ROLE["L0"]
    s = LAYOUT_SLOTS.get(layout) or LAYOUT_SLOTS["L0"]
    other = who_other or "另一人"
    # 单格页单独写：这里是「两个人可以在同一格里」的唯一页型。
    # 沿用「一格只画一个人」的老口径会把它写成一个人的半身像，
    # 而用户要的正是「一页双人表达一个情绪」。
    if layout == "S1":
        if not has_lines and not who_other:
            body = ("本页画面要点里出现的人（一到两人**同框**，谁在前谁在后按画面要点，"
                    "没有第二个人也可以）；整页只有一个画面，不许切格")
        elif who_other:
            body = ("%s 与 %s **同框**（谁在前、谁在后、谁看谁，按本页画面要点）；"
                    "这是一整页一个画面，没有第二块格子，不许再切格" % (who1, other))
        else:
            body = ("%s 的中近景，或一双手/一个物件的特写（按本页画面要点）；"
                    "整页只有这一个画面，不许切格" % who1)
        return "  第 1 格（满版整页）：%s。\n" % body
    desc = {}
    if s.get("scene"):
        desc[s["scene"]] = "空镜，只有背景与物件；**不许画人、不许有任何文字**"
    desc[s["main"]] = "%s 的全身；只画这一个人" % who1
    desc[s["s1"]] = ("%s 的中近景，头与肩完整入画，脸不许占满画面；只画这一个人" % who1)
    if s.get("s2"):
        desc[s["s2"]] = "%s 的中景；只画这一个人" % other
    if s.get("prop"):
        desc[s["prop"]] = "道具特写，不画人脸"
    if s.get("extra"):
        desc[s["extra"]] = "背景细节或另一人的手部，不画正脸"
    out = []
    for i in sorted(roles):
        out.append("  第 %d 格（%s）：%s。" % (i, roles[i], desc.get(i, "背景空镜，不画人、不写字")))
    return "\n".join(out) + "\n"


def cell_plan_text(layout, cells):
    """逐格稿（2026-11-21 v2）：把编剧写的每一格，原样翻译成「第 K 格画什么」。

    和 `slot_plan_text` 的区别是方向相反：
      · 旧口径是代码先规定「第 1 格画 girl 全身、第 2 格画她的中近景」，
        编剧只提供台词——所以每格必然长得一样。
      · 新口径是编剧已经写死了每格的内容，代码只把它按骨架的方位名念一遍。
    格数对不上（剧本 4 格、骨架 5 格）时多出来的格写成空格，不编内容。
    """
    roles = _CELL_ROLE.get(layout) or {}
    n = len(SKELETONS.get(layout) or [])
    out = []
    for k in range(1, n + 1):
        c = (cells or {}).get(k)
        role = roles.get(k, "第 %d 格" % k)
        if not c:
            out.append("  第 %d 格（%s）：空格，只画背景与固定物件，不画人、不写字。"
                       % (k, role))
            continue
        seg = "  第 %d 格（%s）" % (k, role)
        if c.get("shot"):
            seg += " 景别=%s" % c["shot"]
        if c.get("subject"):
            seg += " 主体=%s" % c["subject"]
        seg += "：" + (c.get("visual") or "（画面待补）")
        out.append(seg)
    return "\n".join(out) + "\n"


def norm_text(s):
    """去掉标点空白，只留字，用于把模型改写过标点的台词对回剧本。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "")


def panel_owner(item):
    """判定一个格子主要画的是谁。取 caption 里最早出现的角色名，
    因为模型有时写 "the girl"，有时写裸的 "girl standing..."。"""
    c = (item.get("caption") or "").lower()
    found = []
    for k in ("girl", "boy"):
        m = re.search(r"\b%s\b" % k, c)
        if m:
            found.append((m.start(), k))
    return min(found)[1] if found else None


# 2026-11-21 拆掉：BANNED_IN_SCENE / scene_only。
#   它是「按词表按句删除模型写的场景描述」，属于语义级删改——会误删正常句子，
#   也是「针对某个场景做的特化」。场景现在原样使用，基调由编剧侧的提示词管。

def retail_caption(cap, has_text, has_boy):
    """**只换尾句**，正文一个字不动（2026-11-21 单 Agent 链路）。

    单 Agent 自己写的就是一整段连贯英文，不能像两段链路那样拆成五段重拼。
    但尾句是「本页到底有没有字、有没有男主画外音」的确定性事实，
    模型经常照抄模板却不写对应的 text item——尾句说有裸字、页面里一段都没有，
    模型收到自相矛盾的两句指令，画出来要么没字、要么给男生画气泡。

    做法：从最后一个 "flat screentone shading" 处截断（那是尾句的固定开头），
    换成按实际 text item 算出来的那一句。找不到就整段接在末尾。
    """
    if not has_text:
        tail = TAIL_NO_TEXT
    elif has_boy:
        tail = TAIL_WITH_BOY
    else:
        tail = TAIL_GIRL_ONLY
    cap = (cap or "").strip()
    i = cap.lower().rfind("flat screentone shading")
    if i <= 0:
        return (cap.rstrip(" .") + ". " + tail) if cap else tail
    head = cap[:i].rstrip().rstrip(",").rstrip(".")
    return (head + ". " + tail) if head else tail


def rebuild_summary(js, cast, layout, has_text, scene="", has_boy=False):
    """summary.caption 由代码拼装：分格句 + 画风锁 + 人物串 + 场景 + 尾句。
    前三段是常量或台账，第四段用**整章共用**的场景串（不是模型每页新写的），
    has_boy 决定尾句提不提画外男主——没有男主台词时一个 boy 字都不留。"""
    items = js.get("items") or []
    owners = set()
    for it in items:
        if it.get("label") == "character":
            ow = panel_owner(it)
            if ow:
                owners.add(ow)
    vals = [c for k, c in cast.items() if k in owners] or list(cast.values())
    cast_block = " ; ".join(v.rstrip(".") + "." for v in vals if v)
    # 场景串：编剧给的那一句原样用，不换词、不删词。
    # 它是「整章共用一句」这件事本身让画风不漂，不是靠代码洗词。
    scene_text = (scene or "").strip()
    if not scene_text:
        scene_text = ("Same location in every panel, fixed background objects, "
                      "hard directional light from one side.")

    s = js.setdefault("summary", {})
    s["meta"] = ["comic", "manga", "screentone"]
    # 单格页写 single：这是官方三值里「整页就是一个画面」的信号。
    # 写 multi_panel 会让模型按「分格页」重新切格，一页一格的意图就丢了。
    s["canvas"] = ("single" if len(SKELETONS.get(layout) or []) == 1
                   else "multi_panel")
    if not has_text:
        tail = TAIL_NO_TEXT
    else:
        tail = TAIL_WITH_BOY if has_boy else TAIL_GIRL_ONLY
    parts = [LAYOUT_SENTENCE.get(layout, ""), STYLE_LOCK, cast_block, scene_text, tail]
    s["caption"] = " ".join(p for p in parts if p).strip()


def autofix(js, cast=None, lines=None, layout=None, scene="", narr="",
            narr_cell=None, narrs=None, audit_narr=True, rebuild=True):
    """把模型不该负责的确定性部分接过来：整数化、台词按说话人落格、文字框按公式重排、
    否定句校正、顶层字段归位、外形串补齐。
    模型只管「写什么」；「放哪」和「多宽」一律由代码算。"""
    notes = []
    cast = cast or {}
    items = js.get("items") or []
    panels = [i for i in items if i.get("label") in ("character", "object")]

    # 台词 → 说话人（用剧本的标准答案，不信模型给的 bbox）
    smap = {}
    cell_of = {}                 # 逐格稿：台词 → 编剧写死的格号
    for i, ln in enumerate(lines or []):
        smap[norm_text(ln["text"])] = (ln["who"], i)
        if ln.get("cell"):
            cell_of[norm_text(ln["text"])] = int(ln["cell"])
    owner_panels = {}
    for p in panels:
        ow = panel_owner(p)
        if ow:
            owner_panels.setdefault(ow, []).append(p)

    # 0) 顶层坐标声明归位（模型有时会把它塞进 summary）
    s = js.get("summary") or {}
    for k in ("bbox_coord_space", "bbox_format"):
        if k not in js and k in s:
            js[k] = s.pop(k)
            notes.append("%s 从 summary 提到顶层" % k)
    # 这两个是**本链路自己的协议常量**，不是模型能选的东西：它写 absolute、
    # 写 yxyx 以外的顺序、或者干脆不写，下游都会把坐标读错。缺失或写错一律写死。
    if js.get("bbox_coord_space") != "abs_centralized":
        js["bbox_coord_space"] = "abs_centralized"
        notes.append("坐标空间按协议写死")
    if js.get("bbox_format") != "yxyx":
        js["bbox_format"] = "yxyx"
        notes.append("bbox 顺序按协议写死")
    s.pop("bbox_coord_space", None)
    s.pop("bbox_format", None)

    # 1) bbox 整数化
    for it in items:
        b = it.get("bbox") or []
        if len(b) >= 4:
            ib = [int(round(float(v))) for v in b[:4]]
            if ib != list(b[:4]):
                notes.append("bbox 取整")
            it["bbox"] = ib

    # 1.4) 剧本里写了、模型却漏掉的台词，由代码直接补成 text item。
    # 模型偶尔一条 text 都不写（整页变成无字页），重跑三次也未必补上；
    # 落格规则和 bbox 公式都是确定的，代码补出来比再花一次调用更便宜也更准。
    if lines and layout in SKELETONS:
        present = {norm_text(it.get("text") or "") for it in items
                   if it.get("label") == "text"}
        slots = line_slots(lines, layout)
        for i, ln in enumerate(lines):
            nt = norm_text(ln["text"])
            if nt in present:
                continue
            slot = slots[i]
            if not (1 <= slot <= len(SKELETONS[layout])):
                continue
            cell = list(SKELETONS[layout][slot - 1])
            is_boy = ln["who"] == "boy"
            items.append({
                "label": "text",
                "caption": (boy_text_cap(ln["text"]) if is_boy
                            else bubble_cap(ln["text"], ln["who"])),
                "text": ln["text"],
                "bbox": (bbox_plain(cell, 0) if is_boy else bbox_bubble(cell, 0)),
            })
            present.add(nt)
            notes.append("补漏台词「%s」→第%d格" % (ln["text"], slot))

    # 1.42) 气泡的 caption 一律由代码统一写（形状与方位，内容仍在 text 字段）。
    # 模型原来写的是「Rounded bubble low in the panel…」这类每页不同的话，
    # 十页十种措辞气泡形状就会跟着漂——按 SKILL §7 这本来就该是代码的活。
    line_by_text = {norm_text(ln["text"]): ln for ln in (lines or [])}
    for it in items:
        if it.get("label") != "text":
            continue
        ln = line_by_text.get(norm_text(it.get("text") or ""))
        if ln is None:
            continue
        is_boy = ln["who"] == "boy"
        want = boy_text_cap(ln["text"]) if is_boy else bubble_cap(ln["text"], ln["who"])
        if (it.get("caption") or "") != want:
            it["caption"] = want
            notes.append("男主白边裸字 caption 统一" if is_boy
                         else "气泡 caption 统一")

    # 1.45) 旁白：剧本那一句，位置由代码定。
    # 2026-11-21（实测那页之后改写）：模型**已经**写了同样这句旁白时，留它原位
    # （位置交给第 3 步），不再 append——之前是「多一个框」的结果，页面上同一句
    # 话出现两遍、还糊成两个框，读者看到的是一页乱码。
    # 模型写的旁白框里，script 没有的那句才删：它会真印在画面上，变成剧本里
    # 不存在的一句话。判据只看「caption 类 + 是不是剧本里的台词」，不做语义判断。
    narr = (narr or "").strip()
    # 逐格稿：每格旁白各落各框。旧调用只传 narr，退回单句列表。
    want = []
    if narrs:
        for e in narrs:
            t = str((e.get("text") if isinstance(e, dict) else e) or "").strip()
            if not t:
                continue
            c = e.get("cell") if isinstance(e, dict) else narr_cell
            want.append({"text": t, "cell": c})
    elif narr:
        want = [{"text": narr, "cell": narr_cell}]
    want_keys = {norm_text(e["text"]) for e in want}
    line_keys = {norm_text(ln.get("text")) for ln in (lines or [])}
    cap_caption = CAPTION_BOX
    # 模型写了的旁白框按 text 认领（同一句只留一个），剧本没有的删掉。
    # 2026-11-21：单 Agent 链路**没有上游剧本**，模型写的旁白就是唯一事实源。
    # `audit_narr=False` 时跳过「剧本没有就删」——否则这一页 5 句旁白会被删成 0 句，
    # 出来的页只剩一句台词，读起来就是叙事缺失。措辞仍然统一，只是不再删内容。
    have = {}                       # norm_text -> item
    drop = []
    if audit_narr:
        for it in items:
            if it.get("label") != "text" or _kind(it.get("caption")) != "caption":
                continue
            k = norm_text(it.get("text"))
            if k in want_keys:
                if k not in have:
                    have[k] = it
                else:
                    drop.append(it)     # 同一句出现两次，留第一个
                continue
            if k and k not in line_keys:
                drop.append(it)         # 剧本没有这句且不是台词 → 模型自己编的
        for it in drop:
            items.remove(it)
            notes.append("删多余旁白框")
        for it in have.values():
            it["caption"] = cap_caption  # 措辞统一，避免十页十种形状
    else:
        # 没有剧本可对账：旁白只统一措辞，一句不删
        for it in items:
            if it.get("label") == "text" and _kind(it.get("caption")) == "caption":
                it["caption"] = cap_caption
    # 剧本写了、模型没画的旁白：补框，落到它自己那一格。
    for e in want:
        nk = norm_text(e["text"])
        if nk in have:
            continue
        if layout in SKELETONS:
            slots0 = line_slots(lines or [], layout)
            lp = LAYOUT_SLOTS.get(layout) or LAYOUT_SLOTS["L0"]
            try:
                slot = int(e["cell"]) if e.get("cell") else (
                    slots0[0] if slots0 else lp["main"])
            except (ValueError, TypeError):
                slot = slots0[0] if slots0 else lp["main"]
            slot = max(1, min(slot, len(SKELETONS[layout])))
            cell = list(SKELETONS[layout][slot - 1])
            items.append({
                "label": "text",
                "caption": cap_caption,
                "text": e["text"],
                "bbox": bbox_caption(cell, 0),
            })
            notes.append("旁白落第%d格左上" % slot)

    # 1.5) 只删**没有字的** text item：空框会被画成一个空白气泡，100% 是坏结果。
    # 2026-11-21 不再删「重复台词」——重复可能是刻意的呼应，删不删由编剧决定。
    kept = []
    for it in items:
        if it.get("label") == "text" and not (it.get("text") or "").strip():
            notes.append("删无字气泡")
            continue
        kept.append(it)
    if len(kept) != len(items):
        items[:] = kept

    # 2) 文字元素归到所属格：气泡按说话人归，**绝不落进顶部建立镜头条**
    # 顶条 = 「禁人禁字」的建场格。只有 L0/L1/L3 的槽位表把它标成 scene；
    # S1 / D1 / D2 没有顶条，若沿用「取第 1 格」，单格页会把唯一的气泡格
    # 当成顶条排除掉，气泡就无处可落。
    _scene_slot = (LAYOUT_SLOTS.get(layout) or {}).get("scene")
    TOP = (tuple(SKELETONS[layout][_scene_slot - 1])
           if (layout in SKELETONS and _scene_slot) else None)
    groups = {}
    for t in items:
        if t.get("label") != "text":
            continue
        host = None
        who_idx = smap.get(norm_text(t.get("text")))
        if who_idx:
            t["_ord"] = who_idx[1]
        # 逐格稿：台词自带格号，直接落它写的那一格，不信模型给的 bbox。
        cell_no = cell_of.get(norm_text(t.get("text")))
        if cell_no and layout in SKELETONS and 1 <= cell_no <= len(SKELETONS[layout]):
            cb = tuple(SKELETONS[layout][cell_no - 1])
            for p in panels:
                if tuple(p["bbox"]) == cb:
                    host = p
                    break
        if who_idx and host is None:
            # 候选只能是**画人的格**：只有手的道具特写格也含 "the boy"，
            # 一旦落进去，气泡就挂在一只手上——这就是「没指向正确角色」。
            cands = [p for p in owner_panels.get(who_idx[0], [])
                     if p.get("label") == "character"
                     and (TOP is None or tuple(p["bbox"]) != TOP)]
            if not cands:
                cands = [p for p in panels
                         if p.get("label") == "character"
                         and (TOP is None or tuple(p["bbox"]) != TOP)
                         and panel_owner(p) == who_idx[0]]
            if cands:
                # 有特写就先给特写：特写格里气泡才贴得住脸
                host = sorted(cands, key=lambda p: 0 if re.search(
                    r"close[- ]up", p.get("caption") or "", re.I) else 1)[0]
                t["_ord"] = who_idx[1]
                if _kind(t.get("caption")) == "bubble":
                    notes.append("台词按说话人归格")
        if host is None:
            tb = t["bbox"]
            cy, cx = (tb[0] + tb[2]) / 2, (tb[1] + tb[3]) / 2
            for p in panels:
                if (TOP is not None and tuple(p["bbox"]) == TOP
                        and _kind(t.get("caption")) != "caption"):
                    continue          # 顶条只放空镜：气泡不许进，旁白框可以进
                pb = p["bbox"]
                if pb[0] <= cy <= pb[2] and pb[1] <= cx <= pb[3]:
                    host = p
                    break
        if host is None and panels:
            ok = [p for p in panels if TOP is None or tuple(p["bbox"]) != TOP] or panels
            host = max(ok, key=lambda p: (p["bbox"][2] - p["bbox"][0])
                       * (p["bbox"][3] - p["bbox"][1]))
            notes.append("文字框落在格外，归入最大格")
        if host is None:
            continue
        groups.setdefault(tuple(host["bbox"]), []).append(t)

    # 3) 按公式重算 bbox（气泡自下往上叠、旁白自上往下、拟声居中）
    for cell, ts in groups.items():
        kinds = {"bubble": [], "plain": [], "caption": [], "sfx": []}
        for t in ts:
            kinds[_kind(t.get("caption"))].append(t)
        # 气泡：后说的贴最下（idx 0），先说的往上叠
        if all(t.get("_ord") is not None for t in kinds["bubble"]):
            kinds["bubble"].sort(key=lambda t: t["_ord"], reverse=True)
        else:
            kinds["bubble"].sort(key=lambda t: t["bbox"][0], reverse=True)
        # 同格有男主裸字时：裸字占上半，气泡最多两条只在下半，绝不叠。
        # 没有裸字时气泡放宽到四条——高密度小漫画一格三四个气泡是常态。
        bubble_max = 2 if kinds["plain"] else 4
        for i, t in enumerate(kinds["bubble"]):
            if i >= bubble_max:
                notes.append("同格裸字+气泡超量，第%d条气泡保留原位置" % (i + 1))
                continue
            t["bbox"] = bbox_bubble(list(cell), i)
        # 男主白边裸字：贴右上，和贴右下的气泡上下错开；顺序与气泡同理
        if all(t.get("_ord") is not None for t in kinds["plain"]):
            kinds["plain"].sort(key=lambda t: t["_ord"], reverse=True)
        else:
            kinds["plain"].sort(key=lambda t: t["bbox"][0], reverse=True)
        for i, t in enumerate(kinds["plain"]):
            if i >= 2:
                break
            t["bbox"] = bbox_plain(list(cell), i)
        kinds["caption"].sort(key=lambda t: t["bbox"][0])
        for i, t in enumerate(kinds["caption"]):
            if i >= 2:
                break
            t["bbox"] = bbox_caption(list(cell), i)
        if kinds["sfx"]:
            kinds["sfx"][0]["bbox"] = bbox_sfx(list(cell))
    if groups:
        notes.append("按公式重排 %d 格的文字框" % len(groups))

    # 4) 否定句校正（判据只看格里有没有 text item）
    for p in panels:
        pb = p["bbox"]
        inside = [t for t in items if t.get("label") == "text"
                  and pb[0] <= (t["bbox"][0] + t["bbox"][2]) / 2 <= pb[2]
                  and pb[1] <= (t["bbox"][1] + t["bbox"][3]) / 2 <= pb[3]]
        cap = p.get("caption") or ""
        has_neg = "no speech bubble and no text in this panel" in cap.lower()
        if inside and has_neg:
            p["caption"] = re.sub(r",?\s*No speech bubble and no text in this panel\.?\s*",
                                  "", cap, flags=re.I).strip()
            notes.append("有字格删否定句")
        elif (not inside) and (not has_neg):
            p["caption"] = cap.rstrip().rstrip(".") + ". " + NEG_SENT
            notes.append("空格补否定句")

    # 5) 外形串补齐：模型漏抄时按台账原串确定性地插回去
    for it in items:
        if it.get("label") != "character":
            continue
        cap = it.get("caption") or ""
        low = cap.lower()
        m = re.match(r".*?\b(boy|girl)\b", low)
        who = m.group(1) if m else None
        if not who or who not in cast:
            continue
        # 2026-11-21 POV：画面里只有女生。绝不把 boy 外形串补进 caption——
        # 补了就等于把男主画进画面。男主只以 pov hands 出现，没有外形串。
        if who == "boy":
            continue
        clauses = [x.strip() for x in cast[who].split(",") if len(x.split()) >= 2]
        if not clauses:
            continue
        hit = sum(1 for x in clauses if x.lower() in low)
        if hit / len(clauses) >= 0.6:
            continue
        marker = "monochrome greyscale screentone"
        idx = low.find(marker)
        if idx != -1:
            it["caption"] = cap[:idx] + cast[who] + ", " + cap[idx:]
        else:
            it["caption"] = cap.rstrip().rstrip(".") + ", " + cast[who] + "."
        notes.append("补齐 %s 外形串" % who)

    # 6) 只做字段卫生：清临时字段、清空 text、清掉模型自己加的括注。
    #    2026-11-21 拆掉的三条：
    #      · 收紧景别（extreme/tight close-up → medium close-up）
    #      · 裙子→裤子
    #      · 统一人物画风短句（删掉模型写的画风句，换成代码那句）
    #    三条都在改模型写出来的词，是「针对某个场景的特化」，换场景就误伤。
    #    画风一致性由 summary 里的 STYLE_LOCK 一处负责，不需要在每个人物串上再写一遍。
    for it in items:
        it.pop("_ord", None)
        if it.get("label") != "text" and "text" in it:
            it.pop("text")
            notes.append("清理空 text 字段")
        cap = it.get("caption")
        if not isinstance(cap, str) or not cap:
            continue
        cleaned = re.sub(r"\s*\((?:use|note|i\.e\.|e\.g\.)[^)]*\)", "", cap, flags=re.I)
        if cleaned != cap:
            it["caption"] = re.sub(r"\s{2,}", " ", cleaned).strip()
            notes.append("清理括注")
    sc = (js.get("summary") or {}).get("caption")
    if isinstance(sc, str):
        cleaned = re.sub(r"\s*\((?:use|note|i\.e\.|e\.g\.)[^)]*\)", "", sc, flags=re.I)
        if cleaned != sc:
            js["summary"]["caption"] = cleaned

    # 7) summary 整体重建：分格句与画风段全部由代码给，模型只贡献场景
    # 2026-11-21：单 Agent 链路传 rebuild=False——它自己写的就是一整段连贯英文
    # （分格→画风→主格人物→场景→文字），拆成五段再拼回去反而更差。
    if not rebuild:
        # 单 Agent：正文保留模型写的一整段，只校尾句（见 retail_caption）。
        has_text = any((t.get("text") or "").strip() for t in items if t.get("label") == "text")
        has_boy = any("boy plain text" in (it.get("caption") or "").lower()
                      for it in items if it.get("label") == "text")
        s = js.setdefault("summary", {})
        old = s.get("caption") or ""
        new = retail_caption(old, has_text, has_boy)
        if new != old:
            s["caption"] = new
            notes.append("改尾句（按本页实际文字）")
        return notes
    has_text = any((t.get("text") or "").strip() for t in items if t.get("label") == "text")
    has_boy = any((ln.get("who") or "") == "boy" for ln in (lines or []))
    if not lines:
        # 单 Agent 链路没有剧本台词表：说话人只能从已经落进页面的 text item 认。
        # 不认的话尾句永远写成「no bare text」，而页面里明明有一段白边裸字，
        # 模型收到自相矛盾的两句指令，画出来就是要么没字、要么给男生画气泡。
        has_boy = any("boy plain text" in (it.get("caption") or "").lower()
                      for it in items if it.get("label") == "text")
    rebuild_summary(js, cast, layout, has_text, scene, has_boy=has_boy)
    notes.append("重建 summary（画风锁由代码写死）")

    return notes


def check_page(js, page_n, cast, layout=None, lines=None):
    """只查「100% 会坏、且不会误判」的结构事实。**不判内容好坏，不触发重跑。**

    2026-11-21 拆掉的内容类检查（色相词 / 外形复用率 60% / 台词字数 / 拟声个数 /
    否定句一致性 / 人物画风锁 / summary 画风锁与分格句 / 台词落错格）：
    它们都在判「AI 写得对不对」，而判据是词表。判错的代价是整页重跑
    （每页最坏 3 次 × 12000 token），发色事故就是这类判据把正确答案判成错。
    留下的几条全是几何或协议事实：矩形相交就是相交，汉字在英文 prompt 里就是无效。
    """
    issues = []

    # 出图端按这两个字段解析坐标，值不对整页切图错位
    if js.get("bbox_coord_space") != "abs_centralized":
        issues.append("bbox_coord_space 不是 abs_centralized")
    if js.get("bbox_format") != "yxyx":
        issues.append("bbox_format 不是 yxyx")
    if str((js.get("summary") or {}).get("canvas")) not in ("multi_panel", "single", "collage"):
        issues.append("canvas 不在官方三值（single/multi_panel/collage）")

    items = js.get("items") or []
    if not items:
        issues.append("items 为空")
        return issues

    for it in items:
        b = it.get("bbox")
        if not (isinstance(b, list) and len(b) == 4):
            issues.append("bbox 格式错: %r" % (b,))
            continue
        y0, x0, y1, x1 = b
        if not (-768 <= y0 < y1 <= 768):
            issues.append("y 越界 %s (%s)" % (b, it.get("label")))
        if not (-512 <= x0 < x1 <= 512):
            issues.append("x 越界 %s (%s)" % (b, it.get("label")))

    # 中文进 SD prompt = 整页失效。汉字检测不依赖任何词表，不会误伤英文。
    for it in items:
        if re.search(r"[\u4e00-\u9fff]", it.get("caption") or ""):
            issues.append("caption 含中文：%s" % (it.get("caption") or "")[:30])

    # text item 必须有 text：空文字框会被画成一个空白气泡
    for it in items:
        if it.get("label") == "text" and not (it.get("text") or "").strip():
            issues.append("text item 没有 text 字段")

    # 分格相交：纯几何，没有判断余地。重叠会在切图时互相咬掉。
    panels = [i for i in items if i.get("label") in ("character", "object")]
    for i in range(len(panels)):
        for j in range(i + 1, len(panels)):
            a, b = panels[i]["bbox"], panels[j]["bbox"]
            oy = min(a[2], b[2]) - max(a[0], b[0])
            ox = min(a[3], b[3]) - max(a[1], b[1])
            if oy > 8 and ox > 8:
                issues.append("分格重叠 %s / %s" % (a, b))

    return issues


# ────────────────────── 主流程 ──────────────────────
BRIEF = ("一直暗恋着的青梅竹马，好像喜欢上了别人——我会赢吗？")


# ──────────────────────────────────────────────────────────
#  台子接入：一次请求产出整章 spec（编剧 → 逐页编译 → 闸门）
# ──────────────────────────────────────────────────────────
def layout_to_panels(js):
    """Layout JSON → 台子 spec.panels。

    只给页面显示 / 人工微调用；真正出图走 `prompt_override`，不重新拼提示词。
    """
    items = js.get("items") or []
    frames = [i for i in items if i.get("label") in ("character", "object")]
    texts = [i for i in items if i.get("label") == "text"]
    out = []
    for f in frames:
        b = list(f.get("bbox") or [0, 0, 0, 0])[:4]
        if len(b) < 4:
            b = [0, 0, 0, 0]
        y0, x0, y1, x1 = b
        bub, cap, sfx = [], "", ""
        for t in texts:
            tb = list(t.get("bbox") or [0, 0, 0, 0])[:4]
            if len(tb) < 4:
                continue
            # bbox 是 abs_centralized 的 yxyx：拿**文字框中心**落进哪一格
            ty, tx = (tb[0] + tb[2]) / 2.0, (tb[1] + tb[3]) / 2.0
            if not (y0 <= ty <= y1 and x0 <= tx <= x1):
                continue
            txt = " ".join(str(t.get("text") or "").split()).strip()
            if not txt:
                continue
            note = str(t.get("caption") or "").lower()
            if "no bubble tail" in note:
                cap = (cap + " " + txt).strip()
            elif "no bubble" in note:
                sfx = (sfx + " " + txt).strip()
            else:
                bub.append(txt)
        out.append({"desc": " ".join(str(f.get("caption") or "").split()).strip(),
                    "bubbles": bub, "caption": cap, "sfx": sfx})
    return out


def _plan_from(js):
    """从 Layout JSON 的格描述里抽景别，补一份 `plan.shots`。

    `moss_comic.check()` 会报「没有 plan / 景别单一」，这份体检报告是给页面看的。
    抽的是模型真写出来的景别词，不是编的默认值——所以它同时也是一层校对。
    """
    shots = []
    for it in js.get("items") or []:
        if it.get("label") not in ("character", "object"):
            continue
        c = str(it.get("caption") or "").lower()
        if "full-body" in c or "full body" in c:
            shots.append("full")
        elif "close-up" in c:
            shots.append("close-up")
        elif "prop" in c or "no face" in c:
            shots.append("object")
        elif "wide" in c or "establishing" in c:
            shots.append("wide")
        elif "medium" in c:
            shots.append("medium")
        else:
            shots.append("medium")
    return {"shots": shots}


# ──────────────────────────────────────────────────────────
#  剧本闸门的取舍（2026-11-21）
# ──────────────────────────────────────────────────────────
# 这里原来有 check_script()：查「台词跨幕重复」「缺既成事实」「台账丢了用户写的外形」，
# 不过就把问题清单拼进 user 让编剧重写一次。三条判据都是词表式的：
#   · 「重复」可能是有意的呼应，不是错；
#   · 「既成事实」是格式字段，缺了不代表剧情断；
#   · 「外形词在不在」靠一张中英对照表，用户换一种说法就漏。
# 判错一次 = 多一次 1100×pages token 的重写，而且重写出来的不保证更好。
# 这些全部交给 SCRIPT_SYSTEM 的提示词约束，代码不再判。




def _layout_for(grid, prev_layout=None, want=""):
    """页的格数 → 骨架。**格数由这一页的拍数决定，骨架只是容器。**

    2026-11-21（B 层）：原来只有 L0–L5（3~6 格），编剧就算判断「这一页只有一拍」，
    也没有一格骨架可用，只能往空格子里派内容。这里把 1 格/2 格接进同一条链路。

    1 格 → S1（满版）；2 格 → D1 上下 / D2 左右（交替避免连撞）；
    3 格 → T3（上大横格 + 下两格，2026-11-21 替换掉 L5 的 1:3.1 窄条）；
    5 格 → L0。4 格不再产出（用户明确不要 4 格）。
    编剧若显式写了合法版式，以它为准。
    """
    w = re.sub(r"\s+", "", (want or "")).upper()
    if w in SKELETONS:
        return w
    g = int(grid or 0)
    if g <= 1:
        return "S1"
    if g == 2:
        return "D2" if prev_layout == "D1" else "D1"
    if g == 3:
        return "T3"
    if g == 4:
        # 4 格没有独立骨架：按「上主格 + 下两格」收一格，不让它凭空多一格。
        return "T3"
    if g >= 6:
        return "L3"
    # 5 格：单 Agent 只出 L0（顶条 + 右通高主格 + 左三小，用户认可的那套）。
    return "L0"


def generate(brief, pages, call, progress=None):
    """编剧 AI → 逐页 MOSS 编译 → 台子 spec。

    `call(system, user, max_tokens=..., temperature=..., tag="") -> str`
    返回 `(spec, report)`；spec 每页带 `prompt_override`（整页 Layout JSON）。

    2026-11-21（A 层）：`pages` 是**上限**不是任务量。实际页数由编剧的节拍表推导，
    少于上限就按实际跑；超了上限才截断。原来「编剧只拆出 N 幕 < 要求页」直接抛
    RuntimeError，等于逼它把片段摊成 N 页——这是「凑剧情」的源头之一。
    """
    say = progress or (lambda msg: None)
    t0 = time.time()
    # 2026-11-21（B 层）：不再把 agent_my_test 的 moss-comic-script 挂进逐页 system。
    # 那份 skill 写的是「一页顺着五拍走」「L0 是日常默认」「主画面+三小反应」，
    # 骨架库也只有 L0/L1——与本文件的 SKELETONS（含 1 格 S1 / 2 格 D1·D2）不同源。
    # 挂上去等于每页都在跟它打架：用户要「一页一格」，它先验要求「一页五格」。
    # 逐页口径现在只有一处：MOSS_CONTRACT + build_moss_user 里指定的骨架。
    system = MOSS_CONTRACT

    cap = max(1, int(pages or 1))
    say("编剧 AI 拆节拍（上限 %d 页）…" % cap)
    user = build_script_user(brief, cap)
    script_md = call(SCRIPT_SYSTEM, user,
                     max_tokens=max(6000, 1100 * cap),
                     temperature=0.85, tag="script") or ""
    script = parse_script(script_md, brief)

    # 2026-11-21 拆掉「剧本过闸门就重写一次」：
    # 那一步的判据是词表式的（台词重复 / 缺既成事实 / 外形词缺失），
    # 误判一次就多烧一次 1100×pages token，重写出来的还不保证更好。
    # 剧本质量交给 SCRIPT_SYSTEM 的提示词与用户的判断，代码不再判。

    # 场景串最多两条：逐条把中文翻成英文（每条只用一次调用）。
    scenes = script.get("scenes") or {}
    for key, txt in list(scenes.items()):
        if not re.search(r"[\u4e00-\u9fff]", txt or ""):
            continue
        say("场景 %s 写成了中文，单独翻一次" % key)
        en = call(
            "You translate a Chinese scene note into ONE short English line.\n"
            "Output ONLY that English line, 15-30 words, no quotes, no explanation.\n"
            "Describe: the location + two or three fixed objects that never move + "
            "the direction and hardness of the light.\n"
            "Do NOT mention time of day, color temperature, or mood words "
            "(no warm/cool/golden/soft/glow/hazy/sunset/dusk/evening).",
            "中文场景：\n%s" % txt, max_tokens=200,
            temperature=0.2, tag="scene_en") or ""
        scenes[key] = ((en.strip().split("\n")[0] or "").strip(' "')).strip()
    script["scenes"] = scenes
    script["scene"] = scenes.get("1") or script.get("scene") or ""
    # 外形串原样使用：不锁体态、不锁下半身、不换任何词
    script["cast_text"] = "\n".join("- %s: %s" % (k, v)
                                    for k, v in script["cast"].items())

    acts = script.get("acts") or []
    if not acts:
        raise RuntimeError("编剧没有产出任何一页")
    if len(acts) > cap:
        say("编剧写了 %d 页，超过上限 %d，按上限截断" % (len(acts), cap))
        acts = acts[:cap]
        script["acts"] = acts
    total = len(acts)
    if total < cap:
        say("节拍只够 %d 页（上限 %d），按 %d 页出" % (total, cap, total))
    # 每页带上它自己的场景串（换地点换的是哪一句，不是每页重新措辞）
    for a in acts:
        a["scene_text"] = (scenes.get(a.get("scene_no") or "1")
                           or script.get("scene") or "")

    # 结构核对（不判内容，只比数字）：
    #   ① 这一页承载几拍，就该有几格；
    #   ② 节拍表里的每一拍，最后都要有页认领。
    # 两条都是纯计数，误判不了，所以只写进 report 供页面显示，不重跑。
    beat_nums_in_table = [int(m.group(1)) for m in
                          re.finditer(r"^\s*\|\s*(\d+)\s*\|", script.get("beats") or "",
                                      re.M)]
    claimed = set()
    struct_notes = []
    for a in acts:
        if a.get("grid") and a.get("beat_nums") and len(a["beat_nums"]) != a["grid"]:
            struct_notes.append(
                "第 %d 页：格数 %d ≠ 承载拍数 %d（%s）"
                % (a["n"], a["grid"], len(a["beat_nums"]), a["beat"]))
        claimed.update(a.get("beat_nums") or [])
        # 逐格稿的结构事实：某格没写画面 / 一页写了多句旁白（现在只有一条 caption 通道）
        cells = a.get("cells") or {}
        if cells:
            empty = [k for k in sorted(cells) if not cells[k]["visual"]]
            if empty:
                struct_notes.append("第 %d 页：格 %s 没写画面"
                                    % (a["n"], "、".join(str(x) for x in empty)))
            # 逐格旁白现在每格各画一框（2026-11-21），不再只画第一句。
            # 只有格数超出骨架可容纳的 caption 数才算结构问题——交给 check_page
            # 的几何校验，这里不再按「只画第一句」报过时提示。
    if beat_nums_in_table:
        missing = [x for x in beat_nums_in_table if x not in claimed]
        if missing:
            struct_notes.append("节拍表里没有页认领的拍：%s"
                                % "、".join("第%d拍" % x for x in missing))
    if struct_notes:
        say("结构核对：%s" % "；".join(struct_notes))

    out_pages, report = [], []
    prev_state, prev_layout = None, None
    for n in range(1, total + 1):
        act = acts[n - 1]
        want = (act.get("layout") or "").strip().upper()
        must = _layout_for(act.get("grid") or 0, prev_layout, want)
        say("第 %d/%d 页编译（%d 格 → %s）…"
            % (n, total, len(SKELETONS.get(must) or []), must))
        js, issues, notes = None, [], []
        # 只重试「没解析出 JSON」这一种情况（模型输出坏了，只能再问一次）。
        # 2026-11-21 拆掉「有错就重跑 3 次」：那是按中文 issue 文案做字符串匹配判错，
        # 误判一次 = 3×12000 token，而且重跑出来的东西不保证更好。
        for attempt in range(2):
            raw = call(system,
                       build_moss_user(script, n, prev_state, prev_layout, must),
                       max_tokens=12000,
                       temperature=0.6 - 0.15 * attempt,
                       tag=("p%d" % n) if attempt == 0 else "p%d_r%d" % (n, attempt)) or ""
            try:
                js = pick_json(raw)
                break
            except Exception:                   # noqa: BLE001
                js = None
                say("第 %d 页没解析出 JSON，再问一次" % n)
        if js is None:
            raise RuntimeError("第 %d 页两次都没解析出 JSON" % n)

        notes = autofix(js, script["cast"], act.get("lines"),
                        must, act.get("scene_text") or "",
                        act.get("narr") or "", act.get("narr_cell"),
                        narrs=act.get("narrs"))
        issues = check_page(js, n, script["cast"], must, act.get("lines"))
        used = detect_layout(js)
        if used != must:
            # 只报告，不重跑：版式是结构信息，给页面标出来就够了
            issues.append("版式不符：要求 %s，实际 %s（仅提示，未重跑）"
                          % (must, used or "自拼格子"))

        pure = {k: v for k, v in js.items() if not k.startswith("_")}
        out_pages.append({
            "n": n,
            "layout": must,
            "grid": len(SKELETONS.get(must) or []),
            "beat": act.get("beat") or "",
            "core": act.get("core") or "",
            "hook": act.get("hook") or "",
            "scene": act.get("scene_text") or "",
            "plan": _plan_from(js),
            "panels": layout_to_panels(js),
            "prompt_override": json.dumps(pure, ensure_ascii=False),
        })
        report.append({"n": n, "layout": must,
                       "grid": len(SKELETONS.get(must) or []),
                       "beat": act.get("beat") or "",
                       "issues": issues, "autofix": notes})
        prev_state = page_exit_state(act)
        prev_layout = detect_layout(js) or prev_layout

    title = (script.get("title") or "").strip()
    # 编剧偶尔把格式说明里的占位照抄成标题，这里兜回来
    if title in ("标题", "<标题>") or "不要照抄" in title or len(title) > 18:
        title = ""
    spec = {"title": title or (brief[:10] or "未命名"),
            "style": script.get("scene") or "",
            "cast": script["cast"],
            "pages": out_pages}
    return spec, {"script_md": script_md, "pages": report,
                  "script_issues": struct_notes,
                  "secs": round(time.time() - t0, 1)}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    env = load_env(ENV_PATH)
    key = env.get("DEEPSEEK_API_KEY", "")
    base = env.get("DEEPSEEK_BASE_URL", "")
    model = env.get("DEEPSEEK_MODEL", "")
    if not (key and base and model):
        print("缺少 DEEPSEEK 配置，停止")
        return 2
    print("model=%s  base=%s  key=***%s" % (model, base, key[-4:]))

    llm = LLM(base, key, model)
    # 旧 moss-comic-script 不再挂载（见 generate 里的说明：它写死「一页五拍 / L0 默认」）

    # ── 第 1 步：编剧 ──
    script_path = os.path.join(OUT_DIR, "script.md")
    if "--reuse" in sys.argv[1:] and os.path.exists(script_path):
        print("\n[1/3] 复用已有剧本 script.md（省一次调用）")
        with open(script_path, encoding="utf-8") as f:
            script_md = f.read()
    else:
        print("\n[1/3] 编剧 AI 生成分幕剧本…")
        script_md = llm(SCRIPT_SYSTEM, build_script_user(BRIEF, PAGES),
                        max_tokens=6000, temperature=0.85, tag="script")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_md)
    script = parse_script(script_md, brief)
    # 场景串最多两条，逐条翻（每条一次调用）。同一场景的页共用同一句。
    scenes = script.get("scenes") or {}
    for key, txt in list(scenes.items()):
        if not re.search(r"[\u4e00-\u9fff]", txt or ""):
            continue
        print("  场景 %s 写成了中文，单独翻译一次（不动剧本其余内容）" % key)
        en = llm(
            "You translate a Chinese scene note into ONE short English line.\n"
            "Output ONLY that English line, 15-30 words, no quotes, no explanation.\n"
            "Describe: the location + two or three fixed objects that never move + "
            "the direction and hardness of the light.\n"
            "Do NOT mention time of day, color temperature, or mood words "
            "(no warm/cool/golden/soft/glow/hazy/sunset/dusk/evening).",
            "中文场景：\n%s" % txt, max_tokens=200, temperature=0.2,
            tag="scene_en")
        line = (en or "").strip().split("\n")[0].strip(' "')
        scenes[key] = line
        print("  场景 %s（英文）：%s" % (key, line or "(空)"))
    script["scenes"] = scenes
    script["scene"] = scenes.get("1") or script.get("scene") or ""
    for a in script.get("acts") or []:
        a["scene_text"] = (scenes.get(a.get("scene_no") or "1")
                           or script.get("scene") or "")
    # 外形串原样使用（2026-11-21：不再套体型锁、不再锁下半身）
    script["cast_text"] = "\n".join("- %s: %s" % (k, v) for k, v in script["cast"].items())
    print("  台账（原样）：")
    for k, v in script["cast"].items():
        print("    %s: %s" % (k, v))
    print("  解析：%d 页，角色 %s" % (len(script["acts"]), list(script["cast"].keys())))
    if len(script["acts"]) < PAGES:
        print("  节拍只够 %d 页（上限 %d），按 %d 页出"
              % (len(script["acts"]), PAGES, len(script["acts"])))

    # ── 第 2 步：逐页编译（无状态调用，固定前缀=契约） ──
    # 不再挂旧 moss-comic-script（它写死「一页五拍 / L0 默认」，与 SKELETONS 不同源）。
    system = MOSS_CONTRACT
    print("\n[2/3] MOSS 编译 AI 逐幕生成 Layout JSON（固定前缀 %d 字符）…" % len(system))

    pages = []
    prev_state = None
    prev_layout = None
    # 格数由这一页的拍数决定（见 _layout_for）。编剧若显式给了版式，以它为准。
    total = min(len(script.get("acts") or []), PAGES)
    for n in range(1, total + 1):
        print("\n  ── 第 %d 页 ──" % n)
        # 只重试「没解析出 JSON」；不再按 issue 文案重跑（2026-11-21 拆掉）
        js, issues, notes = None, [], []
        want = (script["acts"][n - 1].get("layout") or "").strip().upper()
        must = _layout_for(script["acts"][n - 1].get("grid") or 0, prev_layout, want)
        print("  版式：编剧选 %s → 采用 %s" % (want or "(未给)", must))
        for attempt in range(2):
            tag = "moss_p%d" % n if attempt == 0 else "moss_p%d_r%d" % (n, attempt)
            raw = llm(system,
                      build_moss_user(script, n, prev_state, prev_layout, must),
                      max_tokens=12000, temperature=0.6 - 0.15 * attempt, tag=tag)
            try:
                js = pick_json(raw)
                break
            except Exception as e:
                print("  ✗ JSON 解析失败：%s" % e)
                with open(os.path.join(OUT_DIR, "page%d.raw.txt" % n), "w",
                          encoding="utf-8") as f:
                    f.write(raw)
                js = None
        if js is None:
            return 4

        notes = autofix(js, script["cast"], script["acts"][n - 1].get("lines"),
                        must, script["acts"][n - 1].get("scene_text") or "",
                        script["acts"][n - 1].get("narr") or "",
                        script["acts"][n - 1].get("narr_cell"),
                        narrs=script["acts"][n - 1].get("narrs"))
        if notes:
            print("  代码修正 %d 项：%s" % (len(notes), "；".join(notes[:5])))
        issues = check_page(js, n, script["cast"], must,
                            script["acts"][n - 1].get("lines"))
        used = detect_layout(js)
        if used != must:
            issues.append("版式不符：要求 %s，实际 %s（仅提示，未重跑）"
                          % (must, used or "自拼格子"))

        if issues:
            print("  ⚠ 结构检查 %d 条（只提示，不重跑）：" % len(issues))
            for s in issues[:8]:
                print("    - " + s)
        else:
            print("  ✓ 结构检查通过")

        js["_checked"] = {"page": n, "issues": issues, "autofix": notes}
        pages.append(js)
        # 落盘给跑图端的是纯净 JSON：不带任何 _ 开头的诊断字段
        pure = {k: v for k, v in js.items() if not k.startswith("_")}
        with open(os.path.join(OUT_DIR, "page%d.json" % n), "w", encoding="utf-8") as f:
            json.dump(pure, f, ensure_ascii=False, indent=2)

        prev_state = page_exit_state(script["acts"][n - 1])
        used = detect_layout(js)
        print("  识别版式：%s（要求 %s）" % (used or "未识别", must))
        prev_layout = used or prev_layout

    # ── 第 3 步：落盘校验报告 ──
    total_issues = sum(len(p["_checked"]["issues"]) for p in pages)
    with open(os.path.join(OUT_DIR, "check.txt"), "w", encoding="utf-8") as f:
        for p in pages:
            f.write("page %d：%d 条问题\n" % (p["_checked"]["page"],
                                              len(p["_checked"]["issues"])))
            for s in p["_checked"]["issues"]:
                f.write("  - " + s + "\n")
        f.write("\n合计 %d 条\n" % total_issues)

    with open(os.path.join(OUT_DIR, "usage.json"), "w", encoding="utf-8") as f:
        json.dump(llm.usage, f, ensure_ascii=False, indent=2)

    print("\n[3/3] 完成。产物在 %s" % OUT_DIR)
    print("  合计闸门问题 %d 条（见 check.txt）" % total_issues)
    total_out = sum(u["completion"] or 0 for u in llm.usage)
    total_in = sum(u["prompt"] or 0 for u in llm.usage)
    print("  本次 token：输入 %s / 输出 %s" % (total_in, total_out))
    return 0 if total_issues == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

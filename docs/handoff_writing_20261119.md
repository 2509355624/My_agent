# 角色扮演写作线交接文档（2026-11-19）

> 给下一个接手的 AI。本文档是当前任务的全部记忆：现状、用户已驳回的做法、
> 用户要的验收标准、所有原文路径。先读完「已驳回」和「验收标准」再动手，
> 不要重蹈覆辙。开工前先用一句话向用户复述你理解的问题，等确认再改。

## 0. 一句话现状

写作规范 + 范文已经换过三轮（be4cd6b → dbd40db → 6b28b76），**全部被用户驳回**。
代码已提交推送，但内容不合格，需要按下面的验收标准重做。
用户已明确：这条线交给另一个 AI 做，本文档是交接记忆。

## 1. 职责分工（用户定死，不许动）

- **编剧 AI**：只输出状态更新 JSON。不做意图解析，不输出「他是谁/能走到哪一步/短板/角色边界」这类硬编码闸门。
- **写作 AI**：只输出正文（+ 画面/服饰/心情/状态四行元数据由外层契约管）。意图理解由写作 AI 自己做。
- **情感 AI、地点/小记/环境、画面 AI**：现状不动。
- 真正要解决的只有两件事：**上下文连贯** 和 **去 DeepSeek AI 味**。其他一概不改。

## 2. 提交链（分支 my_agent_restructure，远端 origin）

| commit | 内容 | 结局 |
|---|---|---|
| be4cd6b | 声称换成 30/70 规范，实际只删了 contract.py 的旧正文节，_SCENE_DUTY/51 条短示例仍在 | 驳回 |
| dbd40db | writing_guide 2,055 字 + 6 段角色卡 first_mes 开场白 4,887 字 + actor.py 去顺序义务 | 驳回：开场白学不到接话 |
| 6b28b76 | 10 组本机存档真实回合 + 10 组改前/改后，共 4,897 字；prompt_example() 人称适配 | **驳回，见下** |

仓库：`D:\AI\my_agent_restructure`；Python：`D:\AI\confyui_env\Scripts\python.exe`。

## 3. 用户已驳回的做法（绝对不要再做）

1. **拿本机旧存档当范文**。那些回合本身就是带 AI 味的 DeepSeek 输出，用户早已驳回过
   （「是早晨那种发白的亮」「纱雾先动的是手指」「这个认知让……」「声音哑的，是……那种哑」
   「是温的」「先……再……最后……」连续七八句）。6b28b76 把它们抽出来当「真实回合示范」，
   等于把被驳回的坏输出又喂回去。**全部删掉。**
2. **改前/改后对照**（原文→改后→说明）。把坏句子展示给模型 = 白象效应，模型会照着坏句子写。
   `refs-人味/examples.md` 那 10 组对照**不能进提示词**。
3. **任何负面框架**。「AI 味通常从三个地方来」「不要写 X」「禁止 X」都是在给模型指坏路。
   只许正向教「用什么、怎么写」。
4. **抽象概括式规则**。「落笔十条」这种每条一句话的清单（「每段删掉一句解释，看是否更好」
   「篇幅跟信息量走」「结尾停在动作、声音或物件上」「答一半/答错/反问/重复/不说话」）
   用户判定 AI 看不懂、没有资料参考、等于没写。**不要自己压缩、自己概括。**
5. **角色卡 first_mes 开场白当范文**。没有「玩家说一句→角色怎么接」的因果，无用。
6. **提示词里的元注释**。「配比：约 30% 规则，70% 示例」「DeepSeek 小说去 AI 味」这类
   标题/注释是浪费字数，删。
7. **字数硬限制**（300~600 下限/上限）。已删，保持删除。长度由这一拍的信息量决定。
8. **顺序义务**（「按顺序补齐」「先做什么、再说什么、最后做什么」）。已删，保持删除。
9. **自己写范文**。用户原话：去互联网找真实有效的酒馆范文，不要 AI 自写。

## 4. 用户要的验收标准

- **规则部分**：直接搬 `skills/writing` 的原文，不压缩、不概括。形态是
  **一条规则 + 紧跟一段两三百字的完整正面示例**，规则+示例成对出现，一路排下去。
  不是「规则清单在一头、示例在另一头」。
- **示例部分**：完整的高质量回复原文整段扔进去——300 字的、400 字的、1000 字的，
  标成「示例一 / 示例二 / 示例三」，**全文照搬，不删不改不说明**。
  来源必须是真实有效的酒馆角色卡/真人认可的回复，不是本机 AI 存档、不是开场白、
  不是改前改后、不是 AI 现写。
- **配比**：约 30% 规则 / 70% 示例。总量允许 7,000~10,000 字（前缀缓存命中，省钱）。
- **措辞**：全正向。「用什么替代什么」可以，但替代的「什么」必须是好写法本身，
  不能把坏句子贴出来当靶子。
- **角色卡自带的 exampleDialogue 也要换**（见 §6），它现在是 AI 味旧文，
  排在契约最前面，影响力比后面的范文还大。
- 上下文连贯（长记忆/压缩记忆）是另一条独立诉求，本轮未做，见 §9。

## 5. 注入链路（改哪里）

| 文件 | 作用 | 现状 |
|---|---|---|
| `app/features/persona/writing_guide.py` | 规则段，2,055 字 | 已提交，内容是「落笔十条」式，**待重做** |
| `app/features/persona/writing_examples.py` | 范文段，EXAMPLE 4,897 字；`prompt_example()` 送模型前把「他」纠成「你」 | 已提交，内容是存档回合+改前改后，**待整体替换** |
| `app/features/persona/contract.py` | `_CONTRACT` 约 15.6k 字，:80 拼 WRITING_GUIDE | **有未提交改动**（账本/位置，别的任务线），别整文件顺手提交 |
| `app/features/persona/head_builder.py` | :104-115 契约后拼 `writing_examples.prompt_example()` + 生成前最后一句 | 已提交 |
| `app/features/persona/prompts/actor.py` | `_SCENE_DUTY`(:8-28)、`_THIS_TURN_ECHO`(:31-35) | 已提交，顺序义务已删 |
| `agents/qq/characters.json` | 角色卡数据（**不在 git**），izumi_sagiri 的 `exampleDialogue` 255 字 | AI 味旧文，待换；voiceAnchor/voiceExample 为空 |

## 6. 角色卡自带示例的问题（用户本轮点名）

`persona_prompt_current_sagiri.txt` 第 15–31 行「## 她说话的示范」来自
`characters.json` 的 `exampleDialogue`，里面有：
「像是自己也觉得没必要」「顿了顿，声音低下去」「这个念头让她心跳快了一下」「装作很忙地翻纸」。
它在契约之前注入，模型先学它。**换范文时必须连这段一起换成真实好回合**，否则白换。

## 7. 规范原文在哪（skill，不在 git，换机器要手动带走）

用户指定的原文库：`D:\AI\agent_my_test\skills\writing\`
（`D:\AI\my_agent_restructure\skills\writing\` 有同一套副本）

- 入口：`skill.md`（分层导航，别一次全读，全库约 30 万字符）
- **规则+示例成对形态的现成材料**：`04-liveness/refs-打磨/techniques.md`
  （30 条技法，每条带示例；注意甄别其中「坏→好对照」式条目，按 §3.2 只取正面部分）
- 主规范：`04-liveness/novel-humanize.md`（小说/网文总装）
- 其他：`04-liveness/human-writing.md`、`huoren-write.md`、
  `refs-打磨/banned-words.md`、`checklist.md`、`diagnosis.md`、`roles.md`、
  `refs-人味/ai-patterns.md`、`character-voice.md`、`human-texture.md`
- `refs-人味/examples.md`：10 组改前/改后，**用户已驳回此形态，禁用**
- 风格层：`05-style/sun-style.md`（白描冷叙事）等；内容层：`03-content/quality-guide.md`
- 向量库 `writing_18` 有 59 条纯范文段落（skill.md 提到，本机是否可查未验证）

## 8. 范文来源现状（核心未解问题）

**目前手上没有一份用户认可的中文完整范文。** 已排查过的来源都不合格：

- 本机存档 `agents/qq/sessions/persona_*izumi_sagiri*.jsonl`：全是带 AI 味的模型输出，禁用。
- 本机角色卡（`D:\AI\tavern_cards`、`D:\AI\tavern_compare`、
  `D:\AI\SillyTavern\data\default-user\characters`）：扫过 `mes_example`，
  没有 ≥150 中文字符的；`first_mes` 是开场白，禁用。
- 用户上传的 `D:\Users\gjl\Downloads\莉莉亚：我们的第一次也是最后的旅程.json`（mes_example 4,466 字符）
  和 `病娇木名.json`（902 字符）：都是英文。
- 结论：需要**上网找真实的酒馆中文角色卡/公认好回复**（chub 等平台），
  或让用户直接提供他认可的范文。这是重做前必须先解决的输入问题。

## 9. 独立未修缺陷（不要和写作线混在一起）

- **RAG 串台**：新存档第一轮对话就出现从未见过的旧记忆。用户明确否定「新旧存档」解释，
  认定是旧存档记忆没删干净/检索串台。未修。相关：`app/features/persona/rag.py`、`wipe.py`（均有未提交改动）。
- **上下文连贯**：压缩记忆/长记忆没接好，写作 AI 上下文不连贯（「放到桌子」后还写「站门口」）。未修。
- 工作区还有约 60 个其他任务线的未提交文件（comic/control/image/novel/qq 等），
  **多会话共享工作区，别整仓提交，别 git 清场**。

## 10. 快照与复现脚本

- 当前完整 system 头快照：`persona_prompt_current_sagiri.txt`
  （16,449 字 / 759 行，web_local + izumi_sagiri + intimate=True 实际拼出）
- 旧快照（勿混淆）：`persona_prompt_full_sagiri.json/.txt`
- 范文抽取脚本（可复现 6b28b76 的坏范文，仅供溯源，勿再运行覆盖）：
  `_tmp_new_examples.py`、`_tmp_find_examples.py`、`_tmp_scan_cards.py`、`_tmp_scan_chub.py`

## 11. 测试与生效

```
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_persona.py"        # 393 条
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_writing_examples.py" # 6 条
D:\AI\confyui_env\Scripts\python.exe -m unittest discover -s tests -p "test_writing_style.py"    # 37 条
```

6b28b76 时三套全过。注意 `test_writing_examples.py` 的长度断言（4,500–5,500）和
`test_persona.py` 的人称断言（系统头无「他」）会约束新范文，改范文要同步改断言。
改 `.py` 后需用户手动重启 **5174 主站进程**；AI 一律不重启、不点 bat。

## 12. 用户点名的 AI 味句式（验收时拿这些自查）

「是早晨那种发白的亮」「纱雾先动的是手指」「这个认知让……」「声音哑的，是睡了一夜没开口的那种哑」
「脚趾头缩了一下」「是温的」「站门口像上菜的」「那种是快，吓人的快」「就，会数」
「先……再……最后……」流水账、「XX 是 YY 的 ZZ」定义癖、「不是 X 而是 Y」「那一刻意识到」
「她/他知道……」、三连排比、内心总结句。
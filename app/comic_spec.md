# 连续剧情提示词生成规范 Skill
**（单人与男女双人特化版）**

> **适用范围**  
> - 单人：`1girl` 或 `1boy`  
> - 男女双人：`1girl, 1boy`  
>
> **暂不适用**  
> - 女女双人（`2girls`）  
> - 多人（3人及以上）  
> - 需要角色间复杂互动且无法用 `girl` / `boy` 区分的场景  
>
> 本 Skill 的目标是：让 AI 按照固定格式，直接输出可批量生成的提示词。

---

## 一、AI 输出格式（必须严格遵守）

AI 只需输出以下两部分，不添加任何额外解释、标题、编号或说明文字。

### 输出模板

```
固定部分（一行，角色标识 + 脸部特征）

动态部分第1格
---
动态部分第2格
---
动态部分第3格
---
...（依此类推）
```

### 输出要求

| 规则 | 说明 |
| :--- | :--- |
| **只输出两部分** | 固定部分 + 动态部分 |
| **不加任何额外文字** | 不要写"以下是""第1张""分镜1""说明"等 |
| **不加标题** | 不要写"固定部分：""动态部分："等标题 |
| **不加编号** | 不要写 1. 2. 3. 或 第一格、第二格 |
| **动态部分用 `---` 分割** | 每格之间用三个短横线分隔，最后一行不加 |
| **固定部分单独一行** | 放在最前面，与动态部分用空行隔开 |
| **每格一行** | 动态部分每格写成一个连续的英文提示词，不换行 |
| **全英文** | 动态部分用英文短词，逗号分隔 |

---

## 二、固定部分（角色标识 + 脸部特征）

| 规则 | 说明 |
| :--- | :--- |
| **必须写角色名** | 如 `Izumi Sagiri`、`Rem`、`Raiden Shogun` |
| **必须写脸部标志性特征** | 发色、发型、瞳色、发饰、面部特殊标记（泪痣、伤疤、第三只眼等） |
| **单人场景标注性别** | `1girl` 或 `1boy` |
| **男女双人场景标注人数与性别** | `1girl, 1boy` |
| **不写服饰** | 服饰属于动态部分 |
| **不写身体特征** | 体型、胸部、腿型等不属于脸部，不写 |
| **可省略画师串** | 若加画师串反而干扰角色，可去掉 |

**为什么必须写脸部特征？**  
模型每一格都是独立生成，没有记忆。如果你只写 `Izumi Masamune`，模型可能这次画黑发，下次画绿发。发色、瞳色、发型、发饰必须写死，才能保证十格一致。

**脸部特征范围：**
- 发色：`black hair`、`silver-white hair`、`brown hair`
- 发型：`short hair`、`long hair`、`twin tails`、`blunt bangs`
- 瞳色：`blue eyes`、`red eyes`、`golden eyes`
- 发饰：`pink ribbon hair ornaments`、`black hairband`
- 面部标记：`mole under eye`、`third eye`、`facial scar`

**单人示例：**
```
1girl, Izumi Sagiri, silver-white long hair, blue eyes, pink ribbon hair ornaments, twin tails
```

**男女双人示例：**
```
1girl, 1boy, Izumi Sagiri, Izumi Masamune, silver-white long hair, blue eyes, pink ribbon hair ornaments, twin tails, black short hair, dark eyes
```

---

## 三、动态部分（每格必须写清楚的 7 个要素）

| 要素 | 必须写清楚的内容 | 示例 |
| :--- | :--- | :--- |
| **① 场景** | 具体地点 | `living room sofa` / `kitchen counter` / `bedroom floor` |
| **② 服饰** | 谁穿什么，**颜色+款式必须写全，每格重复，禁止泛化** | `girl wearing black lace bra and pink frilled shorts` / `boy wearing grey t-shirt and black shorts` |
| **③ 动作** | 谁在做什么，必须指明角色，**涉及物体移动时必须加位置副词** | `girl sitting on sofa hugging knees` / `girl lifting new light blue pajamas out of the box ahead of her` |
| **④ 神态** | 谁是什么表情，必须指明角色，**与局部动作互斥时省略** | `girl shy smile` / `boy gentle smile` |
| **⑤ 视角** | 镜头角度和景别 | `front view, medium shot` / `side view, full body shot` |
| **⑥ 灯光氛围** | **每格必须写，且除非剧情需要变化，否则每格必须保持一致** | `warm lamp light` / `soft afternoon light` / `cool moonlight` |
| **⑦ 其他氛围** | 可选，如天气、季节、情绪氛围 | `gentle breeze` / `cozy atmosphere` |

**每格内部顺序建议：**
```
场景 + girl服饰 + boy服饰 + girl动作 + boy动作 + girl神态 + boy神态 + 视角 + 灯光氛围
```

**单人时**，由于画面只有一人，动态部分可以省略 `girl` / `boy` 前缀，但为了统一，建议保留 `girl` 或 `boy` 以明确性别。

**构图必须和服饰自洽（2026-10-12 实测新增）**：景别决定拍得到哪些部位，服饰标签不许超出画面拍到的范围——
- `close-up` / `portrait`（头肩）：不写鞋、袜 / 腿部穿戴；
- `upper body`（胸以上）：不写鞋、袜 / 腿部穿戴；
- `cowboy shot`（膝以上）：不写鞋；
- `full body` 什么都拍得到，裙 / 裤、鞋、腿部穿戴随便写。

写了拍不到的部位，模型只能把这件服饰塞给镜头（观众）——实测 `upper body` 配
`white sneakers`，画面下角长出一双不属于任何人的小白鞋，整格崩坏。

**发现冲突时改构图，不许删服饰**：把 `close-up` 放大到 `cowboy shot` /
`full body` 即可。服饰是全话一致性的锚——同一个角色每格必须同一套衣服，
删掉一件＝让她凭空换装（实测：close-up 格删掉 `navy pleated skirt`，画面里
裙子整个消失）。

**单人示例：**
```
bedroom, girl wearing old pink frilled pajamas and pink frilled shorts, bare feet, girl sitting on bed holding a gift box with both hands, girl surprised expression, warm lamp light, front view, medium shot
```

**男女双人示例：**
```
bedroom, girl wearing old pink frilled pajamas and pink frilled shorts, bare feet, boy wearing grey t-shirt and black shorts, bare feet, girl sitting on bed holding a gift box with both hands, boy standing beside bed watching her, girl surprised expression, boy gentle smile, warm lamp light, front view, medium shot
```

---

## 四、男女双人场景额外规则

| 规则 | 说明 |
| :--- | :--- |
| **每个动作前必须加角色名** | `girl sitting...` / `boy standing...` |
| **每个服饰前必须加角色名** | `girl wearing...` / `boy wearing...` |
| **每个神态前必须加角色名** | `girl shy smile` / `boy gentle smile` |
| **位置描述不能含糊** | 避免 `girl sitting on bed` 和 `boy sitting on floor` 混淆 |
| **互斥动作不能同时出现** | 不能同时写 `girl peeking from behind door` 和 `girl shy expression` |

---

## 五、服饰与物件颜色规则

**服饰，以及画面里一切看得见颜色的物件（耳机、包、伞、杯子、手机、发饰、围巾、
眼镜、鞋……），都适用本节。** 只给衣服上色、物件裸写，是这份规范最容易漏的一条。

| 规则 | 说明 |
| :--- | :--- |
| **每格服饰必须写完整颜色** | 不能因为前面写过就省略，AI没有"记忆" |
| **颜色+款式必须同时出现** | 不能只写 `bra`，必须写 `black lace bra` |
| **禁止泛化词** | `underwear`、`clothes`、`outfit`、`pajamas` 单独出现都不行 |
| **同一件衣服，每格颜色必须一致** | 如果第1格是 `black lace bra`，第10格也必须是 `black lace bra` |
| **换装剧情中，换掉的那件也要写颜色** | 比如 `old pink frilled pajamas` → `new light blue cotton pajamas`，颜色和款式都要写 |
| **物件同样禁止泛化** | 耳机、包、伞、杯子、手机、发饰、围巾、眼镜、鞋……凡是有颜色的物件都要写颜色：`white in-ear earphones`（**不能**只写 `in-ear earphones`）、`black backpack`（不能只写 `backpack`）、`red umbrella`（不能只写 `umbrella`） |
| **写了款式 / 姿态 ≠ 写了颜色** | `backpack on one shoulder`（这是**姿态**）、`sailor collar`（这是**款式**）、`pleated skirt`（这是**款式**）——三个都还是**没写颜色的裸写法**，必须写成 `white backpack on one shoulder`、`white sailor collar`、`white pleated skirt`。衣领、裙子、衣服、背包……**一件都不能漏**，代码会逐个查，漏一个就打回重写 |
| **同一件东西只写一个标签** | `backpack` 和 `backpack on one shoulder` 是**同一件**——两个都写，模型会画**两个包**。合并成 `white backpack on one shoulder` |
| **同一件物件，每格颜色必须一致** | 第1格写了 `white in-ear earphones`，后面每一格都得是 `white in-ear earphones` |
| **物件的颜色由剧情定，定下来就不许改** | 剧本第一次出现它时就定颜色，全篇照抄；不许每格随机换色 |

> ⚠️ 颜色+物件的组合**大多不在标签库里**（实测 `white_earphones`、`black_backpack`
> 都查不到），但这**不是**不写的理由——颜色对不对，直接决定画面对不对。
> **查库只是找更准说法的参考，不是白名单；库里没有不等于不能写。**
> 反过来，库里**有**的标签也不一定对（见第九节 `hand around wrist`）。
>
> ⚠️ **更要紧的反方向**：标签库里 `pleated_skirt`、`earphones`、`backpack`
> 这类「款式 / 物件名」**本身就不带颜色**，照抄它们就等于违反本节。实测
> `pleated skirt` 没写颜色，三格画面里裙子一次深藏青、一次浅蓝——观众一眼就
> 看出不是同一条裙子。**本规范 > 标签库**：两者冲突时以本节为准。

---

## 六、灯光氛围规则

| 规则 | 说明 |
| :--- | :--- |
| **每格必须写灯光氛围** | 不能省略，AI没有记忆 |
| **同一场景、同一时间段，灯光词必须完全一致** | 不能第1格 `warm lamp light`，第2格 `cool moonlight` |
| **若剧情需要灯光变化，必须明确过渡** | 例如从白天到夜晚，需在对应分镜写出 `sunset light fading into dusk` |
| **灯光词必须具体** | `warm lamp light` / `soft afternoon light` / `cool fluorescent light` / `golden hour sunlight` |
| **禁止泛化词** | `light`、`lighting`、`atmosphere` 单独出现都不行 |
| **灯光一致性直接影响剧情连贯感** | 暖光突然变冷光，会让观众觉得不是同一场景 |

---

## 七、动作位置副词规则（新增）

| 规则 | 说明 |
| :--- | :--- |
| **涉及物体移动时，必须加位置副词** | 否则物体容易与角色本体或衣服融合 |
| **常用位置副词** | `ahead of her`（举到身前）、`in front of her chest`（捧在胸前）、`beside her`（放在身旁）、`behind her back`（藏在身后）、`above her head`（举过头顶）、`on her lap`（放在膝上） |
| **拿取类动作必须加** | `lifting...`、`holding...`、`taking out...`、`picking up...` 都要加 |
| **穿戴类动作必须加** | `putting on...`、`pulling over her head...`、`buttoning up...` 视情况加位置 |
| **为什么** | 不加位置时，新衣服容易和角色原来穿的衣服混在一起，画面变成一坨 |

**错误写法 vs 正确写法**

| 错误写法 | 正确写法 |
| :--- | :--- |
| `girl lifting new light blue cotton pajamas out of the box` | `girl lifting new light blue cotton pajamas out of the box ahead of her` |
| `girl holding a gift box` | `girl holding a gift box in front of her chest` |
| `girl holding a tray` | `girl holding a tray beside her waist` |

---

## 八、局部动作与表情互斥规则（新增）

| 规则 | 说明 |
| :--- | :--- |
| **当角色处于"局部展示"状态时，不要再写完整表情** | 否则模型会画出两个角色 |
| **什么是"局部展示"状态** | `peeking into the hallway`（偷瞄）、`hiding behind door`（躲门后）、`back view`（背影）、`side profile`（侧脸）、`looking away`（扭头看别处） |
| **为什么** | `peeking` 已经包含了"只露一部分脸"的含义，再加 `girl shy expression`，模型会以为是另一个女孩在展示完整表情 |
| **正确做法** | 要么删掉表情，要么改成不冲突的动作（如 `girl standing at doorway`） |
| **关于表情方块（inset panel）** | 有些模型支持在一个画面里用"小方块"额外展示表情，但容易引起混乱，建议直接不用 |

**错误写法 vs 正确写法**

| 错误写法 | 正确写法 |
| :--- | :--- |
| `girl standing by the door peeking into the hallway, girl cautious expression` | `girl standing by the door peeking into the hallway`（删掉表情） |
| `girl peeking from behind door, girl shy smile` | `girl standing at doorway, girl shy smile`（改成不冲突的动作） |
| `girl back view, girl gentle smile` | `girl back view`（背影看不到表情） |

---

## 九、常见错误对照

| 错误写法 | 问题 | 正确写法 |
| :--- | :--- | :--- |
| `wearing pink pajamas, wearing grey t-shirt` | 没写谁穿什么 | `girl wearing pink pajamas, boy wearing grey t-shirt` |
| `girl peeking from behind door, girl shy expression` | 动作和神态互斥 | `girl standing at doorway, shy expression` |
| `1girl, loli, petite, flat chest, long silver hair, blue eyes, Sagiri` | 堆砌非脸部特征 | `1girl, Izumi Sagiri, silver-white long hair, blue eyes, twin tails` |
| 第2格起只写 `bra` | 颜色会变 | 每格都写 `black lace bra` |
| `girl wearing underwear` | 泛化词，AI随机发挥 | `girl wearing black lace bra and black cotton panties` |
| 换装后只写 `new pajamas` | 款式和颜色都没了 | `new light blue cotton pajamas with white lace trim` |
| 固定部分只写 `Izumi Masamune` | 发色在黑色和绿色之间变化 | 固定部分写 `Izumi Masamune, black short hair, dark eyes` |
| 第1格 `warm lamp light`，第2格 `cool moonlight` | 灯光氛围不统一 | 统一写 `warm lamp light`，除非剧情需要变化 |
| `girl lifting new pajamas out of the box` | 新衣服和角色本体衣服融合 | `girl lifting new pajamas out of the box ahead of her` |
| `girl peeking into hallway, girl cautious expression` | 出现两个女孩 | `girl standing by the door peeking into the hallway`（删掉表情） |
| `in-ear earphones` | 物件泛化，颜色全靠模型随机 | `white in-ear earphones` |
| 旁白「一只脚刚踏下车门」→ 画面写 `standing on one leg` | 把旁白的字面词翻成标签，画成单脚站立的芭蕾姿 | 画面写 `standing, automatic_door, bus_interior`（她站在车门处的样子），这句话交给旁白讲 |
| 旁白「她猛回头」→ 画面写 `turning head` | 画面是静态的一张图，却写了"正在转"的瞬间 | 画面写 `looking back`（已经转过来的样子） |
| `bus_interior, bus_stop, outdoors` 写在同一格 | 一格内地点自相矛盾 | 一格只留一个明确地点 |
| `backpack` / `sailor collar` / `pleated skirt` | 只有类别名，**没写颜色** | `white backpack on one shoulder` / `white sailor collar` / `white pleated skirt` |
| `backpack, backpack on one shoulder` | 同一件东西写了两遍，模型会画**两个包** | 合并成 `white backpack on one shoulder` |
| 01/02 格 `pov hands`，03 格 `1girl, 1boy` + 男装 | 镜头人称中途跳了，凭空多出一个男生 | 全篇保持第一人称：「我」只出手，03 格用 `1other` + `pov hands`，**别写 `1boy`** |

---

## 十、画面只写「静止可见的样子」

| 规则 | 说明 |
| :--- | :--- |
| **画面是静态的一张图** | 只写镜头里这一刻**看得见**的东西：姿态、位置、服饰、表情、景别、背景 |
| **「正在发生的那一瞬」不进画面** | 旁白「她一只脚刚踏下车门」→ 画面**不能**写 `standing on one leg`；画面写 `standing, automatic_door, bus_interior`，这件事交给旁白讲 |
| **旁白、气泡、画面有关联，但不是逐词对译** | 旁白是**描述**（讲发生了什么）、气泡是**语言**（嘴里说出来的话）、画面是**画面**（看得见的样子）。不许把旁白的字面词逐个翻成标签 |
| **一格只能有一个明确地点** | 不要同时写 `bus_interior`（车厢内）和 `bus_stop` / `outdoors`（站台外景），模型会画出两个空间混在一起的画面。实测三格都同时写了 `bus interior` 和 `bus stop / outdoors / city`，同一个公交场景三格各不相同 |
| **同一场景内，地点标签跨格保持一致** | 这一幕在车厢里，每一格都写同一组地点标签（如 `bus interior, handrail, door`）；换场景时才整组一起换 |

---

## 十一、镜头人称：全篇第一人称 POV（2026-10-12 拍板，唯一模式）

**镜头＝观众的眼睛，画面里永远只有女主一个人。** 没有第三人称格、没有双人
同框、没有多人构图——剧情里需要"另一个人"时，那个人就是镜头后面的观众本人。

| 规则 | 说明 |
| :--- | :--- |
| **每一格都写视角标签** | `pov`；「我」是女性（百合 / 女女向）时写 `female_pov`，整话统一不许一半一半 |
| **画面里只有一个人** | 计数只有 `1girl`（+ `solo`，我们的手入画时换成 `solo focus` + `1other`）。**`1boy` / `2girls` 任何格都不许写** |
| **男性角色＝观众本人** | 黄毛 / 哥哥 / 上司……都不入画；观众通过自己的手（`pov hands` + 动作标签）参与剧情 |
| **「我」只出手，不出人** | 脸、身体、服饰一律不出现——给「我」写服饰等于命令模型画出一个完整的人 |
| **她对我们做动作** | 用 `* viewer` 族标签：`looking at viewer` / `grabbing viewer` / `hugging viewer` / `reaching towards viewer` / `kabedon on viewer`……库里齐全，写前先查 |
| **我们对她们做动作** | `pov hands` + `another` 族标签：`holding another's wrist` / `grabbing another's arm`（唯一可用写法，`hand around wrist` 语义不明会翻车） |
| **旁白「我」↔ 画面视角必须对上** | 旁白是「我」的剧情，画面标签里就得有 `pov` / `pov hands`（或 `female_pov`）；只有旁白有「我」而画面没有视角标签，模型按第三人称机位渲染 |
| **画面＝「我」的眼睛看到的（2026-10-12 补）** | 你是记录者：她站在哪由旁白决定。旁白说她在厨房 → 画面是**从玄关望进厨房**的她（`kitchen, stove, cooking`），**不是** `standing, doorway`（那是把「我」进门的位置套到她身上）；「我」的动作（推门/走近）只进旁白，绝不变成她的标签；她不在视野的瞬间不成格——让旁白推进到她入画，或镜头已经对着她 |

**为什么**：2026-10-12《夜路遇黄毛》实测——旁白全是「我」却一格 `pov` 都没写，
模型按旁观机位渲染成两个人：黄毛被画成金发女生、金发蹭上黑发女主的头发。
2026-10-11 同类事故：03 格写 `1girl, 1boy` + 男装，凭空多出一个和前两格接不上
的男生。旧版「人称第一格定死、无 pov 算第三人称」的规则给了双人剧本留口子，
现全篇统一第一人称，口子封死。

---

## 十二、本质总结

这套规则的本质是：**把"谁、在哪、穿什么、做什么、什么表情、什么镜头、什么光"七个问题，在每一格都回答清楚。**

- 固定部分回答 **"谁"**，并且必须包含 **角色名 + 脸部标志性特征**。
- 动态部分回答剩下六个 **"在哪、穿什么、做什么、什么表情、什么镜头、什么光"**。

**特别强调：**
1. **脸部特征必须写死在固定部分**。
2. **服饰的颜色和款式必须每一格都写全**。
3. **灯光氛围必须每一格都写，且整段剧情保持一致**。
4. **物体移动时必须加位置副词**，避免和角色本体融合。
5. **局部动作与完整表情互斥**，否则会出现两个角色。
6. **衣物和随身物件的颜色必须写出来**——`backpack` / `sailor collar` /
   `pleated skirt` 这类只有类别名的写法不合格，款式和姿态都替代不了颜色。
7. **全篇第一人称 POV**：镜头＝观众的眼睛，画面里只有女主一个人；「我」只出手
   （`pov hands`），绝不写 `1boy` / `2girls`，每格必带视角标签。

AI 的唯一任务就是：**按格式输出，不加任何多余内容。**

---

*本 Skill 适用于单人与男女双人场景，可根据实际生成效果持续迭代。*

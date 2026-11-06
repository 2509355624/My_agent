# Anima 角色库：一刀砍掉「角色库 AI」，改成一次调用

> 2026-11-06 用户拍板。这份是**实施清单**，下个会话照着做即可。

## 目标

去掉中间那层「角色库 AI 判定」，把向量检索结果**直接塞进大大怪自己的上下文**，
由它一次完成：判渠道 → 决定要不要用库里的角色 → 写提示词。

```
现在的样子：
  用户原话 → 向量检索 → 角色库 AI 判定(use/skip + channel) → 生图 AI 翻译
                          ↑ 多一次调用、多一个误判点

要变成：
  用户原话 → 向量检索（本地，不花 token） → 候选块拼进提示词
           → 大大怪一次调用：判渠道 + 选角色 + 写提示词
```

**不要 skip 这个概念。** 候选块只是「参考资料」，大大怪自己决定用不用；
用不上就忽略，跟其他参考池（标签库资料）一样。

## 一、候选块的形状

每条角色只给四样，**英文 tag 照给**（大大怪读得懂）：

```
【Anima 角色库 · 本次检索到的角色】
下面这些是按用户这句话在角色库里搜出来的（**可能无关，自己判断**）。
用户点名了 Anima 系渠道、并且要的就是其中某个角色 → 把该角色的 key 填进
JSON 的 character 字段；否则 character 留空，一切照常。

- cantarella｜坎特蕾拉｜trigger: cantarella (wuthering waves)
  ｜1girl, purple hair, very long hair, hair between eyes, blue eyes, facial mark
- verina｜维里奈｜trigger: verina (wuthering waves)
  ｜1girl, blonde hair, twintails, green eyes, tacet mark
```

实现：`lora_lib.preview_block(text, k=6)` 返回上面这段字符串（没有候选就返回空串）。
只放前 6 条、每条 identity 截前 120 字，整块控制在 1.5KB 以内。

## 二、改哪几处

### 1) `app/lora_lib.py`

- 新增 `preview_block(text, k=6) -> str`：内部仍用 `search()`（向量 + 名字命中加权），
  把结果拼成上面那个文本块。
- `decide()` / `_judge()` **保留但不再被调用**（先留一轮，确认新路稳定后再删）。

### 2) `app/direct_gen.py`

- 拼提示词的地方（翻译模板，约 1630~1660 行那一段）在末尾追加
  `{role_block}` 占位，占位内容 = `lora_lib.preview_block(用户原话)`。
- JSON 契约加一个字段：`"character": "候选里的 key，或空串"`，并在说明里写清三条：
  1. 只有用户**点名**了某角色才填；
  2. 只描述外观（「绿头发的偶像」）→ 留空；
  3. 提到 krea2 / qwen / moss 这些非 Anima 渠道 → 留空。
- `_enqueue(skill, prompt, text, character="", ...)` 把 key 传下去。

### 3) `app/char_loras.py`

- 新增 `entry_by_key(key) -> dict|None`（从 `config/char_loras.json` 查）。
- `apply(skill, prompt, workflow, user_text="", key="")`：
  - 有 `key` → 直接 `entry_by_key`，**不再走向量/大模型**；
  - 没有 `key` → 保持现状（`match()` 里那条兼容路径可以最后再删）。
  - 后面「原始 tag 在前 + 清洗 + 触发词压尾」的逻辑完全不动。

### 4) `app/tools/normal/generate_image.py`

- `_generate_image(..., character="")` 透传到 `char_loras.apply(..., key=character)`。

## 三、验证用例（四种，都必须跑）

| 用例 | 期望 |
|---|---|
| `anima 坎特蕾拉` | 挂 LoRA；提示词以她的原始 tag 开头；渠道 anima_clear |
| `坎特蕾拉`（不提渠道） | 挂 LoRA；渠道不走默认档也不被改写（走 silver） |
| `krea2 坎特蕾拉` | **不挂** LoRA；渠道仍是 krea2（不许被改成 silver） |
| `画个绿头发的偶像` | **不挂** LoRA；提示词里没有角色库内容 |

回归：`anima 千夏`、`silver 胡桃`、`大大怪 /角色库` 三条要正常。

## 四、回滚

- 改动集中在 4 个文件，git 一次 revert 即可。
- `char_loras.apply` 的 `key` 参数默认空串 = 完全回到旧行为（走 `match()`）。
- 索引文件 `config/char_loras.json` 不动。

## 五、做完之后顺手删的

确认新路稳定一周后，可以删掉：
- `lora_lib.decide()` / `_judge()` / `JUDGE_SYS` / `lora_match_cache.json`
- `char_loras.match()` 里调 `lora_lib.resolve()` 的那条兼容分支

删之前先把 `state/lora_match_cache.json` 备份一份。

---

## 六、实施记录（2026-11-06，已落地）

四文件改完，`py_compile` 全过；验证脚本留在 `tools/`：

| 脚本 | 覆盖 |
|---|---|
| `_verify_anima_onepass.py` | 单元 20 项（`preview_block` / `entry_by_key` / `apply(key=…)` / 模板新槽） |
| `_verify_anima_e2e.py` | 四种用例，`_translate → char_loras.apply` 全链路 |
| `_verify_anima_queue.py` | `_enqueue(character=…)` → `_generate_image` kwargs 透传 |
| `_verify_anima_regress.py` | `anima 千夏`、`silver 胡桃`、`/角色库` |

跑法（系统 py 缺 numpy，必须用这个解释器）：
`$env:PYTHONIOENCODING='utf-8'; D:\AI\confyui_env\Scripts\python.exe tools\_verify_anima_e2e.py`

### 与清单的三处偏差

1. **`anima 坎特蕾拉` 落 `hd_3_clear`，不是 `anima_clear`。**
   代码里 `anima` 恒等于三档（`_TIER_RE`，2026-10-05 起既有口径）。两者都在
   `CHAR_LORA_SKILLS` 里，LoRA 照挂、tag 照打头，只有渠道 id 不同。没有为这个
   用例改渠道解析——改了会碰到「anima = 三档」那堆既有锚定。用例按真实行为断言。

2. **加了相关性闸 `PREVIEW_MIN = 0.45`（清单没写）。**
   `search()` 的 `MIN_SCORE=0.35` 太松：实测「帮我看看这个 bug」top 0.438、
   「画个绿头发的偶像」0.397 都能召回 6 条，1100 字全塞进上下文是纯噪声。
   真点名都在 0.57 以上（坎特蕾拉 1.095、千夏 0.576）。低于闸整块不拼，
   过了闸也只留同样过闸的条目 —— 这正是第四条用例「提示词里没有角色库内容」
   要的结果。热态耗时 ~13ms/次（首次要加载 embedding 模型）。

3. **点名非角色库渠道时，候选块整块不拼。**
   `named` 命中且不在 `CHAR_LORA_SKILLS`（krea2 / qwen / moss / nai）→
   `role_block = ""`。省 token，也免得模型照着候选块去填 `character`。
   没点名时仍照拼（模型可能自己判进 anima 系）。

### 仍然走旧 `match() → resolve() → decide()` 的地方（key 恒空）

网页跑图台 `build_t2i_workflow`、改图管道 `_revise`、`/直通-` 零 token 直通、
漫画 brief。四条都没有「大大怪填 key」的机会，所以留旧路。副作用：`/直通-`
号称零 token，实际仍会烧一次角色库判定 —— 这是改动前就有的，不在本次范围。

### 已知抖动

`坎特蕾拉`（不提渠道）两跑里有一次模型回 `reply` 反问「走哪个渠道」，
另一次正常出 `silver + cantarella`。模板里本来就有「反问只是多耗他一轮」
那条，属于模型温度随机，没有加硬指令去压。


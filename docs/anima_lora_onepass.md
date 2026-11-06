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

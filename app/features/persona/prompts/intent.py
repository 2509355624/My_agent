"""意图判断提示词。"""

_INTENT_PROMPT = """判断下面这句话**在干什么**。只输出一个 JSON 对象，不要解释。

{
  "intent": "intimate | joke | literal | insult | neutral",
  "finish": "inside | outside | none | unknown",
  "note": "用不超过 15 个字说明他到底在干什么",
  "addressed": true,
  "secret": false,
  "facts": ["这一幕已经有的东西，一样一条"],
  "items": {"use": [], "gain": []}
}

类别：
- intimate —— 亲密/求欢：想亲近、调情、要求身体接触或性
- joke —— 玩笑/逗你：拿对方开玩笑、威胁着玩、故意说离谱的话
- literal —— 字面请求：真的要求做某件事、问某个问题
- insult —— 冒犯：贬低、辱骂、羞辱对方
- neutral —— 普通对话：闲聊、叙述、没有明确指向

判断要点：
- 「做红烧兔头」「把你吃掉」「打包带走」这类**针对角色本身**的离谱说法，
  通常是玩笑，不是真的要做菜或者真的要伤害。判 joke。
- 「抱抱」「亲一下」「过来」这种直接的亲近要求，判 intimate。
- 「帮我倒水」「今天几号」这种正常的事项，判 literal。
- 拿不准时判 neutral。

finish（这一轮有没有「射在里面」）：只有明确发生/要求内射才 inside；
戴套、体外、射在外面 outside；没进入或没发生 none；看不出来 unknown。
普通对话一律 none，不要凭空补。内射常常和她的高潮同一拍发生；她到顶的
那一下如果同时射了，也算 inside。射精可以连续，但刚高潮过的下一轮通常
该是余韵，不要为了凑次数每轮都判 inside。
**括在（）里的动作也算发生**：像「（我把精液射进她的小穴里）」这种，
是他真的在做，按字面判 inside，不要因为写法简短就判 none。

%(facts_guide)s

secret：他这一句里有没有没说出口的心里话。有就 true，没有 false。

items：这一轮有没有东西进出她手里（没有就留空数组）。
  use 只记**真的用上了**的——拿到手上、真起作用了；被她挡回来、只是拿给她看的都不算。
  gain 只记**真的递到你手上**的；她给你的私人物品一定记，kind 填 keepsake。
  （可选的道具 list：%(item_keys)s）

【最近发生过什么】（供你判断语境；不要复述、不要改动）
%(recent)s

【你记得的事】
%(ledger)s

角色是谁：%(who)s
他这一轮说的：%(said)s"""

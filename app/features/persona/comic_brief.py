"""moss 漫画页 brief 的文本拼装：一页、一个瞬间、多机位。"""


def build(name, look, last_user, last_ai, scene, state, extra, male):
    """把画面、玩家动作、她此刻和人物拼成一张漫画 brief。"""
    lines = ["这是一部成人向漫画的一页。", ""]
    lines.append("## 画什么")
    lines.append("**这是一页，不是一个故事。** 只画下面那一个瞬间，"
                 "用多个格子从不同角度拍它，不要推进时间、不要讲起承转合。")

    if scene:
        lines.append("")
        lines.append("## 这一页的画面（**已定稿，照它画**）")
        lines.append(scene[:400])
        lines.append("")
        lines.append("这一段是**已经写好的画面**，不是素材。景别、姿态、衣着、"
                     "身体朝向、接触点在哪儿，全部以它为准。")
        lines.append("")
        lines.append("可以做的：换角度、换机位、把同一个姿势拆成不同景别拍（全身→"
                     "近景→手或脸的特写）。")
        lines.append("**不可以做的**：改姿势。她写「坐着」就画坐着，不许改成躺着、"
                     "跪着、反折在椅子上；她写「膝盖并拢」就画膝盖并拢。"
                     "用户原话里如果有和这段冲突的动作描述，**以这一段为准**——"
                     "原话是他要做什么，这一段才是做完之后她实际的样子。")

    if last_user:
        lines.append("")
        lines.append("## 用户这一轮做了什么")
        lines.append(last_user[:600])
        lines.append("")
        lines.append("这一节说明**发生了什么、接触在哪里**，用来定格子的内容；"
                     "但**姿势与衣着不从这里推断**——上面那段画面已经定好了，"
                     "这里只补充「谁在做什么」。")

    if extra.strip():
        lines.append("")
        lines.append("## 用户另外引用的内容（同样要照画）")
        lines.append(extra.strip()[:400])

    if last_ai:
        lines.append("")
        lines.append("## 她此刻正在做什么（正文）")
        lines.append(last_ai[:600])
        lines.append("")
        lines.append("正文里**引号中的话**可以直接当台词用，但**不许改写她的台词、"
                     "也不许替她编旁白**——旁白写什么见下面 `caption` 的规则。")

    if state:
        lines.append("")
        lines.append("## 上一轮结束时的快照（**只作参考**）")
        lines.append(state)
        lines.append("")
        lines.append("⚠️ 这条快照可能落后于上面的画面。"
                     "它和「这一页的画面」冲突时，**以画面为准**。")

    lines.append("")
    lines.append("## 人物")
    lines.append("- 女主角：%s。外观固定：%s" % (name, look or "按角色卡"))
    lines.append("- 男主角：%s。他是读者本人，不要给他起名字、不要画脸特写。"
                 % male)
    return "\n".join(lines)

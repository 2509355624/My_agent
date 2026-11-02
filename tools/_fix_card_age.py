"""把两张「未成年观感」的角色卡改成明确的成年大学生设定（一次性脚本）。

`#角色扮演` 只在私聊开，但角色是妹妹 / 学妹这类字面关联未成年的设定，
接进带亲密内容的玩法里必须把年龄写死。外观串不动——视觉上要的味道不变，
语义上锁死成年。

    python tools/_fix_card_age.py
"""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "agents", "qq", "characters.json")

UPD = {
    "sister": (
        "你是「妹妹」，20 岁，大学生，和你同住。黑发双马尾、遮眼发、蓝眼；"
        "性格纯真、乖巧、惹人怜爱。用温柔乖巧、偶尔带一点撒娇的语气说话。"
        "你清楚自己是成年人，有成年人的判断力。"
    ),
    "junior": (
        "你是「学妹」，20 岁，同一所大学的四年级生，比你低一级。"
        "深棕色短发（波波头）、粉紫色眼；谦逊、崇拜学长、略带羞涩，整齐的平刘海。"
        "用恭敬、带一点羞涩的语气说话，称呼对方为「学长」。"
        "你清楚自己是成年人，有成年人的判断力。"
    ),
}


def main():
    with open(PATH, "r", encoding="utf-8") as f:
        cards = json.load(f)
    n = 0
    for c in cards:
        if c.get("id") in UPD:
            c["systemPrompt"] = UPD[c["id"]]
            n += 1
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    print("updated %d cards" % n)


if __name__ == "__main__":
    main()

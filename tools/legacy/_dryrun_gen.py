# -*- coding: utf-8 -*-
"""指定生图方式的 dry-run 实测（忠实版）。

**不真提交任何生图任务**：把 agent.execute_tool 换成探针，generate_image
一律返回一条「假成功」回执，只把模型实际传的参数记下来。这样能反复跑
「模型会不会照传 qwen」而不惊动 ComfyUI、不花显存、不往群里发图。

忠实度（第一版探针工具调用率 0/7，跟线上完全不符，原因是两条）：
  ① history[0] 必须是真 system 头 —— 线上靠 qq_bot._ensure_system_prompt
     注入，模型据此知道「自己是生图 AI」。漏了它模型就退化成聊天模型，
     只会说「我没有生图能力」。
  ② 图片要传 dataURL —— run_agent_stream(image=...) 走识图/多模态，
     线上就是这么下发的。

用法：
    python _dryrun_gen.py                 # 跑全部用例
    python _dryrun_gen.py "qwen 重绘"     # 只跑一条自定义原话
"""
import base64
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

from app import qq_api
import app.agent as agent_mod
from app.agent import run_agent_stream
from app.agent_prompt import build_stable_prompt

GROUP = "482079537"
AGENT = "qq"
SESSION = "group_" + GROUP

CALLS = []


def fake_execute(name, args):
    if name == "generate_image":
        rec = {k: v for k, v in args.items() if not k.startswith("__")}
        CALLS.append(rec)
        # 回执格式必须像真的（以「已经排上队了」开头），否则 agent.py 的
        # 「本 run 已提交生图」判据不成立、循环不掐断，测的就不是线上路径了。
        return ("已经排上队了，编号 DRY-000（dry-run 未真提交）。"
                "size 832x1216, channel: probe, seed 1234")
    return "dry-run: %s 已跳过" % name


agent_mod.execute_tool = fake_execute


# 用一张**真实**的出图（_qwen_bench 的产物，真动漫人物）做垫图。
# 之前用 2x2 灰 PNG，识图模型读不出内容，模型一律回「识别失败」——
# 那是探针的假阴性，不是线上行为。real=True 时按本地文件读，转 dataURL。
_REAL_IMG = os.path.join(HERE, "_qwen_bench", "20261003",
                         "832x1216_s13_QwenBench_832x1216_s13_00001_.png")


def _data_url():
    if os.path.exists(_REAL_IMG):
        with open(_REAL_IMG, "rb") as f:
            return ("data:image/png;base64,"
                    + base64.b64encode(f.read()).decode("ascii"))
    return "data:image/png;base64," + PNG_B64


def _history():
    """首条必须是真 system 头（线上 _ensure_system_prompt 的等价物）。"""
    return [
        {"role": "system", "content": build_stable_prompt(AGENT)},
        {"role": "user", "content": "[图片]"},
        {"role": "assistant", "content": "（大大怪：233）这图手指好像多了一根。"},
    ]


def one_round(text, own_images=None, quoted_images=None, with_image=True):
    CALLS.clear()
    qq_api.bind_context(
        session_key=SESSION,
        target="group",
        target_id=int(GROUP),
        quoted_images=quoted_images or [],
        own_images=own_images or [],
        user_text=text,
    )
    has_img = bool(own_images or quoted_images)
    user_input = ("[图片]\n" + text) if (has_img and with_image) else text
    img_arg = [_data_url()] if (has_img and with_image) else None
    owners = ["233"] if img_arg else None
    seen = []
    try:
        for ev in run_agent_stream(
            user_input, _history(),
            agent_id=AGENT, session_key=SESSION,
            image=img_arg, image_owners=owners,
            extra_context="【群聊背景】233：（大大怪：把手指修一下）",
        ):
            if ev.get("type") == "assistant":
                seen.append(ev.get("content") or "")
    except Exception as e:                                  # noqa: BLE001
        print("   !! 异常:", type(e).__name__, e)
    finally:
        qq_api.clear_context()

    print("─" * 66)
    print("原话 :", repr(text))
    print("图片 :", ("自己发" if own_images else "") +
          ("引用" if quoted_images else ""), " 下发=", bool(img_arg))
    if not CALLS:
        print("结果 : **没调 generate_image**")
    for i, c in enumerate(CALLS, 1):
        print("调用%d: skill=%r source_image=%r prompt=%r"
              % (i, c.get("skill"), c.get("source_image"),
                 (c.get("prompt") or "")[:70]))
    body = "".join(seen).strip()
    if body:
        print("应答 :", body[:220].replace("\n", " "))
    print()
    return CALLS[:], body


CASES = [
    ("点名 qwen 改图（带引用）", "用 qwen_image_v1 重绘，把 6 根手指换成 5 根", None, ["http://x/1.jpg"]),
    ("口语点名 qwen（带引用）", "qwen 重绘，把 6 根手指换成 5 根", None, ["http://x/1.jpg"]),
    ("点名 qwen + 自己发图", "qwen 重绘一下这只手", ["http://x/2.jpg"], None),
    ("点名 hd_2_clear", "用 hd_2_clear 跑", None, ["http://x/1.jpg"]),
    ("只说高清（形容词）", "高清一点", None, ["http://x/1.jpg"]),
    ("明说文生图 + 自己发图", "别用图生图了，按我说的重画一张", ["http://x/3.jpg"], None),
    ("只引用不改（该拦）", "这张挺好看", None, ["http://x/1.jpg"]),
    ("纯文生图（无图）", "画个蓝发少女站在雨夜街头", None, None),
]


def main():
    argv = sys.argv[1:]
    if argv:
        one_round(argv[0])
        return
    for label, text, own, quoted in CASES:
        print("=" * 66)
        print("用例 :", label)
        one_round(text, own_images=own, quoted_images=quoted)


if __name__ == "__main__":
    main()

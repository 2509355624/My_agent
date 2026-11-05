# -*- coding: utf-8 -*-
"""离线验证：玩家填的「这段时间在干什么」到底有没有进提示词（2026-11-06）。

不碰线上存档：临时目录里造一条会话，把 `_llm_call` 换成假函数，
把真正要发给模型的 messages 抓出来看。
"""
import io
import os
import sys
import tempfile
from unittest import mock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agents, affection, persona  # noqa: E402

tmp = tempfile.TemporaryDirectory()
root = os.path.join(tmp.name, "agents")
os.makedirs(root, exist_ok=True)
state = os.path.join(tmp.name, "state.json")

DOING = "一路赶路去情爱城，中途在驿站歇了一晚"

with mock.patch.object(agents, "AGENTS_DIR", root), \
        mock.patch.object(persona, "_STATE_PATH", state):
    agents.clear_cache()
    persona.save_cards([{"name": "测试", "systemPrompt": "你是测试角色。"}])
    key0 = "private_1"
    persona.switch_line(key0, persona.find_card("测试"))
    key = persona.resolve_line(key0)
    affection.set_mode(key, True)

    seen = {}

    def fake(msgs, **kw):
        seen["msgs"] = msgs
        return "她抬头看你。\n画面：1girl\n心情：平静 强度2\n状态：站着"

    with mock.patch.object(persona, "_llm_call", side_effect=fake):
        turn = persona.skip_day(key, "", days=7, doing=DOING)

    msgs = seen.get("msgs") or []
    task = msgs[-1]["content"] if msgs else ""
    head = msgs[0]["content"] if msgs else ""

    print("调用成功：", turn is not None)
    print("消息条数：", len(msgs))
    print("任务里含玩家原话：", DOING in task)
    print("任务里禁止按天编号：", "不要按天编号" in task)
    print("系统头长度：", len(head))
    print("\n---- 发给模型的最后一条（任务）----")
    print(task)

    # 对照：不填 doing 时应当走旧的「多天」提示词
    seen.clear()
    with mock.patch.object(persona, "_llm_call", side_effect=fake):
        persona.skip_day(key, "", days=7)
    old_task = (seen.get("msgs") or [{}])[-1].get("content", "")
    print("\n不填时走高条模板：", "时间已经过去" in old_task
          and DOING not in old_task)

tmp.cleanup()

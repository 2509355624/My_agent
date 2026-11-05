# -*- coding: utf-8 -*-
"""离线验证：弹窗发的那句普通消息，怀孕还记不记（2026-11-06）。

用户问：删了那套独立流程之后，射进去还会自动记账吗？
这里把编剧和演员都换成假函数，跑一次 `run_turn`，看暴露有没有落盘。
"""
import io
import json
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

SCORE = {
    "intent": "intimate", "delta": 2, "turn_type": "act", "in_scene": True,
    "act": "intense", "finish": "inside", "spot": "pussy",
    "dev": {"mouth": 0, "nipple": 0, "pussy": 8, "anus": 0, "skin": 0},
    "excite": 20, "arousal": 10, "corrupt": 0, "submit": 0, "suspicion": 0,
    "cross": "none", "allow": "yes", "beat": False, "soothe": False,
    "time": "none", "phase": "", "plan": "", "place": "",
    "reason": "射在里面", "note": "", "segments": [], "addressed": True,
    "literal": "", "subtext": "", "noticed": "", "stance": "接住",
    "persona_mode": "日常", "tension": "", "lead": "none", "lead_form": "",
    "lead_reason": "", "hook": "",
}

with mock.patch.object(agents, "AGENTS_DIR", root), \
        mock.patch.object(persona, "_STATE_PATH", state):
    agents.clear_cache()
    persona.save_cards([{"name": "测试", "systemPrompt": "你是她。"}])
    persona.switch_line("private_1", persona.find_card("测试"))
    key = persona.resolve_line("private_1")
    affection.set_mode(key, True)

    with mock.patch.object(persona, "_judge_intent_scored",
                           return_value=("intimate", "", SCORE)), \
            mock.patch.object(persona, "_llm_call",
                              return_value="她抖了一下。\n画面：1girl\n"
                                           "心情：害羞 强度7\n状态：床上"):
        turn = persona.run_turn(key, "（我把精液射进她的小穴里）", "")

    ent = persona._entry(key) or {}
    preg = ent.get("preg") or {}
    print("这一轮有正文：", bool(turn and turn.reply.strip()))
    print("怀孕状态：", json.dumps(
        {"status": preg.get("status"), "exposures": preg.get("exposures")},
        ensure_ascii=False))
    ok = bool(preg.get("exposures")) or preg.get("status") == "pregnant"
    print("内射记账：", "✓ 记了" if ok else "✗ 没记")

tmp.cleanup()

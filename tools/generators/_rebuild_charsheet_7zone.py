# -*- coding: utf-8 -*-
"""把 qwen-charsheet 恢复成最初的「7 分区 character design sheet」版式。

静态提示词的唯一来源 = `skills/qwen-charsheet/workflow.ui_backup.json` 里那个
`PrimitiveStringMultiline`（2559 字，本来就带「右下 2×3 细节格不画任何文字」
那条修正——用户这次要的「右下角不要字体」是现成的，不用另改）。

改三处（两份 workflow 一起改）：
  node 7  EmptyLatentImage           2304×1296 → **2304×1536**（3:2；提示词里写死
                                     "3:2 landscape"，画布必须跟它一致）
  node 8  TextEncodeQwenImage21.prompt  参考姿势+三视图 → **7 分区静态提示词**
  node 9  KSampler.steps             18 → **25**（300 秒预算下 §11 实测最干净那档）

⚠️ workflow.json 里 seed 是**未加引号的** `__SEED__`（非法 JSON），照 §19 的套路
   先换成带引号的占位符再 parse、写回时还原。
"""
import json
import os

SKILL = r"D:\AI\agent_my_test\skills\qwen-charsheet"
BACKUP = os.path.join(SKILL, "workflow.ui_backup.json")
W, H, STEPS = 2304, 1536, 25
PLACE = "__SEED_PLACEHOLDER__"

ui = json.load(open(BACKUP, encoding="utf-8"))
static = next(n["widgets_values"][0] for n in ui["nodes"]
              if n["type"] == "PrimitiveStringMultiline")
print("[*] 静态提示词长度:", len(static))
assert "No text, labels, or captions" in static, "右下细节格的无文字修正丢了"
combined = static + "\n\n" + "__MULTI_PROMPTS__"

for name in ("workflow.json", "workflow_i2i.json"):
    p = os.path.join(SKILL, name)
    raw = open(p, encoding="utf-8").read().replace("__SEED__", '"%s"' % PLACE)
    wf = json.loads(raw)
    assert wf["7"]["class_type"] == "EmptyLatentImage"
    assert wf["8"]["class_type"] == "TextEncodeQwenImage21"
    assert wf["9"]["class_type"] == "KSampler"
    wf["7"]["inputs"]["width"] = W
    wf["7"]["inputs"]["height"] = H
    wf["8"]["inputs"]["prompt"] = combined
    wf["9"]["inputs"]["steps"] = STEPS
    out = json.dumps(wf, ensure_ascii=False, indent=2)
    out = out.replace('"%s"' % PLACE, "__SEED__")
    with open(p, "w", encoding="utf-8") as f:      # 默认换行转换 → CRLF，与基线一致
        f.write(out)
    print("[*] 已写 %s：%dx%d / %d 步 / prompt %d 字"
          % (name, W, H, STEPS, len(combined)))

# -*- coding: utf-8 -*-
"""单条用例 + 指定 brief 开关，跑 N 遍看稳定性。

用法: python _dryrun_one.py brief|full <重复数> "<原话>" <自己发图0|1> <引用0|1>
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MODE = sys.argv[1] if len(sys.argv) > 1 else "brief"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 1
TEXT = sys.argv[3] if len(sys.argv) > 3 else "qwen 重绘一下这只手"
OWN = [None] if (len(sys.argv) > 4 and sys.argv[4] == "0") else ["http://x/2.jpg"]
QUOTED = [None] if (len(sys.argv) > 5 and sys.argv[5] == "0") else None

from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"), override=False)
os.environ["PROMPT_BRIEF"] = "1" if MODE == "brief" else "0"

import _dryrun_gen as d
from app import agent_prompt as ap
print("### mode=%s brief=%s 系统头=%d 字  原话=%r"
      % (MODE, ap._brief_mode(),
         len(ap.build_system_prompt(message_count=30, last_tool="generate_image",
                                    agent_id="qq")), TEXT))
for i in range(N):
    calls, body = d.one_round(TEXT, own_images=OWN, quoted_images=QUOTED)
    print(">>> 第%d遍: %s" % (i + 1, ("skill=%r src=%r" % (calls[0].get("skill"),
          calls[0].get("source_image"))) if calls else "没调工具"))

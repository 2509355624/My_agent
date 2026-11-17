# -*- coding: utf-8 -*-
"""真机冒烟：silver 渠道 + source_image（本地文件），走完整 generate_image 链路。

验的是本次改动新增的「锁构图」骨架在 turbo 底模上能不能跑通：
resolve -> fit -> upload -> 占位符注入 -> 入队 -> worker -> 取图。
只读/只写 state/image_log.jsonl（追加一行），不碰别的生产状态。
"""
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

REF = os.path.join(ROOT, "_ref02_参考图.png")
PROMPT = ("1girl, solo, standing, hands on hips, looking at viewer, "
          "green hair, taoist hat, green robe, candlelight, dark room, "
          "full body")
SEED = 777001


def main():
    print("[smoke] ref exists = %s (%s)" % (os.path.isfile(REF), REF), flush=True)
    from app.tools.normal import generate_image as gi
    print("[smoke] _I2I_LLLITE_SKILLS = %r" % (gi._I2I_LLLITE_SKILLS,), flush=True)
    print("[smoke] silver in _I2I_SKILLS = %s" % ("silver" in gi._I2I_SKILLS),
          flush=True)
    print("[smoke] skill=silver seed=%d" % SEED, flush=True)

    t0 = time.time()
    try:
        out = gi._generate_image(PROMPT, skill="silver", source_image=REF,
                                 seed=SEED, _skip_confirm=True)
    except Exception:
        print("[smoke] EXCEPTION after %.1fs:\n%s"
              % (time.time() - t0, traceback.format_exc()), flush=True)
        return 1

    print("[smoke] elapsed %.1fs" % (time.time() - t0), flush=True)
    print("[smoke] result:\n%s" % (out,), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

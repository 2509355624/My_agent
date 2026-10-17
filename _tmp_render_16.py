"""一次性验证脚本（出图版）：把第三轮编剧产物 _tmp_verify_16.md 当剧本，
用 anima-line 真出 03/04/05 三格的场景图，检验新规范在**图上**是否有效：

  03 格 —— 百合向第一人称，入画的手应该是女生的手（旧版是男生的手）
  04 格 —— 镜头对着学姐，画面里该出现银发灰开衫黑框眼镜的学姐（旧版只有「我」）
  05 格 —— 学妹摸头，两张脸要分得清（旧版角色错乱）

只跑阶段一（silver_only=True），不加气泡——气泡那部分用户已确认解决。
跑完看 comics/C20261012-04/raw/。跑完即可删。
"""
import logging
import os
import shutil
import sys
import time

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(message)s",
                    stream=sys.stdout)

from app import comic  # noqa: E402

SRC = "_tmp_verify_16.md"
DST = os.path.join("comics", "C20261012-04")
PANELS = ("03", "04", "05")

os.makedirs(os.path.join(DST, "raw"), exist_ok=True)
shutil.copyfile(SRC, os.path.join(DST, "script.md"))

script_path = os.path.join(DST, "script.md")
meta, panels = comic.parse_script(script_path)

print("解析到 %d 格：%s" % (len(panels), [p["no"] for p in panels]), flush=True)

task = comic.Task(script_path, meta, panels)
task.save_state()

comic._require_comfy()

t0 = time.time()
for p in task.panels:
    if p.no not in PANELS:
        continue
    print("[%5.1fs] 格 %s 开始渲染（seed 稍后）…" % (time.time() - t0, p.no),
          flush=True)
    try:
        comic.render_scene(task, p)
        print("[%5.1fs] 格 %s 完成 -> %s（seed %s）"
              % (time.time() - t0, p.no, task.raw_path(p), p.seed), flush=True)
    except Exception as e:                                     # noqa: BLE001
        print("[%5.1fs] 格 %s 失败：%s: %s"
              % (time.time() - t0, p.no, type(e).__name__, e), flush=True)
    task.save_state()

print("[%5.1fs] 全部完成" % (time.time() - t0), flush=True)

"""一次性测试（只跑两张图，2026-10-12 用户指定）：

  图1 第一人称 · 手 —— 入画的手能不能是**女生的手**   （已出：01_first_person_hand.png）
  图2 第一人称 · 两人 —— 场景里的**两个女生**能不能分开画出来（本脚本跑）

角色设定两张图统一（保持一致性）：
  我   = black hair, short hair, purple eyes, white sailor uniform, navy pleated skirt
  学姐 = silver hair, long hair, blue eyes, grey cardigan, black-framed glasses

写法依据（2026-10-12 现查）：
  · 库里没有 female_hands / girl_hands / male_hand —— 手的性别没有标签，
    只能靠 pov 的性别先验 + 袖子；`small_hands` 有、`faceless_male` 有。
  · 多人写法用 Tensor.Art 那套四件套：编号+各自外貌+空间位置+互动。

⚠️ 下载走磁盘兜底：跑完第一张后 ComfyUI 被内存守卫重启过（实测），
   /view 请求会撞上重启窗口报 10054，但图其实已经落在 output 目录里了。
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
from app.tools.normal import generate_image as G  # noqa: E402

OUT = os.path.join("comics", "_pov_test")
COMFY_OUT = r"D:\AI\ComfyUI_v037\output"
SKILL = "anima-line"

TESTS = [
    ("02_first_person_two_girls", "图2 · 第一人称场景里的两个女生",
     # 两个女生同框。Tensor.Art 四件套：人数 + 各自外貌 + 左右位置 + 互动。
     "2girls, yuri, "
     "silver hair, long hair, blue eyes, black-framed glasses, grey cardigan, "
     "black hair, short hair, purple eyes, white sailor uniform, "
     "navy pleated skirt, "
     "standing side by side, holding hands, looking at each other, "
     "classroom, window, afternoon light, upper body, "
     "the silver-haired girl in a grey cardigan stands on the left, "
     "the black-haired girl in a white sailor uniform stands on the right"),
]


def _fetch(filename, dst):
    """HTTP 拿图；撞上 ComfyUI 重启就退回从它自己的 output 目录直取。"""
    try:
        comic._download(filename, dst)
        return "http"
    except Exception as e:                                     # noqa: BLE001
        src = os.path.join(COMFY_OUT, str(filename))
        if os.path.isfile(src):
            shutil.copyfile(src, dst)
            return "disk(%s)" % type(e).__name__
        raise


def _wait_comfy(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if comic.image_jobs.comfy_alive(timeout=4):
            return True
        time.sleep(3)
    return False


def main():
    os.makedirs(OUT, exist_ok=True)
    if not _wait_comfy(150):
        print("ComfyUI 不在线，放弃", flush=True)
        return
    print("ComfyUI 在线", flush=True)

    t0 = time.time()
    for name, title, prompt in TESTS:
        seed = comic._new_seed()
        print("\n" + "=" * 70, flush=True)
        print("[%s]  seed=%s" % (title, seed), flush=True)
        print("—— 实际发给模型的完整提示词 ——", flush=True)
        print(prompt, flush=True)
        print("-" * 70, flush=True)
        wf = G.build_t2i_workflow(SKILL, prompt, seed)
        if wf is None:
            print("!! skill 没有文生图工作流", flush=True)
            continue
        try:
            fn = comic._submit_retry(
                lambda: comic._submit(wf, SKILL, prompt, seed), title)
            dst = os.path.join(OUT, name + ".png")
            how = _fetch(fn, dst)
            print("[%5.1fs] 落盘 -> %s（%s）" % (time.time() - t0, dst, how),
                  flush=True)
        except Exception as e:                                 # noqa: BLE001
            print("[%5.1fs] 失败：%s: %s"
                  % (time.time() - t0, type(e).__name__, e), flush=True)
    print("\n[%5.1fs] 跑完" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    main()

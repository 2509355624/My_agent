"""临时探针：qwen30 图生图，在白衣上刻不同长度的中文，看崩坏点在哪。

走 image_jobs 全局串行队列（跟 QQ 排队队列同一条），渠道 = skills/qwen30。
用完即删，不进仓库。
"""
import os
import sys
import time

sys.path.insert(0, r"D:\AI\agent_my_test")

from app import comic, image_jobs            # noqa: E402

SRC = r"D:\AI\ComfyUI_v037\output\Anima_00480_.png"
OUT = r"D:\AI\agent_my_test\_tmp_qwen30_text"
SKILL = "qwen30"
SEED = 20261016

TIERS = [
    ("05zi", "孔雀东南飞"),
    ("10zi", "孔雀东南飞，五里一徘徊"),
    ("24zi", "孔雀东南飞，五里一徘徊。十三能织素，十四学裁衣。"),
    ("37zi", "孔雀东南飞，五里一徘徊。十三能织素，十四学裁衣。十五弹箜篌，十六诵诗书。"),
]


def prompt_of(txt):
    return ('write the Chinese characters "%s" on the white shirt she is wearing, '
            'keep the pose, face, hair, expression and background exactly the same'
            % txt)


def main():
    if not image_jobs.comfy_alive():
        print("ComfyUI 不在线，先起来再跑")
        return 1
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    print("上传源图…")
    uploaded = comic._upload_source(SRC)
    print("源图 ->", uploaded, "（%.1fs）" % (time.time() - t0))

    for tag, txt in TIERS:
        p = prompt_of(txt)
        t1 = time.time()
        print("\n=== %s ｜ %d 字 ｜ %s" % (tag, len(txt), txt))
        try:
            wf = comic._build_i2i(SKILL, p, SEED, uploaded)
            name = comic._submit_retry(
                lambda: comic._submit(wf, SKILL, p, SEED), tag)
            dst = os.path.join(OUT, "qwen30_%s.png" % tag)
            comic._download(name, dst)
            size = os.path.getsize(dst)
            print("  出图 %s ｜ %.1f KB ｜ 耗时 %.1fs"
                  % (dst, size / 1024.0, time.time() - t1))
        except Exception as e:
            print("  失败：%s: %s" % (type(e).__name__, e))
    print("\n全部完成，总耗时 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())

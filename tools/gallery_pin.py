# -*- coding: utf-8 -*-
"""设置 / 重置画廊「看已隐藏」的 6 位密码。

用法（项目根目录）：
    <venv>\\python.exe tools/gallery_pin.py 839125

密码只写 state/gallery_pin.json（.gitignore），存 PBKDF2 加盐哈希，不进仓库。
忘了只能重跑本脚本重置——明文不可从哈希反推，谁也读不回来。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.features.gallery import pin  # noqa: E402


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit() or len(sys.argv[1]) != 6:
        print("用法: python tools/gallery_pin.py <6 位数字>")
        raise SystemExit(2)
    pin.set_pin(sys.argv[1])
    print("隐藏墙密码已写入 state/gallery_pin.json（不会进仓库）")
    print("  密码: %s" % sys.argv[1])
    print("忘了只能重跑本脚本重置，明文不可从哈希反推。")


if __name__ == "__main__":
    main()

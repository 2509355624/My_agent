# -*- coding: utf-8 -*-
"""设置 / 重置管理页公网登录的账号密码。

用法（在项目根目录）：
    <venv>\\python.exe tools/admin_passwd.py                 # 随机生成强密码
    <venv>\\python.exe tools/admin_passwd.py admin 自定义密码   # 自己指定

凭据只写 state/admin_auth.json（.gitignore），不进仓库。密码用 PBKDF2 加盐
哈希保存；脚本只在生成时打印一次明文，之后谁也读不回来，忘了就重跑本脚本。
"""

import os
import secrets
import string
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import admin_auth  # noqa: E402


def _random_password(n=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def main():
    user = sys.argv[1] if len(sys.argv) > 1 else "admin"
    if len(sys.argv) > 2:
        password = sys.argv[2]
    else:
        password = _random_password()

    admin_auth.set_password(user, password)
    print("管理页登录凭据已写入 state/admin_auth.json（不会进仓库）")
    print("  账号: %s" % user)
    print("  密码: %s" % password)
    print("请妥善保存，忘了只能重跑本脚本重置（重置会使旧登录失效）。")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""一次性修 bat 里的机器相关路径（字节级替换，保持 GBK/CRLF 不变）。

    python _fix_bat_paths.py            # 预览
    python _fix_bat_paths.py --apply    # 写入（每个文件先留 .bak）

背景
----
`e8633ff` 把启动器里的 python 路径统一改成了 `D:\\AI\\confyui_env\\...`，
注释里写明那是「笔记本的 venv」。但台式机上是 `my_env`，`confyui_env` 压根
不存在 —— 所以在台式机上跑这些 bat 会直接卡在「找不到 Python」。
两台机器共用同一个 git 仓库，写死任何一边都会弄坏另一边，所以改成**自动探测**。

同时修 `启动SnowLuma.bat` 里写死的 `D://APP//qq//QQ.exe`（台式机 QQ 在
`C:\\APP\\qq\\QQ.exe`）。
"""
import os
import sys
import shutil

BS = chr(92)   # 反斜杠

QQ_OLD = "D://APP//qq//QQ.exe"
QQ_NEW = "C:" + BS + "APP" + BS + "qq" + BS + "QQ.exe"

PY_OLD = "D:" + BS + "AI" + BS + "confyui_env" + BS + "Scripts" + BS + "python.exe"

# 纯 ASCII，插进任何编码的 bat 都不会出问题
PY_BLOCK = "\r\n".join([
    "rem --- venv python auto-detect: desktop my_env / laptop confyui_env ---",
    'set "PY="',
    'if not defined PY if exist "D:' + BS + 'AI' + BS + 'my_env' + BS + 'Scripts' + BS + 'python.exe" '
    'set "PY=D:' + BS + 'AI' + BS + 'my_env' + BS + 'Scripts' + BS + 'python.exe"',
    'if not defined PY if exist "D:' + BS + 'AI' + BS + 'confyui_env' + BS + 'Scripts' + BS + 'python.exe" '
    'set "PY=D:' + BS + 'AI' + BS + 'confyui_env' + BS + 'Scripts' + BS + 'python.exe"',
    'if not defined PY if exist "D:' + BS + 'AI' + BS + 'comfy_env' + BS + 'Scripts' + BS + 'python.exe" '
    'set "PY=D:' + BS + 'AI' + BS + 'comfy_env' + BS + 'Scripts' + BS + 'python.exe"',
    'if not defined PY set "PY=python"',
    "",
]).encode("ascii")

ANCHOR = b'cd /d "%~dp0"\r\n'

# 每个文件：把裸 python 路径换成带引号的 %PY%
TARGETS = [
    "启动SnowLuma.bat",
    "一键启动.bat",
    "启动QQ机器人.bat",
    "启动看门狗.bat",
    "一键启动全部.bat",
    "一键启动QQ机器人.bat",
]


def main():
    apply = "--apply" in sys.argv
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    for f in TARGETS:
        raw = open(f, "rb").read()
        orig = raw
        notes = []

        # 1) QQ.exe 路径
        n = raw.count(QQ_OLD.encode("ascii"))
        if n:
            raw = raw.replace(QQ_OLD.encode("ascii"), QQ_NEW.encode("ascii"))
            notes.append("QQ_EXE x%d" % n)

        # 2) python 路径 -> %PY%
        #    [!!] 顺序要紧：必须**先把路径替换掉、最后再插探测块**。
        #    反过来的话，替换用的 `"<路径>"` -> `"%PY%"` 会把刚插进去的
        #    探测块里那条候选路径也一起吃掉，生成
        #    `if not defined PY if exist "%PY%" set "PY=%PY%"` 这种废话。
        if PY_OLD.encode("ascii") in raw:
            cnt = raw.count(PY_OLD.encode("ascii"))
            # 命令位 / if exist / echo 三种上下文统一换成 %PY%
            raw = raw.replace(
                b'cmd /k "' + PY_OLD.encode("ascii") + b'"',
                b'cmd /k "%PY%"')
            raw = raw.replace(
                b'"' + PY_OLD.encode("ascii") + b'"',
                b'"%PY%"')
            raw = raw.replace(PY_OLD.encode("ascii"), b"%PY%")
            notes.append("python 路径 x%d -> %%PY%%" % cnt)
            # 最后插探测块（本文件此前没有定义 PY）
            if b'set "PY="' not in raw and ANCHOR in raw:
                raw = raw.replace(ANCHOR, ANCHOR + PY_BLOCK, 1)
                notes.append("插入 PY 探测块")

        if raw == orig:
            print("%-22s 无改动" % f)
            continue

        if apply:
            shutil.copy2(f, f + ".bak")
            open(f, "wb").write(raw)
            print("%-22s 已写入: %s" % (f, ", ".join(notes)))
        else:
            print("%-22s 待改: %s" % (f, ", ".join(notes)))

    if not apply:
        print()
        print("（预览模式。加 --apply 才写入，写入前每个文件留 .bak）")


if __name__ == "__main__":
    main()

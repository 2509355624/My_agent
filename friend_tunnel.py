# -*- coding: utf-8 -*-
"""起一条 Cloudflare Quick Tunnel 指向群友页（5175），并把地址落盘。

## 为什么要包这一层

Quick Tunnel 的地址是**随机**的，而且只印在它自己的输出里，重启就换。群友页
那条 `/预设网页` 指令要报地址，就得有人把它写进 `state/friend_url.txt`。
让用户每次手动复制粘贴，迟早会贴错、贴到一个已经 1033 的旧地址上——那种
「链接打不开」的报错最难查，因为服务其实是活的。

所以：谁拿到地址谁写文件。这里只干三件事——起进程、边转发输出边抓
`https://xxx.trycloudflare.com`、抓到就写。

## 免费到什么程度

Quick Tunnel 不需要账号、域名、信用卡；带宽不限速（走 Cloudflare 边缘）。
代价写在 `app/friend_site.py` 那边：地址会变、没有 SLA、并发约 200 个请求
会返回 429——群友页自己还有三道限流，比这更严，撞不到这个上限。
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 只认它自己的域名：cloudflared 的输出里还有一堆别的 https 链接（文档、
# 报错帮助），抓错一个就会把帮助页地址当成群友页发出去。
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

TARGET = os.getenv("FRIEND_TUNNEL_TARGET", "http://127.0.0.1:5175")

_BIN_CANDIDATES = (
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    "cloudflared",
)


def _find_bin():
    for p in _BIN_CANDIDATES:
        if p == "cloudflared" or os.path.isfile(p):
            return p
    return "cloudflared"


def _write_url(url):
    path = os.path.join(ROOT, "state", "friend_url.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(url + "\n")
    os.replace(tmp, path)
    return path


def _clear_url():
    """隧道断了就把文件删掉。

    不删的后果很具体：地址只在隧道活着的时候有效，进程一没它就 1033。
    留着文件，`/预设网页` 会把一个打不开的链接发给群友，而他们只会觉得
    「这网站坏了」，没人会想到是隧道没了。
    """
    try:
        os.remove(os.path.join(ROOT, "state", "friend_url.txt"))
    except OSError:
        pass


def main():
    exe = _find_bin()
    print("=" * 50)
    print("  FRIEND TUNNEL  ->  %s" % TARGET)
    print("=" * 50)
    print("bin : %s" % exe)
    print("")
    try:
        proc = subprocess.Popen(
            [exe, "tunnel", "--url", TARGET, "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as e:
        print("[ERROR] 起不来：%s" % e)
        print("        装 cloudflared：winget install Cloudflare.cloudflared")
        return 1

    got = ""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if got:
                continue
            m = URL_RE.search(line)
            if m:
                got = m.group(0)
                path = _write_url(got)
                print("")
                print("[OK] 地址已写入 %s" % path)
                print("[OK] %s" % got)
                print("[OK] 群友私聊机器人发 /预设网页 就能拿到这个地址")
                print("")
    except KeyboardInterrupt:
        print("\n[BYE] 收到中断，正在收隧道…")
    finally:
        # 走出这个循环 = 隧道已经没了（cloudflared 自己退了，或者被 Ctrl+C
        # 打断），地址必然失效，所以无条件清掉。
        # ⚠️ 直接关 bat 窗口时 Windows 会硬杀进程，`finally` 跑不到，文件会
        # 残留——那种情况下 `/预设网页` 可能发一次死链，重开隧道就被覆盖了。
        if got:
            _clear_url()
            print("[BYE] 隧道已断，已清掉 state/friend_url.txt（旧地址不再发出）")
    if not got:
        print("")
        print("[WARN] 整条隧道没抓到地址（进程已退出 code=%s）。"
              % proc.wait())
        print("       多半是网络连不上 Cloudflare，重开一次试试。")
        return 1
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())

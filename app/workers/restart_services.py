# -*- coding: utf-8 -*-
"""看门狗用的隐藏重启助手：替代 `一键启动全部.bat force auto`。

旧批处理里每个服务都是 `start "TITLE" cmd /k ...`，即使根 cmd 用
CREATE_NO_WINDOW 隐藏，`start` 仍会新开一排 cmd 窗口。看门狗每轮重启时用户
就看到窗口不停闪。

这里改为启动一个**独立的隐藏进程**，由 `ServiceManager` 用
CREATE_NO_WINDOW 拉起服务；进程本身不产生任何窗口。只重启 SnowLuma 链路真正
需要的三项，避免无谓地连带重启群友页/隧道。
"""

from __future__ import annotations

import sys

from app.features.control.manager import ServiceManager

# 旧看门狗重启的是「Agent Web / SnowLuma / QQBOT-ADAPTER」三项。
DEFAULT_IDS = ("agent", "snowluma", "qqbot")


def main(argv: list[str] | None = None) -> int:
    ids = list(argv or sys.argv[1:]) or list(DEFAULT_IDS)
    manager = ServiceManager()
    for sid in ids:
        if sid in manager.specs:
            manager.restart(sid)
    # 助手进程必须等到重启线程收尾；否则 daemon 线程随进程退出，服务半途而废。
    manager.wait_idle(ids, timeout=120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

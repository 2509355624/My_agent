"""统一日志配置：控制台（stderr）+ 落盘文件（按大小轮转）。

**为什么要有这个**：agent 自己的诊断（`[llm]` / `[chain]` / `[cache]` / `[window]`
/ `[compact]`）原先全走 `print()`，落到 **stdout**；而 `logging` 落到 **stderr**。
在控制台里两个流看着一样，但只要输出被接管（Windows Terminal 的 ConPTY、`> file`
重定向、被别的进程当子进程拉起），stdout 就变成**块缓冲**——`logging` 的行照常出现，
`print` 的行要攒满 8KB 才吐一次，看起来就像"日志丢了"。2026-09-27 用户报
「看不到模型调用日志」根因就在这里。

现在两条都收敛到 logging：行格式统一（时间戳 + 级别 + logger 名），并且同时写一份
文件，随时能 tail / grep，不再依赖窗口的滚动缓冲。

**文件名按进程分开**：两个进程同写一个文件，在 Windows 上各持自己的写入偏移量，
交错追加会把行写烂。所以 qq_bot 写 `logs/qq_bot.log`，网页端写 `logs/agent.log`。
"""

import logging
import logging.handlers
import os

from app.config import BASE_DIR

LOG_DIR = os.path.join(BASE_DIR, "logs")

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# 单文件 5MB、留 3 个备份，够查几天的问题又不至于把盘塞满
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3

# 已经配过的进程名。setup() 幂等——重复调用只会让日志出现重复行。
_configured = set()


def setup(name, level=logging.INFO):
    """给当前进程配好日志。name 只用来定文件名（qq_bot / agent）。

    每个入口（`agent.py` → `app.main.run`、`app.qq_bot.main`）各调一次。
    **没调过的进程里 `log.info` 会被 lastResort 悄悄丢掉**（它只放 WARNING 以上），
    所以新加入口时别忘了这一句——`tests/test_logsetup.py` 有专门的回归断言。

    建目录 / 开文件失败只警告，不拦启动：日志写不出来不该让机器人起不来。
    """
    if name in _configured:
        return
    _configured.add(name)

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()        # 默认 stderr，与旧 basicConfig 一致
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, name + ".log"),
            maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("日志文件打不开（%s），本次只输出到控制台", e)


def get(name):
    """取一个 logger。各模块用它代替裸 `logging.getLogger`，方便统一改名。"""
    return logging.getLogger(name)

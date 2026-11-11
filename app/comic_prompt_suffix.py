# -*- coding: utf-8 -*-
"""漫画台「附加提示词」：全局一份，拼在每格画面词末尾再送生图。

2026-11-12 站主要求：编剧 AI 给完每格提示词之后，能统一在后面追加一段
自定义文本，并且持久保存。拍板的设计口径：

  * **全局一份** —— 所有任务、所有渠道、所有格子共用，不按渠道 / 任务分档；
  * **拼在渲染层** —— `p.scene` 送进生图之前追加，不写回 `script.md` /
    `state.json`。写回剧本会让 `Task.load_state` 判「scene 变了 → 旧图作废」，
    改一次后缀，所有历史任务都得重跑；
  * 只作用于阶段一「画面:」行 —— 不加到「加气泡」的图生图编辑指令，
    写真也没有格子；已出的图不动；
  * 群友版跑图同样吃到（注入在渲染层，跟谁发起无关），但设置框只在
    5174 站主页可见，后端挡群友写全局。

代码只做格式清洗和落盘，不判断这段提示词「合不合理」——判断归模型。
"""
import json
import logging
import os
import re
import threading

from app.config import state_path

FILE = "comic_prompt_suffix.json"
# 只是可用性边界，不是内容闸门：别让一整篇文章塞进每格提示词。
MAX_CHARS = 2000
_LOCK = threading.RLock()

log = logging.getLogger("comic_prompt_suffix")


def path():
    """全局后缀文件路径（测试进程自动导向临时目录，见 config.state_path）。"""
    return state_path(FILE)


def clean(text):
    """只做格式清洗：换行折成逗号、空白压缩、首尾分隔符去掉。不判断内容。"""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]*\n[ \t\n]*", ", ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?:[，,]\s*){2,}", ", ", s)
    return s.strip().strip(",， ").strip()


def load():
    """读全局后缀；文件缺 / 读坏 / 格式不对都按「没填」处理，不挡出图。"""
    fp = path()
    if not os.path.isfile(fp):
        return ""
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("全局附加提示词读不出来（%s），按空处理", e)
        return ""
    if isinstance(data, str):            # 手改成纯文本也能用
        return clean(data)
    if isinstance(data, dict):
        return clean(data.get("suffix") or "")
    log.warning("全局附加提示词格式不认得（%s），按空处理", type(data).__name__)
    return ""


def save(text):
    """写全局后缀（临时文件 + os.replace 原子替换），返回清洗后真正生效的文本。"""
    s = clean(text)
    if len(s) > MAX_CHARS:
        raise ValueError("附加提示词最多 %d 字，现在 %d 字" % (MAX_CHARS, len(s)))
    fp = path()
    with _LOCK:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"suffix": s}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, fp)
    return s


def apply(scene, suffix=None):
    """把后缀拼到画面词末尾；空后缀原样返回，**不改传入的字符串**。

    `suffix=None` 时现读全局文件；给了值就直接用（便于 CLI / 测试注入）。
    """
    x = clean(suffix if suffix is not None else load())
    s = (scene or "").strip()
    if not x:
        return s
    s = s.rstrip(",， ").strip()
    return (s + ", " + x) if s else x
